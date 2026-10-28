"""Pydantic models shared by scraping, matching and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from utils import strip_html


class Job(BaseModel):
    """A normalized job listing regardless of source platform."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    title: str
    company: str
    location: str = ""
    is_remote: bool = False
    job_url: str
    description: str = ""
    is_easy_apply: bool = False
    site: str = ""
    job_url_direct: str | None = None
    date_posted: str | None = None
    job_level: str | None = None
    job_type: str | None = None
    listing_type: str | None = None
    experience_range: str | None = None
    skills: str = ""
    work_from_home_type: str | None = None
    search_query: str = ""

    @property
    def plain_description(self) -> str:
        return strip_html(self.description)

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{self.skills}\n{self.plain_description}"

    @property
    def display_location(self) -> str:
        if self.is_remote and "remote" not in self.location.lower():
            return f"{self.location} (Remote)".strip()
        return self.location or "Not specified"


class MatchResult(BaseModel):
    """Outcome of the strict relevance filter, with an audit trail."""

    accepted: bool
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    rejections: list[str] = Field(default_factory=list)
    required_min_years: int | None = None
    required_max_years: int | None = None
    yoe_known: bool = False

    @property
    def summary(self) -> str:
        if self.accepted:
            return "; ".join(self.reasons)
        return "; ".join(self.rejections)


@dataclass
class ApplyResult:
    """Outcome of one Playwright auto-application attempt."""

    status: Literal["APPLIED_AUTO", "EXTERNAL", "FAILED", "SKIPPED"]
    message: str
    code: str = ""
    detail: str = ""
    screenshot: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "APPLIED_AUTO"
