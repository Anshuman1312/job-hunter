"""Public job feeds for Y Combinator (Work at a Startup) and Wellfound.

Both sites render their listings with JavaScript, but each one embeds the same
JSON payload on the server-rendered page (or exposes the JSON endpoint used by
its own front-end).  We read those payloads directly so no browser is needed.

* YC:      ``/jobs/search?q=...`` returns compact JSON; each job detail page
           carries ``data-page`` JSON with experience and the description.
* Wellfound: ``/role/r/<role>`` embeds an Apollo cache in ``__NEXT_DATA__``
           that already contains experience, remote flags and descriptions.

The functions below only fetch and normalise data.  Filtering stays in
``matcher.py`` so every source follows exactly the same rules.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from datetime import datetime
from typing import Any, Iterable

from bs4 import BeautifulSoup
from curl_cffi import requests as http

from config_loader import AppConfig
from models import Job
from utils import job_id_for, normalize_url, safe_str, strip_html

YC_BASE = "https://www.workatastartup.com"
YC_SEARCH = f"{YC_BASE}/jobs/search"
WELLFOUND_BASE = "https://wellfound.com"
CUTSHORT_BASE = "https://cutshort.io"
FOUNDIT_API = "https://www.foundit.in/middleware/jobsearch"

# Only fetch YC detail pages for titles that could plausibly match.  This keeps
# a run to a few dozen requests instead of hundreds.
_YC_TITLE_HINTS = (
    "python",
    "data engineer",
    "analytics engineer",
    "data platform",
    "data infrastructure",
    "data systems",
    "big data",
    "etl",
    "backend",
    "back end",
    "back-end",
    "software engineer",
    "platform engineer",
    "api engineer",
    "database",
    "django",
    "fastapi",
    "full stack",
    "fullstack",
    "full-stack",
)

_YC_TITLE_BLOCKERS = (
    "intern",
    "internship",
    "trainee",
    "fresher",
    "sales",
    "account executive",
    "recruiter",
    "designer",
    "marketing",
    "customer success",
    "support engineer",
    "solutions engineer",
    "engineering manager",
    "product manager",
)

_WELLFOUND_ROLE_ALIASES = {
    "python developer": "python-developer",
    "python engineer": "python-developer",
    "data engineer": "data-engineer",
    "backend engineer": "backend-engineer",
    "backend developer": "backend-engineer",
    "software engineer": "software-engineer",
}


def _session() -> http.Session:
    session = http.Session(impersonate="chrome")
    return session


def _get(
    session: http.Session,
    url: str,
    config: AppConfig,
    logger: logging.Logger,
    *,
    expect_json: bool = False,
    headers: dict[str, str] | None = None,
) -> Any | None:
    attempts = max(1, int(config.boards.max_retries) + 1)
    for attempt in range(1, attempts + 1):
        try:
            request_headers = {"Accept-Language": "en-US,en;q=0.9"}
            if headers:
                request_headers.update(headers)
            response = session.get(
                url,
                timeout=max(10, int(config.boards.timeout_seconds)),
                headers=request_headers,
            )
            if response.status_code != 200:
                logger.warning(
                    "Board request %s returned HTTP %s (attempt %d/%d)",
                    url,
                    response.status_code,
                    attempt,
                    attempts,
                )
            elif expect_json:
                return response.json()
            else:
                return response.text
        except Exception as exc:  # network errors, JSON errors, Cloudflare
            logger.warning(
                "Board request failed %s: %s: %s (attempt %d/%d)",
                url,
                type(exc).__name__,
                exc,
                attempt,
                attempts,
            )
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    return None


# --------------------------------------------------------------------------- #
# Y Combinator - Work at a Startup
# --------------------------------------------------------------------------- #
def _yc_title_is_candidate(title: str) -> bool:
    text = title.lower()
    if any(blocker in text for blocker in _YC_TITLE_BLOCKERS):
        return False
    return any(hint in text for hint in _YC_TITLE_HINTS)


def _yc_location_is_candidate(location: str, config: AppConfig) -> bool:
    text = location.lower()
    if "remote" in text:
        if any(term in text for term in config.filters.remote_blocklist):
            # "Remote (India)" and "Remote - Worldwide" stay eligible.
            return any(term in text for term in ("india", "asia", "worldwide", "global", "anywhere"))
        return True
    return "india" in text or any(city in text for city in config.filters.allowed_cities)


def _yc_detail(session: http.Session, job_id: str, config: AppConfig, logger: logging.Logger) -> dict[str, Any]:
    html = _get(session, f"{YC_BASE}/jobs/{job_id}", config, logger)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    holder = soup.find("div", attrs={"data-page": True})
    if holder is None:
        return {}
    try:
        page = json.loads(holder["data-page"])
    except (TypeError, ValueError):
        return {}
    job = page.get("props", {}).get("job")
    return job if isinstance(job, dict) else {}


def _yc_job(
    item: dict[str, Any],
    detail: dict[str, Any],
    search_term: str,
) -> Job:
    raw_id = safe_str(item.get("id") or detail.get("id"))
    job_url = f"{YC_BASE}/jobs/{raw_id}"
    title = safe_str(detail.get("title") or item.get("title"))
    company = safe_str(item.get("companyName")) or "YC startup"
    location = safe_str(detail.get("location") or item.get("location"))

    description_parts = [
        strip_html(safe_str(detail.get("descriptionHtml"))),
        strip_html(safe_str(detail.get("interviewProcessHtml"))),
    ]
    description = "\n".join(part for part in description_parts if part)
    if not description:
        description = safe_str(item.get("companyOneLiner"))

    salary = safe_str(detail.get("salaryRange") or item.get("salary"))
    if salary:
        description = f"Salary: {salary}\n{description}"

    skills_value = detail.get("skills")
    if isinstance(skills_value, (list, tuple, set)):
        skills = ", ".join(safe_str(skill) for skill in skills_value)
    else:
        skills = safe_str(skills_value)

    experience = safe_str(detail.get("minExperience"))
    role_type = safe_str(item.get("roleType"))
    if role_type:
        skills = f"{role_type}, {skills}" if skills else role_type

    return Job(
        id=job_id_for("yc", raw_id, job_url),
        title=title,
        company=company,
        location=location,
        is_remote="remote" in location.lower() or "remote" in title.lower(),
        job_url=normalize_url(job_url),
        description=description,
        is_easy_apply=False,
        site="yc",
        job_type=safe_str(detail.get("jobType") or item.get("jobType")) or None,
        experience_range=experience or None,
        skills=skills,
        search_query=search_term,
    )


def fetch_yc_jobs(
    search_terms: Iterable[str],
    config: AppConfig,
    logger: logging.Logger,
) -> list[Job]:
    """Search Work at a Startup and enrich the best candidates with details."""

    session = _session()
    terms = [term.strip() for term in search_terms if term.strip()]
    if not terms:
        return []

    candidates: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        for term in terms:
            url = f"{YC_SEARCH}?q={urllib.parse.quote(term)}"
            payload = _get(session, url, config, logger, expect_json=True)
            rows = (payload or {}).get("jobs") if isinstance(payload, dict) else None
            if not rows:
                logger.warning("YC search returned no jobs for %r", term)
                continue
            added = 0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                title = safe_str(item.get("title"))
                location = safe_str(item.get("location"))
                if not title or not _yc_title_is_candidate(title):
                    continue
                if not _yc_location_is_candidate(location, config):
                    continue
                raw_id = safe_str(item.get("id"))
                if raw_id and raw_id not in candidates:
                    candidates[raw_id] = (item, term)
                    added += 1
            logger.info("YC search %r: %d new candidates", term, added)
            time.sleep(config.boards.page_delay_seconds)
    finally:
        session.close()

    if not candidates:
        return []

    selected = list(candidates.items())[: max(0, int(config.boards.yc_max_details))]
    logger.info(
        "YC: fetching details for %d of %d candidates", len(selected), len(candidates)
    )

    session = _session()
    jobs: list[Job] = []
    try:
        for index, (raw_id, (item, term)) in enumerate(selected, start=1):
            detail = _yc_detail(session, raw_id, config, logger)
            job = _yc_job(item, detail, term)
            if job.title and job.job_url:
                jobs.append(job)
            if index < len(selected):
                time.sleep(max(0.0, float(config.boards.detail_delay_seconds)))
    finally:
        session.close()

    logger.info("YC: normalized %d jobs", len(jobs))
    return jobs


# --------------------------------------------------------------------------- #
# Wellfound
# --------------------------------------------------------------------------- #
def _wellfound_slug(role: str) -> str:
    text = role.strip().lower()
    if text.startswith("http"):
        return text.rstrip("/").rsplit("/", 1)[-1]
    return _WELLFOUND_ROLE_ALIASES.get(text, text.replace(" ", "-"))


def _wellfound_location(job: dict[str, Any]) -> str:
    names = [safe_str(name) for name in (job.get("locationNames") or []) if safe_str(name)]
    accepted = [
        safe_str(name)
        for name in (job.get("acceptedRemoteLocationNames") or [])
        if safe_str(name)
    ]
    location = ", ".join(names)
    if job.get("remote"):
        if accepted:
            remote_note = "Remote - " + ", ".join(accepted)
        else:
            remote_note = "Remote"
        location = f"{location} ({remote_note})" if location else remote_note
    return location


def _wellfound_experience(job: dict[str, Any]) -> str | None:
    minimum = job.get("yearsExperienceMin")
    maximum = job.get("yearsExperienceMax")
    try:
        low = int(minimum) if minimum is not None else None
    except (TypeError, ValueError):
        low = None
    try:
        high = int(maximum) if maximum is not None else None
    except (TypeError, ValueError):
        high = None
    if low is not None and high is not None:
        return f"{low}-{high} years"
    if low is not None:
        return f"{low}+ years"
    return None


def _wellfound_page(
    session: http.Session,
    role_slug: str,
    page: int,
    config: AppConfig,
    logger: logging.Logger,
) -> tuple[list[Job], int]:
    url = f"{WELLFOUND_BASE}/role/r/{role_slug}"
    if page > 1:
        url = f"{url}?page={page}"

    html = _get(session, url, config, logger)
    if not html:
        return [], 0

    soup = BeautifulSoup(html, "html.parser")
    holder = soup.find("script", id="__NEXT_DATA__")
    if holder is None or not holder.string:
        logger.warning("Wellfound page has no __NEXT_DATA__: %s", url)
        return [], 0

    try:
        payload = json.loads(holder.string)
    except ValueError:
        logger.warning("Wellfound returned invalid JSON: %s", url)
        return [], 0

    page_props = payload.get("props", {}).get("pageProps", {})
    cache = page_props.get("apolloState", {}).get("data", {})
    if not isinstance(cache, dict):
        return [], 0

    startups: dict[str, dict[str, Any]] = {}
    company_by_job: dict[str, str] = {}
    for key, value in cache.items():
        if not key.startswith("StartupResult:") or not isinstance(value, dict):
            continue
        startups[key] = value
        company_name = safe_str(value.get("name")) or "Wellfound startup"
        for ref in value.get("highlightedJobListings") or []:
            if isinstance(ref, dict) and ref.get("__ref"):
                company_by_job[str(ref["__ref"]).split(":", 1)[-1]] = company_name

    jobs: list[Job] = []
    page_count = 0
    for key, value in cache.items():
        if not key.startswith("JobListingSearchResult:") or not isinstance(value, dict):
            continue
        raw_id = safe_str(value.get("id")) or key.split(":", 1)[-1]
        title = safe_str(value.get("title"))
        if not title:
            continue
        slug = safe_str(value.get("slug"))
        job_url = f"{WELLFOUND_BASE}/jobs/{raw_id}"
        if slug:
            job_url = f"{job_url}-{slug}"
        location = _wellfound_location(value)
        description = safe_str(value.get("description"))
        compensation = safe_str(value.get("compensation"))
        if compensation:
            description = f"Compensation: {compensation}\n{description}"

        jobs.append(
            Job(
                id=job_id_for("wellfound", raw_id, job_url),
                title=title,
                company=company_by_job.get(raw_id, "Wellfound startup"),
                location=location,
                is_remote=bool(value.get("remote"))
                or "remote" in location.lower(),
                job_url=normalize_url(job_url),
                description=description,
                is_easy_apply=False,
                site="wellfound",
                job_type=safe_str(value.get("jobType")) or None,
                experience_range=_wellfound_experience(value),
                skills=safe_str(value.get("primaryRoleTitle")),
                search_query=role_slug,
            )
        )

    # The page-count field lives inside the SEO search result object.
    root = cache.get("ROOT_QUERY")
    if isinstance(root, dict):
        talent = root.get("talent")
        if isinstance(talent, dict):
            for key, value in talent.items():
                if key.startswith("seoLanding") and isinstance(value, dict):
                    try:
                        page_count = max(page_count, int(value.get("pageCount") or 0))
                    except (TypeError, ValueError):
                        pass
    return jobs, page_count


# --------------------------------------------------------------------------- #
# Foundit (free-to-candidate portal with many staffing-consultancy listings)
# --------------------------------------------------------------------------- #
def _foundit_experience(row: dict[str, Any]) -> str | None:
    def years(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("years")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return str(int(number)) if number.is_integer() else f"{number:.1f}"

    low = years(row.get("minimumExperience"))
    high = years(row.get("maximumExperience"))
    if low and high and low != high:
        return f"{low}-{high} years"
    if low:
        return f"{low}+ years"
    return None


def _foundit_location(value: str) -> str:
    text = " ".join(value.split())
    if text.lower().startswith("india"):
        text = text[5:].lstrip(", ").strip()
    if not text:
        return "India"
    if text.lower().endswith("india"):
        return text
    return f"{text}, India"


def _foundit_job(row: dict[str, Any], term: str) -> Job | None:
    raw_id = safe_str(row.get("jobId")) or safe_str(row.get("id"))
    path = safe_str(row.get("jdUrl")) or safe_str(row.get("seoJdUrl"))
    title = safe_str(row.get("title"))
    if not raw_id or not path or not title:
        return None

    url = path if path.startswith("http") else f"https://www.foundit.in{path}"
    location = _foundit_location(safe_str(row.get("locations")))
    company = safe_str(row.get("companyName")) or "Foundit company"
    skills = safe_str(row.get("skills"))
    experience = _foundit_experience(row)

    description_parts = []
    if experience:
        description_parts.append(f"Experience: {experience}")
    if skills:
        description_parts.append(f"Skills: {skills}")
    employment = row.get("employmentTypes")
    if isinstance(employment, list) and employment:
        description_parts.append(
            "Employment: " + ", ".join(safe_str(item) for item in employment)
        )

    posted = safe_str(row.get("postedBy"))
    created = row.get("createdAt")
    if not posted and isinstance(created, (int, float)):
        try:
            posted = datetime.fromtimestamp(created / 1000).date().isoformat()
        except (OSError, ValueError):
            posted = ""

    return Job(
        id=job_id_for("foundit", raw_id, url),
        title=title,
        company=company,
        location=location,
        is_remote="remote" in f"{title} {location}".lower(),
        job_url=normalize_url(url),
        description="\n".join(description_parts),
        site="foundit",
        experience_range=experience,
        skills=skills,
        date_posted=posted or None,
        search_query=term,
    )


def fetch_foundit_jobs(
    terms: Iterable[str],
    config: AppConfig,
    logger: logging.Logger,
) -> list[Job]:
    """Read Foundit's public search JSON (no login, free for candidates)."""

    session = _session()
    jobs: list[Job] = []
    seen_terms: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in seen_terms}:
            seen_terms.append(cleaned)

    try:
        for term in seen_terms:
            params = urllib.parse.urlencode(
                {
                    "query": term,
                    "locations": "India",
                    "limit": min(60, max(10, int(config.boards.foundit_page_size))),
                    "start": 0,
                }
            )
            payload = _get(
                session,
                f"{FOUNDIT_API}?{params}",
                config,
                logger,
                expect_json=True,
                headers={
                    "Accept": "application/json",
                    "Referer": "https://www.foundit.in/srp/results",
                },
            )
            rows = (
                (payload or {}).get("jobSearchResponse", {}).get("data")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(rows, list):
                logger.warning("Foundit: no rows for %r", term)
                continue
            kept = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                job = _foundit_job(row, term)
                if job is not None:
                    jobs.append(job)
                    kept += 1
            logger.info("Foundit %r: %d jobs", term, kept)
            time.sleep(max(0.0, float(config.boards.page_delay_seconds)))
    finally:
        session.close()

    by_id: dict[str, Job] = {}
    for job in jobs:
        by_id.setdefault(job.id, job)
    result = list(by_id.values())
    logger.info("Foundit: normalized %d jobs", len(result))
    return result


# --------------------------------------------------------------------------- #
# Cutshort (India)
# --------------------------------------------------------------------------- #
def _cutshort_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _cutshort_experience(job: dict[str, Any]) -> str | None:
    exp = job.get("expRange")
    if not isinstance(exp, dict):
        return None
    low = _cutshort_number(exp.get("minVanity") or exp.get("min"))
    high = _cutshort_number(exp.get("maxVanity") or exp.get("max"))
    if low and high and low != high:
        return f"{low}-{high} years"
    if low:
        return f"{low}+ years"
    return None


def _cutshort_location(job: dict[str, Any]) -> tuple[str, bool]:
    names = [safe_str(name) for name in (job.get("locations") or []) if safe_str(name)]
    remote_type = safe_str(job.get("remoteType")).lower()
    is_remote = "remote" in remote_type and remote_type != "remote_not_okay"
    location = ", ".join(names)
    if is_remote:
        location = f"{location} (Remote)" if location else "Remote"
    return location or "India", is_remote


def _cutshort_job(job: dict[str, Any], role_slug: str) -> Job | None:
    raw_id = safe_str(job.get("_id"))
    job_url = safe_str(job.get("publicUrl"))
    title = safe_str(job.get("headline"))
    if not raw_id or not job_url or not title:
        return None

    company_id = job.get("companyId") if isinstance(job.get("companyId"), dict) else {}
    company_details = (
        job.get("companyDetails") if isinstance(job.get("companyDetails"), dict) else {}
    )
    company = safe_str(company_id.get("name")) or safe_str(company_details.get("name"))
    location, is_remote = _cutshort_location(job)

    description = strip_html(safe_str(job.get("sanitizedComment")))
    salary = safe_str(job.get("salaryRangeText"))
    if salary:
        description = f"Salary: {salary}\n{description}"

    skills_value = job.get("allSkills")
    if isinstance(skills_value, (list, tuple, set)):
        skills = ", ".join(safe_str(skill) for skill in skills_value)
    else:
        skills = safe_str(skills_value)

    role_types = job.get("roleTypes")
    job_type = (
        ", ".join(safe_str(item) for item in role_types)
        if isinstance(role_types, list)
        else safe_str(role_types)
    )

    return Job(
        id=job_id_for("cutshort", raw_id, job_url),
        title=title,
        company=company or "Cutshort company",
        location=location,
        is_remote=is_remote,
        job_url=normalize_url(job_url),
        description=description,
        is_easy_apply=False,
        site="cutshort",
        job_type=job_type or None,
        experience_range=_cutshort_experience(job),
        skills=skills,
        search_query=role_slug,
    )


def _cutshort_page(
    session: http.Session,
    role_slug: str,
    page: int,
    config: AppConfig,
    logger: logging.Logger,
) -> list[Job]:
    url = f"{CUTSHORT_BASE}/jobs/{role_slug}"
    if page > 1:
        url = f"{url}?page={page}"

    html = _get(session, url, config, logger)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    holder = soup.find("script", id="__NEXT_DATA__")
    if holder is None or not holder.string:
        logger.warning("Cutshort page has no __NEXT_DATA__: %s", url)
        return []
    try:
        payload = json.loads(holder.string)
    except ValueError:
        return []

    page_props = payload.get("props", {}).get("pageProps", {})
    queries = page_props.get("dehydratedState", {}).get("queries", [])
    page_data: dict[str, Any] = {}
    for query in queries:
        key = query.get("queryKey")
        if isinstance(key, list) and key and key[0] == "jobListData":
            data = query.get("state", {}).get("data", {}).get("data", {})
            if isinstance(data, dict):
                page_data = data.get("pageData") or {}
            break
    rows = page_data.get("jobs") if isinstance(page_data, dict) else None
    if not isinstance(rows, list):
        return []

    jobs: list[Job] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job = _cutshort_job(row, role_slug)
        if job is not None:
            jobs.append(job)
    return jobs


def fetch_cutshort_jobs(
    roles: Iterable[str],
    config: AppConfig,
    logger: logging.Logger,
) -> list[Job]:
    """Read Cutshort's role pages (India-focused, no login required)."""

    session = _session()
    jobs: list[Job] = []
    slugs: list[str] = []
    for role in roles:
        slug = role.strip().lower().strip("/")
        if slug.startswith("http"):
            slug = slug.rstrip("/").rsplit("/", 1)[-1]
        if slug and slug not in slugs:
            slugs.append(slug)

    try:
        for slug in slugs:
            for page in range(1, max(1, int(config.boards.cutshort_pages)) + 1):
                page_jobs = _cutshort_page(session, slug, page, config, logger)
                logger.info("Cutshort %s page %d: %d jobs", slug, page, len(page_jobs))
                jobs.extend(page_jobs)
                if not page_jobs or len(jobs) >= config.boards.cutshort_max_jobs:
                    break
                time.sleep(max(0.0, float(config.boards.page_delay_seconds)))
            if len(jobs) >= config.boards.cutshort_max_jobs:
                logger.info(
                    "Cutshort: reached max_jobs=%d", config.boards.cutshort_max_jobs
                )
                break
    finally:
        session.close()

    by_id: dict[str, Job] = {}
    for job in jobs:
        by_id.setdefault(job.id, job)
    result = list(by_id.values())[: config.boards.cutshort_max_jobs]
    logger.info("Cutshort: normalized %d jobs", len(result))
    return result


def fetch_wellfound_jobs(
    roles: Iterable[str],
    config: AppConfig,
    logger: logging.Logger,
) -> list[Job]:
    session = _session()
    jobs: list[Job] = []
    seen: set[str] = set()
    try:
        for role in roles:
            slug = _wellfound_slug(role)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            max_pages = max(1, int(config.boards.wellfound_pages))
            for page in range(1, max_pages + 1):
                page_jobs, page_count = _wellfound_page(
                    session, slug, page, config, logger
                )
                jobs.extend(page_jobs)
                logger.info(
                    "Wellfound %s page %d: %d jobs (pageCount=%s)",
                    slug,
                    page,
                    len(page_jobs),
                    page_count or "?",
                )
                if not page_jobs or (page_count and page >= page_count):
                    break
                time.sleep(max(0.0, float(config.boards.page_delay_seconds)))
                if len(jobs) >= config.boards.wellfound_max_jobs:
                    break
            if len(jobs) >= config.boards.wellfound_max_jobs:
                logger.info(
                    "Wellfound: reached max_jobs=%d", config.boards.wellfound_max_jobs
                )
                break
    finally:
        session.close()

    by_id: dict[str, Job] = {}
    for job in jobs:
        by_id.setdefault(job.id, job)
    result = list(by_id.values())[: config.boards.wellfound_max_jobs]
    logger.info("Wellfound: normalized %d jobs", len(result))
    return result
