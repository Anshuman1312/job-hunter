"""Direct careers-site feeds for companies where the candidate has a referral.

Three public back-ends are supported:

* Workday          - JSON search + detail API (Accenture).
* SuccessFactors   - the public RSS feed (Wipro, HCL).
* Capgemini        - the ``cg-jobstream`` search API used by its own site.

Everything else (Infosys, TCS, Persistent, Siemens, Amdocs, Barclays) is
covered through company-scoped JobSpy queries in ``config.json`` because those
career sites require an authenticated session or render only in a browser.

As with ``boards.py`` these helpers only fetch and normalise; filtering stays in
``matcher.py``.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from curl_cffi import requests as http

from config_loader import AppConfig, CompanySource
from models import Job
from utils import job_id_for, normalize_url, safe_str, strip_html

CAPGEMINI_API = "https://cg-jobstream-api.azurewebsites.net/api/job-search"
WORKDAY_PAGE_SIZE = 20

_LOCATION_SUFFIX = re.compile(r"\s*\(([^()]*)\)\s*$")


def _session() -> http.Session:
    return http.Session(impersonate="chrome")


def _request(
    session: http.Session,
    method: str,
    url: str,
    config: AppConfig,
    logger: logging.Logger,
    payload: dict[str, Any] | None = None,
) -> Any | None:
    attempts = max(1, int(config.boards.max_retries) + 1)
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method,
                url,
                json=payload,
                timeout=max(10, int(config.boards.timeout_seconds)),
                headers={
                    "Accept": (
                        "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7"
                        if "/services/rss/" in url
                        else "application/json, text/html;q=0.9"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if response.status_code != 200:
                logger.warning(
                    "Company feed %s returned HTTP %s (attempt %d/%d)",
                    url,
                    response.status_code,
                    attempt,
                    attempts,
                )
            elif "json" in response.headers.get("content-type", ""):
                return response.json()
            else:
                return response.text
        except Exception as exc:  # network/JSON errors
            logger.warning(
                "Company feed failed %s: %s: %s (attempt %d/%d)",
                url,
                type(exc).__name__,
                exc,
                attempt,
                attempts,
            )
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    return None


def _is_india(text: str, config: AppConfig) -> bool:
    value = (text or "").lower()
    if "india" in value:
        return True
    if any(city in value for city in config.filters.allowed_cities):
        return True
    # Cities that appear on careers sites but are not always in the allow-list.
    return any(
        city in value
        for city in (
            "bengaluru",
            "bangalore",
            "hyderabad",
            "pune",
            "chennai",
            "mumbai",
            "gurugram",
            "gurgaon",
            "noida",
            "kolkata",
            "ahmedabad",
            "coimbatore",
            "trivandrum",
            "thiruvananthapuram",
            "kochi",
            "bhubaneswar",
            "mysore",
            "mysuru",
            "nagpur",
            "indore",
            "jaipur",
            "chandigarh",
            "vizag",
            "visakhapatnam",
        )
    )


def _experience_from_text(text: str) -> str | None:
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"{_trim_number(match.group(1))}-{_trim_number(match.group(2))} years"
    match = re.search(r"(\d+(?:\.\d+)?)\s*\+\s*(?:years|yrs)", text, re.IGNORECASE)
    if match:
        return f"{_trim_number(match.group(1))}+ years"
    return None


def _trim_number(value: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|architect|manager|head)\b", re.IGNORECASE
)


def _guard_experience(title: str, experience: str | None) -> str | None:
    """Treat senior titles without a stated band as 5+ so they are rejected.

    Company career sites frequently omit the years of experience.  A senior
    title is a strong signal for 5+ years, and the candidate explicitly does
    not want those roles.
    """

    if experience:
        return experience
    if _SENIOR_TITLE.search(title or ""):
        return "5+ years"
    return None


# --------------------------------------------------------------------------- #
# Workday (Accenture)
# --------------------------------------------------------------------------- #
def _workday_jobs(
    source: CompanySource,
    config: AppConfig,
    logger: logging.Logger,
    session: http.Session,
) -> list[Job]:
    base = source.base_url.rstrip("/")
    jobs: list[Job] = []
    seen: set[str] = set()
    details_fetched = 0

    for term in source.search_terms:
        for page in range(max(1, int(source.pages))):
            payload = {
                "appliedFacets": {},
                "limit": WORKDAY_PAGE_SIZE,
                "offset": page * WORKDAY_PAGE_SIZE,
                "searchText": term,
            }
            data = _request(session, "POST", f"{base}/jobs", config, logger, payload)
            postings = (data or {}).get("jobPostings") or []
            if not postings:
                break
            for posting in postings:
                if not isinstance(posting, dict):
                    continue
                path = safe_str(posting.get("externalPath"))
                if not path or path in seen:
                    continue
                if source.india_only and not _is_india(path, config):
                    continue
                seen.add(path)

                info: dict[str, Any] = {}
                if details_fetched < max(0, int(source.max_details)):
                    detail = _request(session, "GET", f"{base}{path}", config, logger)
                    if isinstance(detail, dict):
                        info = detail.get("jobPostingInfo") or {}
                        details_fetched += 1
                    time.sleep(max(0.0, float(config.boards.detail_delay_seconds)))

                title = safe_str(info.get("title")) or safe_str(posting.get("title"))
                if not title:
                    continue
                location = safe_str(info.get("location")) or safe_str(
                    posting.get("locationsText")
                )
                description = strip_html(safe_str(info.get("jobDescription")))
                url = safe_str(info.get("externalUrl")) or f"{base}{path}"

                jobs.append(
                    Job(
                        id=job_id_for(source.slug, safe_str(info.get("jobReqId")), url),
                        title=title,
                        company=source.company,
                        location=location or "India",
                        is_remote="remote" in f"{title} {location}".lower(),
                        job_url=normalize_url(url),
                        description=description,
                        site=source.slug,
                        experience_range=_guard_experience(
                            title, _experience_from_text(description)
                        ),
                        date_posted=safe_str(info.get("startDate")) or None,
                        search_query=term,
                    )
                )
            if len(postings) < WORKDAY_PAGE_SIZE:
                break
            time.sleep(max(0.0, float(config.boards.page_delay_seconds)))

    logger.info("Workday %s: %d India job(s)", source.company, len(jobs))
    return jobs


# --------------------------------------------------------------------------- #
# SuccessFactors RSS (Wipro, HCL)
# --------------------------------------------------------------------------- #
def _split_title_location(raw: str) -> tuple[str, str]:
    """Turn ``"Python Developer (Pune, IN, 411014)"`` into title + location."""

    text = " ".join(raw.split())
    match = _LOCATION_SUFFIX.search(text)
    if not match:
        return text, ""
    title = text[: match.start()].strip()
    location = " ".join(match.group(1).split())
    if location.upper() in {"IN", "IND"}:
        location = "India"
    else:
        first = location.split(",")[0].strip()
        if first:
            location = f"{first}, India"
    return title, location


def _successfactors_jobs(
    source: CompanySource,
    config: AppConfig,
    logger: logging.Logger,
    session: http.Session,
) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()

    for term in source.search_terms:
        separator = "&" if "?" in source.rss_url else "?"
        url = f"{source.rss_url}{separator}q={urllib.parse.quote(term)}"
        xml_text = _request(session, "GET", url, config, logger)
        if not isinstance(xml_text, str) or "<item>" not in xml_text:
            logger.warning("SuccessFactors %s: no items for %r", source.company, term)
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("SuccessFactors %s: bad XML for %r: %s", source.company, term, exc)
            continue

        for item in root.iter("item"):
            raw_title = safe_str(item.findtext("title"))
            link = safe_str(item.findtext("link"))
            if not raw_title or not link:
                continue
            link = link.split("?")[0]
            if link in seen:
                continue
            seen.add(link)

            title, location = _split_title_location(raw_title)
            description = strip_html(safe_str(item.findtext("description")))
            remote = "remote" in f"{title} {location}".lower()

            jobs.append(
                Job(
                    id=job_id_for(source.slug, None, link),
                    title=title,
                    company=source.company,
                    location=location or "India",
                    is_remote=remote,
                    job_url=normalize_url(link),
                    description=description,
                    site=source.slug,
                    experience_range=_guard_experience(
                        title, _experience_from_text(description)
                    ),
                    date_posted=safe_str(item.findtext("pubDate")) or None,
                    search_query=term,
                )
            )
        time.sleep(max(0.0, float(config.boards.page_delay_seconds)))

    logger.info("SuccessFactors %s: %d job(s)", source.company, len(jobs))
    return jobs


# --------------------------------------------------------------------------- #
# Capgemini job stream API
# --------------------------------------------------------------------------- #
def _capgemini_jobs(
    source: CompanySource,
    config: AppConfig,
    logger: logging.Logger,
    session: http.Session,
) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()

    for term in source.search_terms:
        params = urllib.parse.urlencode(
            {
                "search": term,
                "page": 1,
                "size": max(1, int(source.results_wanted)),
                "country_code": "in-en",
            }
        )
        data = _request(session, "GET", f"{CAPGEMINI_API}?{params}", config, logger)
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            logger.warning("Capgemini: no rows for %r", term)
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = safe_str(row.get("id"))
            url = safe_str(row.get("apply_job_url"))
            title = safe_str(row.get("title"))
            if not raw_id or not url or not title:
                continue
            location = safe_str(row.get("location"))
            if source.india_only and location and not _is_india(location, config):
                continue
            key = f"{raw_id}:{url}"
            if key in seen:
                continue
            seen.add(key)

            description = strip_html(
                safe_str(row.get("description_stripped")) or safe_str(row.get("description"))
            )
            jobs.append(
                Job(
                    id=job_id_for(source.slug, raw_id, url),
                    title=title,
                    company=source.company,
                    location=location or "India",
                    is_remote="remote" in f"{title} {location}".lower(),
                    job_url=normalize_url(url),
                    description=description,
                    site=source.slug,
                    experience_range=_guard_experience(
                        title, _experience_from_text(description)
                    ),
                    date_posted=safe_str(row.get("updated_at")) or None,
                    search_query=term,
                )
            )
        time.sleep(max(0.0, float(config.boards.page_delay_seconds)))

    logger.info("Capgemini: %d India job(s)", len(jobs))
    return jobs


_FETCHERS = {
    "workday": _workday_jobs,
    "successfactors": _successfactors_jobs,
    "capgemini": _capgemini_jobs,
}


def fetch_company_jobs(config: AppConfig, logger: logging.Logger) -> list[Job]:
    """Fetch every configured referral-company careers feed."""

    if not config.company_sources:
        return []

    session = _session()
    jobs: list[Job] = []
    try:
        for source in config.company_sources:
            fetcher = _FETCHERS.get(source.kind)
            if fetcher is None:
                logger.warning("Unknown company source kind: %s", source.kind)
                continue
            try:
                jobs.extend(fetcher(source, config, logger, session))
            except Exception as exc:
                logger.error(
                    "Company feed %s (%s) failed: %s: %s",
                    source.company,
                    source.kind,
                    type(exc).__name__,
                    exc,
                )
            time.sleep(max(0.0, float(config.boards.page_delay_seconds)))
    finally:
        session.close()

    deduped: dict[str, Job] = {}
    for job in jobs:
        deduped.setdefault(job.id, job)
    logger.info("Company feeds: %d job(s) from %d source(s)", len(deduped), len(config.company_sources))
    return list(deduped.values())
