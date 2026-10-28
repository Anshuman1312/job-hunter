"""Orchestrator: scrape -> dedupe -> match -> auto-apply or email fallback."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Any

from config_loader import AppConfig, ConfigError
from db import Database
from matcher import JobMatcher
from models import Job, MatchResult
from notifier import Notifier
from scraper import dedupe_jobs, fetch_jobs, load_jobs, summary_by_site
from utils import (
    PROJECT_ROOT,
    page_signed_in,
    resolve_path,
    setup_logging,
    site_from_url,
    truncate,
)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-hunter",
        description=(
            "Scrape backend jobs, filter them for 3-5 years Python/Django/FastAPI, "
            "auto-apply where possible and email the rest."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "watch", "login", "stats", "test-email"],
        help="run (default), watch, login, stats or test-email",
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.json"),
        help="Path to config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without applying, emailing or writing state.",
    )
    parser.add_argument(
        "--no-scrape",
        action="store_true",
        help="Reuse the most recent cached job list instead of scraping.",
    )
    parser.add_argument(
        "--jobs-file",
        default=None,
        help="Load jobs from a specific JSON file (useful for tests).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N matched jobs.")
    parser.add_argument(
        "--max-applies",
        type=int,
        default=None,
        help="Override the daily automatic-application cap for this run.",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Run the browser without a visible window.",
    )
    parser.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Force a visible browser window.",
    )
    parser.add_argument(
        "--include-seen",
        action="store_true",
        help="Re-process jobs that already exist in the database.",
    )
    parser.add_argument(
        "--sites",
        default=None,
        help="Comma-separated site override, e.g. linkedin,indeed",
    )
    parser.add_argument(
        "--boards-only",
        action="store_true",
        help=(
            "Only fetch the custom feeds (YC, Wellfound, Cutshort, Foundit and "
            "referral-company career sites); skip JobSpy sites."
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=None,
        help="login: keep the browser open N seconds and close it once sign-in is detected.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="watch: minutes between cycles (default from config).",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="watch: stop after N cycles (0 = run forever).",
    )
    return parser


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def cmd_run(config: AppConfig, args: argparse.Namespace, logger: logging.Logger) -> int:
    dry_run = bool(args.dry_run)
    if args.headless is not None:
        config.application.headless = bool(args.headless)
        logger.info("Browser headless override: %s", config.application.headless)

    if args.max_applies is not None:
        if args.max_applies < 0:
            logger.error("--max-applies cannot be negative")
            return 2
        config.application.max_daily_auto_applies = args.max_applies

    if args.sites:
        sites = [item.strip().lower() for item in args.sites.split(",") if item.strip()]
        if not sites:
            logger.error("--sites did not contain any platform names")
            return 2
        for query in config.search:
            query.sites = sites
        logger.info("Scraping sites overridden to: %s", ", ".join(sites))

    logger.info("Run started | dry_run=%s | resume=%s", dry_run, config.application.resume_path)

    # Dry runs use an in-memory database so no state file or rows are created.
    database = Database(":memory:" if dry_run else config.database.path)
    applier = None
    try:
        jobs = _get_jobs(config, args, logger)
        if not jobs:
            logger.warning("No jobs available; nothing to do")
            return 0

        jobs = dedupe_jobs(jobs)
        logger.info(
            "Loaded %d unique jobs (%s)",
            len(jobs),
            ", ".join(f"{site}={count}" for site, count in summary_by_site(jobs).items()),
        )

        known = set() if args.include_seen else database.known_ids([job.id for job in jobs])
        unseen = [job for job in jobs if job.id not in known]
        skipped_seen = len(jobs) - len(unseen)
        logger.info(
            "Deduplication: %d unseen, %d already recorded (skipped before parsing)",
            len(unseen),
            skipped_seen,
        )

        matcher = JobMatcher(config.filters)
        matched: list[tuple[Job, MatchResult]] = []
        rejection_counts: dict[str, int] = {}
        for job in unseen:
            result = matcher.match(job)
            if result.accepted:
                matched.append((job, result))
            else:
                reason = result.rejections[0] if result.rejections else "unknown reason"
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                logger.debug("Rejected %s | %s | %s", job.id, job.title, result.summary)

        matched.sort(key=lambda item: item[1].score, reverse=True)
        # Roles at referral companies go first; the sort above already orders
        # each group by score and Python's sort is stable.
        matched.sort(key=lambda item: 0 if config.referral_for(item[0].company) else 1)
        if args.limit is not None:
            matched = matched[: max(0, args.limit)]

        logger.info("Matching: %d of %d unseen jobs passed strict filters", len(matched), len(unseen))
        _log_rejection_summary(rejection_counts, logger)

        if not matched:
            return 0

        if config.email.send_job_digest:
            digest_stats: dict[str, int] = {
                "scraped": len(jobs),
                "already_seen": skipped_seen,
                "matched": len(matched),
                "emailed": 0,
                "email_failed": 0,
            }
            code = _run_digest(config, database, matched, dry_run, digest_stats, logger)
            _print_summary(digest_stats, dry_run, logger)
            return code

        notifier: Notifier | None = None
        if not dry_run:
            notifier = Notifier(config, logger)

        cap = config.application.max_daily_auto_applies
        applied_today = database.count_applied_today()
        remaining = max(0, cap - applied_today)
        logger.info("Auto-apply quota: %d used today, cap %d, %d remaining", applied_today, cap, remaining)
        auto_possible = bool(config.application.auto_apply_enabled and not dry_run)
        if auto_possible and any(job.is_easy_apply for job, _ in matched):
            try:
                from auto_applier import AutoApplier

                applier = AutoApplier(config, logger)
                applier.start()
            except Exception as exc:
                logger.error(
                    "Could not start browser automation: %s: %s. "
                    "All Easy Apply jobs will use the email fallback.",
                    type(exc).__name__,
                    exc,
                )
                if applier is not None:
                    applier.stop()
                applier = None

        stats: dict[str, int] = {
            "scraped": len(jobs),
            "already_seen": skipped_seen,
            "matched": len(matched),
            "auto_applied": 0,
            "email_fallback": 0,
            "email_failed": 0,
            "skipped": 0,
            "would_auto_apply": 0,
            "would_email": 0,
        }

        try:
            for index, (job, match) in enumerate(matched, start=1):
                logger.info(
                    "[%d/%d] %s @ %s | %.1f | %s",
                    index,
                    len(matched),
                    job.title,
                    job.company,
                    match.score,
                    job.display_location,
                )
                match_notes = _match_notes(job, match)

                if dry_run:
                    _plan_dry_run(job, config, remaining, stats, logger)
                    if job.is_easy_apply and config.application.auto_apply_enabled and remaining > 0:
                        remaining -= 1
                    continue

                if not config.application.auto_apply_enabled:
                    _email_fallback(
                        database,
                        notifier,
                        job,
                        "Automatic applications are disabled in config.json",
                        match_notes,
                        stats,
                        logger,
                    )
                    continue

                if not job.is_easy_apply:
                    _email_fallback(
                        database,
                        notifier,
                        job,
                        "No Easy Apply option detected; the application is external",
                        match_notes,
                        stats,
                        logger,
                    )
                    continue

                if remaining <= 0:
                    _email_fallback(
                        database,
                        notifier,
                        job,
                        f"Daily auto-apply limit reached ({cap} per day)",
                        match_notes,
                        stats,
                        logger,
                    )
                    continue

                if applier is None:
                    _email_fallback(
                        database,
                        notifier,
                        job,
                        "Browser automation is unavailable; run `python main.py login` and retry",
                        match_notes,
                        stats,
                        logger,
                    )
                    continue

                result = applier.apply(job)
                if result.status == "APPLIED_AUTO":
                    note = f"{match_notes}; auto_apply: {result.message}"
                    if result.screenshot:
                        note += f"; screenshot={result.screenshot}"
                    database.record(
                        job.id, job.title, job.company, job.job_url, "APPLIED_AUTO", note
                    )
                    stats["auto_applied"] += 1
                    remaining = max(0, remaining - 1)
                    applied_today += 1
                    logger.info("AUTO-APPLIED: %s @ %s", job.title, job.company)
                    if notifier is not None:
                        notifier.send_auto_applied(job, match_notes)
                    continue

                if result.status == "EXTERNAL":
                    _email_fallback(
                        database,
                        notifier,
                        job,
                        result.message or "External application link detected",
                        match_notes,
                        stats,
                        logger,
                    )
                    continue

                if result.status == "SKIPPED" and result.code == "already_applied":
                    database.record(
                        job.id,
                        job.title,
                        job.company,
                        job.job_url,
                        "FAILED_AUTO",
                        f"{match_notes}; skipped: {result.message}",
                    )
                    stats["skipped"] += 1
                    logger.info("Skipped (already applied): %s @ %s", job.title, job.company)
                    continue

                # FAILED or SKIPPED (login/browser issue): record the failure,
                # then immediately dispatch the manual-apply email.
                failure_note = f"{match_notes}; auto_apply failed ({result.code}): {result.message}"
                if result.screenshot:
                    failure_note += f"; screenshot={result.screenshot}"
                database.record(
                    job.id, job.title, job.company, job.job_url, "FAILED_AUTO", failure_note
                )
                _email_fallback(
                    database,
                    notifier,
                    job,
                    f"Auto-apply failed: {result.message}",
                    match_notes,
                    stats,
                    logger,
                    already_recorded=True,
                )

            if not dry_run and notifier is not None:
                if config.email.send_run_summary and (
                    stats["auto_applied"] + stats["email_fallback"] + stats["email_failed"] > 0
                ):
                    summary = {
                        key: value for key, value in stats.items() if not key.startswith("would_")
                    }
                    try:
                        notifier.send_run_summary(summary)
                    except Exception as exc:
                        logger.warning("Could not send run summary email: %s", exc)

            _print_summary(stats, dry_run, logger)
            return 0
        finally:
            if applier is not None:
                applier.stop()
                applier = None
    finally:
        database.close()


def _get_jobs(
    config: AppConfig,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> list[Job]:
    if args.jobs_file:
        path = resolve_path(args.jobs_file, config.project_root)
        logger.info("Loading jobs from file: %s", path)
        jobs = load_jobs(path, logger)
        if not jobs:
            logger.error("No valid jobs found in %s", path)
        return jobs

    if args.no_scrape:
        logger.info("Scraping disabled; loading cache: %s", config.latest_cache_path)
        jobs = load_jobs(config.latest_cache_path, logger)
        if not jobs:
            logger.error(
                "No cached jobs at %s. Run once without --no-scrape first.",
                config.latest_cache_path,
            )
        return jobs

    jobs = fetch_jobs(
        config,
        logger,
        use_cache=False,
        boards_only=bool(getattr(args, "boards_only", False)),
    )
    if not jobs:
        cached = load_jobs(config.latest_cache_path, logger)
        if cached:
            logger.warning("Scraping returned nothing; falling back to %d cached jobs", len(cached))
            return cached
    return jobs


def _run_digest(
    config: AppConfig,
    database: Database,
    matched: list[tuple[Job, MatchResult]],
    dry_run: bool,
    stats: dict[str, int],
    logger: logging.Logger,
) -> int:
    """Send one email containing every new match, then mark those jobs seen."""

    cap = max(1, int(config.email.digest_max_jobs))
    selected = matched[:cap]
    omitted = max(0, len(matched) - len(selected))

    notifier = Notifier(config, logger, force_dry_run=dry_run)
    logger.info(
        "Digest: emailing %d job(s) (%d omitted by digest_max_jobs)",
        len(selected),
        omitted,
    )
    try:
        sent = notifier.send_job_digest(
            selected, total_matched=len(matched), omitted=omitted
        )
    except Exception as exc:
        logger.error("Digest email raised: %s: %s", type(exc).__name__, exc)
        sent = False

    if not sent:
        stats["email_failed"] = 1
        logger.error("Digest email failed; jobs stay unseen for the next run")
        return 1

    stats["emailed"] = len(selected)
    if not dry_run:
        for job, _match in selected:
            database.mark_seen(
                job.id,
                job.title,
                job.company,
                job.job_url,
                site=job.site,
                emailed=True,
            )
        logger.info("Digest sent and %d job(s) marked as seen", len(selected))
    else:
        logger.info("[dry-run] Digest rendered to logs/outbox; nothing marked as seen")
    return 0


def _match_notes(job: Job, match: MatchResult) -> str:
    details = "; ".join(match.reasons)
    return (
        f"score={match.score}; {details}; site={job.site or 'unknown'}; "
        f"query={job.search_query or 'n/a'}"
    )


def _email_fallback(
    database: Database,
    notifier: Notifier | None,
    job: Job,
    reason: str,
    match_notes: str,
    stats: dict[str, int],
    logger: logging.Logger,
    already_recorded: bool = False,
) -> bool:
    """Case B: send the manual-apply email and persist the outcome."""

    sent = False
    if notifier is not None:
        try:
            sent = notifier.send_manual_apply(job, reason)
        except Exception as exc:
            logger.error("Email fallback raised: %s: %s", type(exc).__name__, exc)
            sent = False

    note = f"{match_notes}; fallback_reason={reason}"
    if sent:
        if already_recorded:
            # Preserve the earlier FAILED_AUTO note while moving the current
            # status to EMAIL_SENT_EXTERNAL.
            database.update_status(
                job.id, "EMAIL_SENT_EXTERNAL", f"email sent after failure: {reason}"
            )
        else:
            database.record(
                job.id,
                job.title,
                job.company,
                job.job_url,
                "EMAIL_SENT_EXTERNAL",
                note,
            )
        stats["email_fallback"] += 1
        logger.info("EMAIL SENT (manual apply): %s @ %s", job.title, job.company)
        return True

    failure = f"{note}; email dispatch FAILED - submit manually at {job.job_url}"
    if already_recorded:
        database.update_status(job.id, "FAILED_AUTO", failure)
    else:
        database.record(
            job.id,
            job.title,
            job.company,
            job.job_url,
            "FAILED_AUTO",
            failure,
        )
    stats["email_failed"] += 1
    logger.error("EMAIL FAILED for %s @ %s; manual URL: %s", job.title, job.company, job.job_url)
    return False


def _plan_dry_run(
    job: Job,
    config: AppConfig,
    remaining: int,
    stats: dict[str, int],
    logger: logging.Logger,
) -> None:
    if not config.application.auto_apply_enabled:
        action = "EMAIL FALLBACK (auto-apply disabled)"
        stats["would_email"] += 1
    elif not job.is_easy_apply:
        action = "EMAIL FALLBACK (external/no Easy Apply)"
        stats["would_email"] += 1
    elif remaining <= 0:
        action = "EMAIL FALLBACK (daily cap reached)"
        stats["would_email"] += 1
    else:
        action = "AUTO-APPLY (Easy Apply detected)"
        stats["would_auto_apply"] += 1
    logger.info("DRY RUN -> %s | %s", action, job.job_url)


def _log_rejection_summary(counts: dict[str, int], logger: logging.Logger) -> None:
    if not counts:
        return
    total = sum(counts.values())
    logger.info("Rejected %d jobs. Top reasons:", total)
    for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:6]:
        logger.info("  - %4d  %s", count, truncate(reason, 110))


def _print_summary(stats: dict[str, int], dry_run: bool, logger: logging.Logger) -> None:
    logger.info("=" * 62)
    logger.info("Run summary%s", " (dry run)" if dry_run else "")
    for key, value in stats.items():
        if key.startswith("would_") and not dry_run:
            continue
        logger.info("  %-20s %s", key.replace("_", " "), value)
    logger.info("=" * 62)


# --------------------------------------------------------------------------- #
# Other commands
# --------------------------------------------------------------------------- #
def cmd_watch(config: AppConfig, args: argparse.Namespace, logger: logging.Logger) -> int:
    """Stay active: re-scan on a cadence and apply to anything new."""

    import random
    import time
    from datetime import timedelta

    watch_cfg = config.watch
    interval = int(
        getattr(args, "interval", None) or watch_cfg.interval_minutes or 45
    )
    interval = max(watch_cfg.min_interval_minutes, interval)
    cycles_arg = getattr(args, "cycles", None)
    max_cycles = watch_cfg.max_cycles if cycles_arg is None else int(cycles_arg)

    logger.info(
        "Watch mode | every %d min (+/- %ds) | max_cycles=%s | dry_run=%s | interval floor=%d min",
        interval,
        watch_cfg.jitter_seconds,
        max_cycles or "unlimited",
        bool(args.dry_run),
        watch_cfg.min_interval_minutes,
    )
    if bool(args.dry_run):
        logger.info("Dry-run watch: nothing will be applied, emailed or saved.")

    cycle = 0
    while True:
        cycle += 1
        started = datetime.now()
        logger.info(
            "=== Cycle %d starting %s ===", cycle, started.strftime("%d %b %H:%M:%S")
        )
        try:
            code = cmd_run(config, args, logger)
            if code != 0:
                logger.warning("Cycle %d exited with code %s", cycle, code)
        except KeyboardInterrupt:
            logger.info("Watch interrupted by user")
            return 130
        except Exception as exc:
            logger.exception("Cycle %d crashed and was skipped: %s", cycle, exc)

        if max_cycles and cycle >= max_cycles:
            logger.info("Reached max_cycles=%d; stopping watch mode", max_cycles)
            return 0

        jitter = random.uniform(-watch_cfg.jitter_seconds, watch_cfg.jitter_seconds)
        delay = max(60.0, interval * 60 + jitter)
        next_at = datetime.now() + timedelta(seconds=delay)
        logger.info(
            "Sleeping %.0f min; next scan at %s",
            delay / 60,
            next_at.strftime("%d %b %H:%M:%S"),
        )
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Watch stopped by user")
            return 0


def _wait_for_sign_in(
    applier: Any, wait_seconds: int, logger: logging.Logger, grace_seconds: int = 60
) -> bool:
    """Poll open pages until LinkedIn and Indeed look signed in, then hold a
    short grace period so other tabs (Naukri, Glassdoor) can finish too."""

    import time

    deadline = time.time() + max(30, wait_seconds)
    last_report = 0.0
    ready_since: float | None = None

    while time.time() < deadline:
        context = getattr(applier, "context", None)
        if context is None:
            return False

        state = {"linkedin": False, "indeed": False}
        for page in list(context.pages):
            try:
                if page.is_closed():
                    continue
                if page_signed_in(page):
                    site = site_from_url(page.url)
                    if site in state:
                        state[site] = True
            except Exception:
                continue

        now = time.time()
        if state["linkedin"] and state["indeed"]:
            if ready_since is None:
                ready_since = now
                logger.info(
                    "LinkedIn and Indeed signed in — finishing other tabs for %ds, then saving",
                    grace_seconds,
                )
            elif now - ready_since >= grace_seconds:
                logger.info("Sign-in confirmed for LinkedIn and Indeed")
                return True
        else:
            ready_since = None
            if now - last_report > 20:
                logger.info(
                    "Waiting for sign-in... linkedin=%s indeed=%s (%ds left)",
                    "ok" if state["linkedin"] else "pending",
                    "ok" if state["indeed"] else "pending",
                    int(deadline - now),
                )
                last_report = now

        time.sleep(3)
    return False


def cmd_login(config: AppConfig, args: argparse.Namespace, logger: logging.Logger) -> int:
    from auto_applier import AutoApplier

    applier = AutoApplier(config, logger)
    logger.info(
        "Opening LinkedIn and Indeed. Your session is stored in: %s",
        config.application.browser_profile_dir,
    )
    applier.start()
    wait_override = getattr(args, "wait_seconds", None)
    interactive = (
        sys.stdin is not None
        and sys.stdin.isatty()
        and wait_override is None
    )
    try:
        applier.open_login_pages()
        print()
        print("=" * 74)
        print("Log in to LinkedIn and Indeed in the browser window.")
        print("Complete any CAPTCHA / 2FA prompts. Do not close the window.")
        print("Order: do Naukri / Glassdoor FIRST, then LinkedIn last.")
        if interactive:
            print("When both sites show you as signed in, return here and press ENTER.")
        else:
            print(
                f"Sign in now — the window closes automatically once both sites "
                f"detect a session (up to {int(wait_override or config.application.login_wait_seconds)}s)."
            )
        print("=" * 74)
        if interactive:
            try:
                input("Press ENTER to save the browser session... ")
                logger.info("Saving browser session and closing the browser")
            except (EOFError, KeyboardInterrupt):
                print()
                logger.info("Saving browser session and closing the browser")
        else:
            seconds = int(wait_override or config.application.login_wait_seconds)
            signed_in = _wait_for_sign_in(applier, seconds, logger, grace_seconds=120)
            if signed_in:
                logger.info("Saving browser session and closing the browser")
            else:
                logger.warning(
                    "Sign-in was not detected before the timeout; saving the profile anyway"
                )
        # Give the browser a moment to flush cookies to the persistent profile.
        import time

        time.sleep(2)
    finally:
        applier.stop()
    logger.info("Session saved. You can now run: python main.py run")
    return 0


def cmd_stats(config: AppConfig, args: argparse.Namespace, logger: logging.Logger) -> int:
    with Database(config.database.path) as database:
        stats = database.stats()
        rows = database.recent(15)

    print()
    print(f"Database: {config.database.path}")
    print(f"Total applications: {stats['total']}")
    print(f"Jobs emailed (seen): {stats['seen_jobs']}")
    print(f"Auto-applied today: {stats['applied_today']} / {config.application.max_daily_auto_applies}")
    print()
    print(f"{'STATUS':<24}{'COUNT':>7}")
    print("-" * 33)
    for status in ("APPLIED_AUTO", "FAILED_AUTO", "EMAIL_SENT_EXTERNAL"):
        print(f"{status:<24}{stats['by_status'].get(status, 0):>7}")
    print()

    if not rows:
        print("No applications recorded yet.")
        return 0

    print("Recent activity")
    print("-" * 100)
    for row in rows:
        title = truncate(str(row["title"]), 34)
        company = truncate(str(row["company"]), 22)
        print(
            f"{str(row['applied_at'])[:19]:<20}{str(row['status']):<22}"
            f"{title:<36}{company}"
        )
    print()
    return 0


def cmd_test_email(config: AppConfig, args: argparse.Namespace, logger: logging.Logger) -> int:
    notifier = Notifier(config, logger, force_dry_run=bool(args.dry_run))
    if not config.email.password and not notifier.dry_run:
        logger.error(
            "No SMTP password. Set %s in .env before testing.", config.email.password_env
        )
        return 1
    ok = notifier.send_test()
    if ok:
        logger.info("Test email completed successfully")
        return 0
    logger.error("Test email failed")
    return 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = AppConfig.load(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    logger.info("job-hunter | command=%s | config=%s", args.command, config.config_path)
    if not config.resume_exists:
        logger.warning(
            "Resume not found at %s. Form uploads will be skipped.", config.application.resume_path
        )
    if config.email.attach_resume_on_fallback:
        logger.warning(
            "CV attachment is ENABLED for manual-apply emails. Disable "
            "email.attach_resume_on_fallback in config.json if this is not intended."
        )

    try:
        if args.command == "run":
            return cmd_run(config, args, logger)
        if args.command == "watch":
            return cmd_watch(config, args, logger)
        if args.command == "login":
            return cmd_login(config, args, logger)
        if args.command == "stats":
            return cmd_stats(config, args, logger)
        if args.command == "test-email":
            return cmd_test_email(config, args, logger)
        parser.print_help()
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
