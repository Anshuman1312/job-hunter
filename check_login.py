"""Report which job sites the saved Playwright profile is signed in to.

Usage:
    python check_login.py

Opens each site in the persistent `browser_session/` profile, then prints a
signed-in / signed-out line per site. Read-only: it never applies or emails.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

from auto_applier import AutoApplier
from config_loader import AppConfig
from utils import setup_logging

# (label, url to open, url fragments that mean "still signed out")
CHECKS: list[tuple[str, str, tuple[str, ...]]] = [
    ("LinkedIn", "https://www.linkedin.com/feed/", ("/login", "/authwall", "/checkpoint", "/signup")),
    ("Indeed", "https://www.indeed.com/", ("/login", "secure.indeed.com", "/account/login")),
    ("Naukri", "https://www.naukri.com/mnjuser/profile", ("nlogin", "/login", "logincheck")),
    ("Glassdoor", "https://www.glassdoor.co.in/index.htm", ("login_input", "/profile/login")),
]


def check_site(page, bad_markers: tuple[str, ...]) -> tuple[bool, str]:
    """Return (signed_in, detail) for an already-navigated page."""

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    url = page.url or ""
    path = urlsplit(url).path.lower() + "?" + urlsplit(url).query.lower()

    if any(marker in path for marker in bad_markers):
        return False, f"redirected to login ({url[:80]})"

    try:
        passwords = page.locator("input[type='password']")
        for index in range(min(passwords.count(), 4)):
            if passwords.nth(index).is_visible():
                return False, f"password field visible ({url[:80]})"
    except Exception:
        pass

    return True, url[:80]


def main() -> int:
    config = AppConfig.load()
    logger = setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    logger.info("Checking saved sessions in %s", config.application.browser_profile_dir)

    applier = AutoApplier(config, logger)
    applier.start()
    results: list[tuple[str, bool, str]] = []
    try:
        assert applier.context is not None
        for label, url, bad in CHECKS:
            page = applier.context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                signed_in, detail = check_site(page, bad)
            except Exception as exc:
                signed_in, detail = False, f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            results.append((label, signed_in, detail))
    finally:
        applier.stop()

    print()
    print(f"{'SITE':<12}{'STATUS':<14}DETAIL")
    print("-" * 78)
    for label, signed_in, detail in results:
        status = "SIGNED IN" if signed_in else "SIGNED OUT"
        print(f"{label:<12}{status:<14}{detail}")
    print()

    signed_out = [label for label, ok, _ in results if not ok]
    if signed_out:
        print("Needs sign-in: " + ", ".join(signed_out))
        print("Run: python main.py login --wait-seconds 300")
        return 1

    print("All checked sites have a saved session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
