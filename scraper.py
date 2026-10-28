"""Job ingestion through python-jobspy with normalization and local caching."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config_loader import AppConfig, SearchQuery
from models import Job
from utils import (
    atomic_write_json,
    is_same_platform,
    job_id_for,
    normalize_url,
    read_json,
    safe_str,
    to_bool,
)


class ScraperUnavailable(RuntimeError):
    """Raised when python-jobspy is not installed."""


def _load_scrape_jobs() -> Any:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ScraperUnavailable(
            "python-jobspy is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return scrape_jobs


def _supported_kwargs(function: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop unsupported keyword arguments across JobSpy versions."""

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return kwargs
    accepts_extra = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_extra:
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


CUSTOM_SITES = {"yc", "wellfound", "cutshort", "foundit"}


def scrape_query(
    query: SearchQuery,
    config: AppConfig,
    logger: logging.Logger,
    sites: list[str] | None = None,
) -> list[Job]:
    scrape_jobs = _load_scrape_jobs()

    kwargs: dict[str, Any] = {
        "site_name": list(sites or query.sites),
        "search_term": query.search_term,
        "location": query.location,
        "results_wanted": query.results_wanted,
        "hours_old": query.hours_old,
        "is_remote": query.is_remote,
        "country_indeed": query.country_indeed,
        "linkedin_fetch_description": config.scraping.linkedin_fetch_description,
        "verbose": config.scraping.verbose,
    }
    if query.google_search_term:
        kwargs["google_search_term"] = query.google_search_term
    if config.scraping.proxy:
        kwargs["proxies"] = [config.scraping.proxy]

    try:
        frame = scrape_jobs(**_supported_kwargs(scrape_jobs, kwargs))
    except Exception as exc:  # JobSpy raises many platform-specific errors
        logger.error("Scrape failed for query %r: %s: %s", query.name, type(exc).__name__, exc)
        return []

    if frame is None:
        logger.warning("Query %r returned no data frame", query.name)
        return []

    try:
        rows = frame.to_dict(orient="records")
    except Exception as exc:
        logger.error("Could not read scrape results for %r: %s", query.name, exc)
        return []

    jobs: list[Job] = []
    for row in rows:
        job = normalize_row(row, query)
        if job is not None:
            jobs.append(job)
    logger.info("Query %r returned %d normalized jobs", query.name, len(jobs))
    return jobs


def scrape_all(
    config: AppConfig,
    logger: logging.Logger,
    boards_only: bool = False,
) -> list[Job]:
    """Scrape every configured source.

    JobSpy covers LinkedIn/Indeed/Glassdoor/Naukri/Google.  YC and Wellfound
    are fetched through their public JSON payloads (see ``boards.py``) because
    JobSpy does not support them.
    """

    all_jobs: list[Job] = []

    if not boards_only and config.scraping.jobspy_enabled:
        for query in config.search:
            jobspy_sites = [site for site in query.sites if site not in CUSTOM_SITES]
            if not jobspy_sites:
                continue
            all_jobs.extend(scrape_query(query, config, logger, sites=jobspy_sites))

    if config.boards.enabled:
        yc_terms = [
            query.search_term for query in config.search if "yc" in query.sites
        ]
        if yc_terms:
            from boards import fetch_yc_jobs

            try:
                all_jobs.extend(fetch_yc_jobs(yc_terms, config, logger))
            except Exception as exc:
                logger.error("YC scrape failed: %s: %s", type(exc).__name__, exc)

        wellfound_roles = [
            query.search_term for query in config.search if "wellfound" in query.sites
        ]
        if wellfound_roles:
            from boards import fetch_wellfound_jobs

            try:
                all_jobs.extend(fetch_wellfound_jobs(wellfound_roles, config, logger))
            except Exception as exc:
                logger.error("Wellfound scrape failed: %s: %s", type(exc).__name__, exc)

        cutshort_roles = [
            query.search_term for query in config.search if "cutshort" in query.sites
        ]
        if cutshort_roles:
            from boards import fetch_cutshort_jobs

            try:
                all_jobs.extend(fetch_cutshort_jobs(cutshort_roles, config, logger))
            except Exception as exc:
                logger.error("Cutshort scrape failed: %s: %s", type(exc).__name__, exc)

        if config.company_sources:
            from company_boards import fetch_company_jobs

            try:
                all_jobs.extend(fetch_company_jobs(config, logger))
            except Exception as exc:
                logger.error(
                    "Company careers scrape failed: %s: %s", type(exc).__name__, exc
                )

        foundit_terms = [
            query.search_term for query in config.search if "foundit" in query.sites
        ]
        if foundit_terms:
            from boards import fetch_foundit_jobs

            try:
                all_jobs.extend(fetch_foundit_jobs(foundit_terms, config, logger))
            except Exception as exc:
                logger.error("Foundit scrape failed: %s: %s", type(exc).__name__, exc)

    return dedupe_jobs(all_jobs)


def normalize_row(row: dict[str, Any], query: SearchQuery) -> Job | None:
    site = safe_str(row.get("site")).lower()
    if "." in site:
        # Some JobSpy versions serialize the Site enum as "Site.LINKEDIN".
        site = site.rsplit(".", 1)[-1]
    job_url = safe_str(row.get("job_url"))
    direct_url = safe_str(row.get("job_url_direct"))
    url = job_url or direct_url
    if not url:
        return None

    title = safe_str(row.get("title"))
    company = safe_str(row.get("company"))
    if not title or not company:
        return None

    raw_id = row.get("id")
    job_id = job_id_for(site, raw_id, url)

    skills_value = row.get("skills")
    if isinstance(skills_value, (list, tuple, set)):
        skills = ", ".join(safe_str(item) for item in skills_value if safe_str(item))
    else:
        skills = safe_str(skills_value)

    location = safe_str(row.get("location"))
    work_from_home = safe_str(row.get("work_from_home_type"))
    is_remote = (
        to_bool(row.get("is_remote"))
        or "remote" in work_from_home.lower()
        or "remote" in location.lower()
    )

    if site == "glassdoor":
        easy_apply = False
    elif direct_url and not is_same_platform(direct_url, site):
        easy_apply = False
    else:
        explicit = row.get("easy_apply", row.get("is_easy_apply"))
        if explicit is not None and safe_str(explicit):
            easy_apply = to_bool(explicit)
        else:
            easy_apply = site in {"linkedin", "indeed"}

    return Job(
        id=job_id,
        title=title,
        company=company,
        location=location,
        is_remote=is_remote,
        job_url=normalize_url(url),
        description=safe_str(row.get("description")),
        is_easy_apply=easy_apply,
        site=site,
        job_url_direct=normalize_url(direct_url) if direct_url else None,
        date_posted=safe_str(row.get("date_posted")),
        job_level=safe_str(row.get("job_level")) or None,
        job_type=safe_str(row.get("job_type")) or None,
        listing_type=safe_str(row.get("listing_type")) or None,
        experience_range=safe_str(row.get("experience_range")) or None,
        skills=skills,
        work_from_home_type=work_from_home or None,
        search_query=query.name,
    )


def dedupe_jobs(jobs: Iterable[Job]) -> list[Job]:
    """Deduplicate by id and normalized URL, keeping the first record."""

    unique: dict[str, Job] = {}
    seen_ids: set[str] = set()
    for job in jobs:
        if job.id and job.id in seen_ids:
            continue
        key = normalize_url(job.job_url) or job.id
        if key in unique:
            continue
        unique[key] = job
        if job.id:
            seen_ids.add(job.id)
    return list(unique.values())


def save_jobs(jobs: list[Job], config: AppConfig, logger: logging.Logger | None = None) -> Path:
    cache_dir = config.scraping.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = [job.model_dump(mode="json") for job in jobs]
    atomic_write_json(config.latest_cache_path, payload)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = cache_dir / f"jobs_{stamp}.json"
    atomic_write_json(snapshot, payload)

    if logger:
        logger.info("Saved %d jobs to %s", len(jobs), config.latest_cache_path)

    # Keep the cache directory small.
    snapshots = sorted(cache_dir.glob("jobs_*.json"))
    for old in snapshots[:-10]:
        try:
            old.unlink()
        except OSError:
            pass
    return snapshot


def load_jobs(path: str | Path, logger: logging.Logger | None = None) -> list[Job]:
    raw = read_json(path, default=[])
    if not isinstance(raw, list):
        return []
    jobs: list[Job] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            job = Job.model_validate(item)
        except Exception as exc:
            if logger:
                logger.warning("Skipping invalid cached job: %s", exc)
            continue
        # Fill missing ids for hand-written/legacy caches.
        if not job.id:
            job.id = job_id_for(job.site, None, job.job_url)
        jobs.append(job)
    return dedupe_jobs(jobs)


def fetch_jobs(
    config: AppConfig,
    logger: logging.Logger,
    use_cache: bool = False,
    boards_only: bool = False,
) -> list[Job]:
    if use_cache:
        jobs = load_jobs(config.latest_cache_path, logger)
        if not jobs:
            logger.warning("No cached jobs found at %s", config.latest_cache_path)
        return jobs

    jobs = scrape_all(config, logger, boards_only=boards_only)
    if jobs:
        save_jobs(jobs, config, logger)
    else:
        logger.warning("No jobs were scraped; keeping any existing cache untouched")
    return jobs


def summary_by_site(jobs: list[Job]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.site or "unknown"] = counts.get(job.site or "unknown", 0) + 1
    return counts
