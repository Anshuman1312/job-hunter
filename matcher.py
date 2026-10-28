"""Strict job relevance filtering: role, 3-5 years, stack and location."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config_loader import FilterConfig
from models import Job, MatchResult
from utils import strip_html

_REMOTE_WORDS = (
    "remote",
    "anywhere",
    "worldwide",
    "work from home",
    "work-from-home",
    "wfh",
    "distributed",
    "telecommute",
    "global",
)

_SENIOR_RE = re.compile(r"(?<![a-z0-9])(senior|sr\.?)(?![a-z0-9])", re.IGNORECASE)

_REMOTE_PAREN_RE = re.compile(r"\(\s*remote[^)]*\)", re.IGNORECASE)
_REMOTE_PREFIX_RE = re.compile(r"^remote\s*[-–—]\s*", re.IGNORECASE)
_REMOTE_SCOPE_RE = re.compile(r"remote\s*\(([^)]*)\)", re.IGNORECASE)
_REMOTE_SCOPE_OK = (
    "remote",
    "worldwide",
    "global",
    "anywhere",
    "india",
    "asia",
)

_YOE_RANGE_RE = re.compile(
    r"(?<![a-z0-9])(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?![a-z0-9])",
    re.IGNORECASE,
)
_YOE_PLUS_RE = re.compile(
    r"(?<![a-z0-9])(\d{1,2})\s*\+\s*(?:years?|yrs?)(?![a-z0-9])",
    re.IGNORECASE,
)
_YOE_MIN_RE = re.compile(
    r"(?:minimum(?:\s+of)?|min\.?|at\s+least|atleast|more\s+than|over)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_YOE_EXPERIENCE_RE = re.compile(
    r"(?<![a-z0-9])(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[a-z/&,+-]+){0,4}?\s+(?:experience|exp)(?![a-z0-9])",
    re.IGNORECASE,
)

_CONTEXT_STRONG = (
    "required",
    "requirement",
    "qualification",
    "must have",
    "you have",
    "minimum",
)
_CONTEXT_OPTIONAL = (
    "preferred",
    "nice to have",
    "is a plus",
    "a plus",
    "bonus",
    "desired",
    "good to have",
    "advantage",
)


@dataclass(frozen=True)
class _YoECandidate:
    low: int
    high: int | None
    snippet: str
    optional: bool


def _keyword_hit(text: str, keywords: list[str]) -> bool:
    for keyword in keywords:
        word = keyword.strip().lower()
        if not word:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _keyword_in(text: str, keywords: list[str]) -> bool:
    return any(keyword.strip().lower() in text for keyword in keywords if keyword.strip())


class JobMatcher:
    """Apply the candidate's hard requirements and produce an explainable score."""

    def __init__(self, config: FilterConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def match(self, job: Job) -> MatchResult:
        title = (job.title or "").lower()
        description = strip_html(job.description)
        description_lower = description.lower()
        location = (job.location or "").lower()
        level = (job.job_level or "").lower()

        result = MatchResult(accepted=False, score=0.0)

        # --- Role / seniority -----------------------------------------
        if _keyword_hit(title, self.config.entry_title_keywords) or "internship" in level:
            result.rejections.append("entry-level/internship title")
            return result
        if level in {"entry level", "internship"}:
            result.rejections.append(f"job level is {level!r}")
            return result
        if _keyword_hit(title, self.config.hard_senior_title_keywords):
            result.rejections.append("senior leadership title (lead/principal/architect/manager)")
            return result
        if _keyword_hit(title, self.config.title_exclude):
            result.rejections.append("title is outside the backend/Python target role")
            return result
        if self.config.title_include and not _keyword_in(title, self.config.title_include):
            result.rejections.append("title does not look like a backend/Python role")
            return result

        # --- Experience (3-5 years) -----------------------------------
        min_years = None
        max_years = None
        yoe_known = False
        yoe_snippets: list[str] = []
        experience_source = "\n".join(
            part for part in (job.experience_range or "", description) if part
        )
        if experience_source:
            min_years, max_years, yoe_snippets = self._extract_yoe(experience_source)
            yoe_known = bool(yoe_snippets)

        result.required_min_years = min_years
        result.required_max_years = max_years
        result.yoe_known = yoe_known

        if yoe_known:
            if min_years is not None and min_years > self.config.max_years:
                result.rejections.append(
                    f"requires at least {min_years} years (target {self.config.min_years}-{self.config.max_years})"
                )
                return result
            if max_years is not None and max_years < self.config.min_years:
                result.rejections.append(
                    f"requires at most {max_years} years (too junior for {self.config.min_years}+)"
                )
                return result
            # 0-2 / 1-2 year roles are below the candidate's 2.7 years, but a
            # 2-3 range is a good fit and must not be rejected as "junior".
            if (
                max_years is not None
                and max_years <= self.config.min_years
                and (min_years or 0) <= self.config.min_years - 1
            ):
                result.rejections.append(
                    f"junior experience range ({min_years}-{max_years} years)"
                )
                return result
            result.reasons.append(
                "experience: "
                + (
                    yoe_snippets[0]
                    if yoe_snippets
                    else f"matches {self.config.min_years}-{self.config.max_years} band"
                )
            )
        else:
            if _SENIOR_RE.search(title) and self.config.reject_senior_without_yoe:
                result.rejections.append(
                    f"senior title without a stated {self.config.min_years}-"
                    f"{self.config.max_years} year range"
                )
                return result
            if not self.config.allow_unknown_experience:
                result.rejections.append("no years-of-experience signal in the description")
                return result
            result.reasons.append("experience: no explicit years found; accepted by policy")

        # --- Core stack ------------------------------------------------
        haystack = f"{title}\n{job.skills}\n{description_lower}"
        is_data_role = _keyword_in(title, self.config.data_role_keywords)

        if is_data_role:
            data_hits = [
                keyword
                for keyword in self.config.data_stack_keywords
                if _keyword_hit(haystack, [keyword])
            ]
            core_data_hit = _keyword_hit(haystack, ["python"]) or _keyword_hit(
                haystack, ["sql"]
            )
            if not core_data_hit or not data_hits:
                result.rejections.append(
                    "data role without a Python/SQL or data-stack signal"
                )
                return result
            result.reasons.append(
                "role: data engineering; stack: " + " + ".join(data_hits[:5])
            )
            frameworks_found = data_hits
        else:
            missing_skills = [
                skill
                for skill in self.config.required_skills
                if not _keyword_hit(haystack, [skill])
            ]
            if missing_skills:
                result.rejections.append(
                    "missing core skill(s): " + ", ".join(missing_skills)
                )
                return result

            frameworks_found = [
                framework
                for framework in self.config.required_frameworks
                if _keyword_hit(haystack, [framework])
            ]
            if not frameworks_found:
                result.rejections.append(
                    "missing framework: none of "
                    + ", ".join(self.config.required_frameworks)
                )
                return result
            result.reasons.append("stack: " + " + ".join(frameworks_found[:5]))

        # --- Location --------------------------------------------------
        remote = bool(job.is_remote) or any(word in location for word in _REMOTE_WORDS)
        if remote:
            location_blob = f"{location} {(job.work_from_home_type or '').lower()}"
            blocked_region = next(
                (
                    term
                    for term in self.config.remote_blocklist
                    if term in location_blob
                ),
                None,
            )
            global_hint = any(
                hint in location_blob
                for hint in ("india", "asia", "worldwide", "global", "anywhere")
            )
            if blocked_region and not global_hint:
                result.rejections.append(
                    f"remote role restricted to {blocked_region!r}"
                )
                return result
            if not self._remote_location_is_eligible(location, global_hint):
                result.rejections.append(
                    f"remote role tied to a non-India location: {job.location}"
                )
                return result
            result.reasons.append("location: remote")
        elif any(
            _keyword_hit(location, [country]) for country in self.config.allowed_countries
        ) or _keyword_in(location, self.config.allowed_cities):
            result.reasons.append(f"location: {job.location or 'India'}")
        else:
            if not location:
                result.rejections.append("location is missing")
            else:
                result.rejections.append(f"location not allowed: {job.location}")
            return result

        # --- Score -----------------------------------------------------
        score = 35.0
        score += 10  # core skill present
        score += min(24, len(frameworks_found) * (6 if is_data_role else 12))
        bonus_hits = [
            keyword
            for keyword in self.config.bonus_keywords
            if _keyword_hit(haystack, [keyword])
        ]
        score += min(15, len(bonus_hits) * 3)
        if _keyword_in(
            title,
            [
                "python",
                "django",
                "fastapi",
                "backend",
                "back end",
                "back-end",
                "data engineer",
                "analytics engineer",
            ],
        ):
            score += 10
        if is_data_role:
            score += 5
        if yoe_known:
            score += 5
        if remote:
            score += 2
        result.score = round(min(100.0, score), 1)

        if bonus_hits:
            result.reasons.append("bonus: " + ", ".join(bonus_hits[:5]))
        result.reasons.append(f"score: {result.score}")

        if result.score < self.config.min_score:
            result.rejections.append(
                f"score {result.score} below minimum {self.config.min_score}"
            )
            return result

        result.accepted = True
        return result

    def _remote_location_is_eligible(self, location: str, global_hint: bool) -> bool:
        """Reject "Remote (US)" style roles while keeping global/India remote.

        A remote job tied to a named foreign city or country is not actionable
        from India without sponsorship, so it is treated as a different job
        market.  Plain "Remote", "Remote - Worldwide" and "Pune (Remote -
        India)" remain eligible.
        """

        text = (location or "").lower()
        if not text:
            return True
        if global_hint or any(city in text for city in self.config.allowed_cities):
            return True

        scope_matches = _REMOTE_SCOPE_RE.findall(text)
        for scope in scope_matches:
            words = [word.strip() for word in re.split(r"[,/;&]| and ", scope) if word.strip()]
            if words and not all(
                any(ok in word for ok in _REMOTE_SCOPE_OK) for word in words
            ):
                return False

        base = _REMOTE_PAREN_RE.sub(" ", text)
        base = _REMOTE_PREFIX_RE.sub("", base).strip(" ,-/")
        base = re.sub(r"\bremote\b", " ", base)
        base = re.sub(r"[^a-z]+", " ", base).strip()
        return not base

    def matches(self, job: Job) -> bool:
        return self.match(job).accepted

    # ------------------------------------------------------------------ #
    # Experience extraction
    # ------------------------------------------------------------------ #
    def _extract_yoe(self, description: str) -> tuple[int | None, int | None, list[str]]:
        text = description.lower().replace("\u2013", "-").replace("\u2014", "-")
        # "3.5+ years" must not be read as "5+ years": floor the decimal part
        # so a 3.5-year requirement still falls inside a 2-3.5 target.
        text = re.sub(
            r"(?<![a-z0-9])(\d{1,2})\.\d+\s*([-–]|to)\s*(\d{1,2})\.\d+\s*(years?|yrs?)",
            r"\1-\3 \4",
            text,
        )
        text = re.sub(
            r"(?<![a-z0-9])(\d{1,2})\.\d+\s*(\+)?\s*(years?|yrs?)",
            r"\1\2 \3",
            text,
        )
        candidates: list[_YoECandidate] = []
        seen_spans: set[tuple[int, int]] = set()

        def add(match: re.Match[str], low: int, high: int | None) -> None:
            span = (match.start(), match.end())
            if span in seen_spans:
                return
            # Skip a nested match such as the "5 years of experience" part of
            # "3-5 years of experience"; the outer range is the real signal.
            for existing_start, existing_end in seen_spans:
                if existing_start <= match.start() < existing_end:
                    return
                if existing_start < match.end() <= existing_end:
                    return
            seen_spans.add(span)
            snippet = match.group(0).strip()
            after = text[match.end() : match.end() + 110]
            before = text[max(0, match.start() - 55) : match.start()]

            def qualifier(haystack: str, reverse: bool = False) -> str:
                hits: list[tuple[int, str]] = []
                for word in _CONTEXT_OPTIONAL:
                    position = haystack.rfind(word) if reverse else haystack.find(word)
                    if position >= 0:
                        hits.append((len(haystack) - position if reverse else position, "optional"))
                for word in _CONTEXT_STRONG:
                    position = haystack.rfind(word) if reverse else haystack.find(word)
                    if position >= 0:
                        hits.append((len(haystack) - position if reverse else position, "strong"))
                if not hits:
                    return ""
                return min(hits, key=lambda item: item[0])[1]

            # Qualifiers usually follow the phrase ("3-5 years required",
            # "8+ years preferred"), so inspect that side first. This avoids
            # letting "preferred" in a previous sentence poison a real range.
            kind = qualifier(after) or qualifier(before, reverse=True)
            if kind == "optional":
                candidates.append(_YoECandidate(low, high, snippet, True))
                return
            strong = kind == "strong"
            if strong or len(candidates) < 25:
                candidates.append(_YoECandidate(low, high, snippet, False))

        for match in _YOE_RANGE_RE.finditer(text):
            low, high = int(match.group(1)), int(match.group(2))
            if 0 <= low <= high <= 40:
                add(match, low, high)

        for match in _YOE_PLUS_RE.finditer(text):
            low = int(match.group(1))
            if 0 <= low <= 40:
                add(match, low, None)

        for match in _YOE_MIN_RE.finditer(text):
            low = int(match.group(1))
            if 0 <= low <= 40:
                add(match, low, None)

        for match in _YOE_EXPERIENCE_RE.finditer(text):
            low = int(match.group(1))
            if 0 <= low <= 40:
                add(match, low, None)

        required = [candidate for candidate in candidates if not candidate.optional]
        if not required:
            return None, None, []

        # A hard "6+ years required" statement disqualifies the role even if
        # another line mentions a lower range.
        hard = [candidate for candidate in required if candidate.low >= self.config.max_years + 1]
        if hard:
            hardest = max(hard, key=lambda candidate: candidate.low)
            return (
                hardest.low,
                None,
                [candidate.snippet for candidate in required[:5]],
            )

        min_years = min(candidate.low for candidate in required)
        highs = [candidate.high for candidate in required if candidate.high is not None]
        max_years = None if any(candidate.high is None for candidate in required) else max(highs, default=None)
        snippets = [candidate.snippet for candidate in required[:5]]
        return min_years, max_years, snippets
