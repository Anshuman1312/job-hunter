"""Small shared helpers used across the job-hunter modules."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import random
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent

_TRACKING_PARAMS = {
    "eBP",
    "originalsubdomain",
    "ref",
    "refid",
    "trackingid",
    "trk",
    "tk",
    "lipi",
    "midtoken",
    "mkt_tok",
    "ebclid",
    "campaignid",
    "source",
    "src",
    "from",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gh_src",
    "lever-source",
    "lever-origin",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def load_env_file(path: str | Path) -> None:
    """Load simple ``KEY=VALUE`` pairs from a .env file without extra deps.

    Existing environment variables always win, so shell-provided secrets are
    never overwritten by a local file.
    """

    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def expand_env_placeholders(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with environment values."""

    if isinstance(value, str):
        return _ENV_PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_placeholders(item) for key, item in value.items()}
    return value


def resolve_path(value: str | Path, base: str | Path = PROJECT_ROOT) -> Path:
    """Resolve a possibly relative path against *base*."""

    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()


# --------------------------------------------------------------------------- #
# Data cleaning
# --------------------------------------------------------------------------- #
def safe_str(value: Any) -> str:
    """Convert pandas/JobSpy values (NaN, None, Timestamp) to clean text."""

    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(safe_str(item) for item in value if safe_str(item))
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat", "<na>"}:
        return ""
    return text


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isnan(value):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "remote", "t"}


def strip_html(text: str) -> str:
    """Return readable plain text from a job description."""

    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<\s*(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def clean_id(value: Any) -> str:
    """Normalize a JobSpy id which may arrive as ``12345.0``."""

    text = safe_str(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


# --------------------------------------------------------------------------- #
# URLs and identifiers
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Strip tracking parameters and trailing slashes for stable dedupe."""

    raw = safe_str(url)
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", parts.path)
    if not path:
        path = "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def url_hash(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def job_id_for(site: str, raw_id: Any, url: str) -> str:
    """Build a stable, readable primary key for a job."""

    site_slug = re.sub(r"[^a-z0-9]+", "", safe_str(site).lower()) or "web"
    identifier = clean_id(raw_id)
    if identifier:
        return f"{site_slug}:{identifier}"
    return f"{site_slug}:{url_hash(url)}"


def is_same_platform(url: str, site: str) -> bool:
    host = urlsplit(normalize_url(url)).netloc.lower()
    site = site.lower()
    if site == "linkedin":
        return host.endswith("linkedin.com")
    if site == "indeed":
        return host.endswith("indeed.com")
    if site == "glassdoor":
        return host.endswith("glassdoor.com") or host.endswith("glassdoor.co.in")
    return False


# --------------------------------------------------------------------------- #
# Logging / timing
# --------------------------------------------------------------------------- #
def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    # Windows consoles default to cp1252, which cannot encode emoji used in
    # email subjects. Prefer UTF-8 and never crash on an unencodable character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    logger = logging.getLogger("job_hunter")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def human_delay(min_seconds: float, max_seconds: float, logger: logging.Logger | None = None) -> float:
    """Sleep for a random human-like interval and return the duration used."""

    low = max(0.0, float(min_seconds))
    high = max(low, float(max_seconds))
    duration = random.uniform(low, high)
    if logger:
        logger.debug("Sleeping %.1fs", duration)
    time.sleep(duration)
    return duration


def local_timestamp(moment: datetime | None = None) -> str:
    value = moment or datetime.now().astimezone()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------- #
# JSON I/O
# --------------------------------------------------------------------------- #
def read_json(path: str | Path, default: Any = None) -> Any:
    file_path = Path(path)
    if not file_path.is_file():
        return default
    try:
        return json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: str | Path, data: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temp_path.replace(file_path)


def safe_filename(value: str, fallback: str = "item", max_length: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_str(value)).strip("._-")
    cleaned = cleaned[:max_length].strip("._-")
    return cleaned or fallback


# --------------------------------------------------------------------------- #
# Session checks (shared by `main.py login` and check_login.py)
# --------------------------------------------------------------------------- #
_SITE_LOGIN_MARKERS: dict[str, tuple[str, ...]] = {
    "linkedin": ("/login", "/authwall", "/checkpoint", "/signup"),
    "indeed": ("/login", "secure.indeed.com", "/account/login"),
    "naukri": ("nlogin", "/login", "logincheck"),
    "glassdoor": ("login_input", "/profile/login"),
}


def site_from_url(url: str) -> str:
    """Map a URL to one of the supported job platforms."""

    host = (urlsplit(safe_str(url)).netloc or "").lower()
    for site in ("linkedin", "indeed", "naukri", "glassdoor"):
        if site in host:
            return site
    return ""


def page_signed_in(page: Any) -> bool:
    """Heuristic: a job-site page is signed in unless it shows a login wall.

    Works without knowing each site's navigation markup: a redirect to a login
    URL or a visible password field means "not signed in yet".
    """

    url = safe_str(getattr(page, "url", ""))
    site = site_from_url(url)
    if not site:
        return False

    parts = urlsplit(url)
    path = (parts.path or "").lower() + "?" + (parts.query or "").lower()
    if any(marker in path for marker in _SITE_LOGIN_MARKERS[site]):
        return False

    try:
        passwords = page.locator("input[type='password']")
        for index in range(min(passwords.count(), 4)):
            if passwords.nth(index).is_visible():
                return False
    except Exception:
        pass

    return True
