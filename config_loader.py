"""Typed configuration loading with path resolution and validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from utils import (
    PROJECT_ROOT,
    expand_env_placeholders,
    load_env_file,
    resolve_path,
)


class ConfigError(RuntimeError):
    """Raised when config.json is missing or invalid."""


class CandidateConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str
    email: str
    phone: str
    city: str = ""
    state: str = ""
    country: str = "India"
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    search_term: str
    location: str
    is_remote: bool = False
    sites: list[str] = Field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    results_wanted: int = 40
    hours_old: int = 72
    country_indeed: str = "India"
    google_search_term: str | None = None


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_years: int = 3
    max_years: int = 5
    required_skills: list[str] = Field(default_factory=lambda: ["python"])
    required_frameworks: list[str] = Field(default_factory=lambda: ["django", "fastapi"])
    data_role_keywords: list[str] = Field(
        default_factory=lambda: [
            "data engineer",
            "analytics engineer",
            "data platform engineer",
            "data infrastructure engineer",
            "big data engineer",
            "etl developer",
            "data warehouse engineer",
            "database engineer",
        ]
    )
    data_stack_keywords: list[str] = Field(
        default_factory=lambda: [
            "python",
            "sql",
            "spark",
            "pyspark",
            "airflow",
            "dbt",
            "kafka",
            "snowflake",
            "databricks",
            "bigquery",
            "redshift",
            "etl",
            "data pipeline",
            "data warehouse",
            "postgresql",
            "mysql",
            "hadoop",
            "flink",
            "dagster",
            "prefect",
        ]
    )
    remote_blocklist: list[str] = Field(default_factory=list)
    bonus_keywords: list[str] = Field(default_factory=list)
    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    hard_senior_title_keywords: list[str] = Field(
        default_factory=lambda: [
            "lead",
            "principal",
            "staff",
            "architect",
            "head of",
            "director",
            "vice president",
            "vp ",
            "chief",
            "manager",
        ]
    )
    entry_title_keywords: list[str] = Field(
        default_factory=lambda: [
            "intern",
            "internship",
            "trainee",
            "fresher",
            "entry level",
            "entry-level",
            "graduate",
            "campus",
            "apprentice",
        ]
    )
    allowed_countries: list[str] = Field(default_factory=lambda: ["india"])
    allowed_cities: list[str] = Field(default_factory=list)
    allow_unknown_experience: bool = True
    reject_senior_without_yoe: bool = True
    min_score: float = 0

    @field_validator("max_years")
    @classmethod
    def _max_after_min(cls, value: int, info: Any) -> int:
        minimum = info.data.get("min_years", 0)
        if value < minimum:
            raise ValueError("min_years cannot be greater than max_years")
        return value


class ApplicationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resume_path: Path = Path("./assets/resume.pdf")
    browser_profile_dir: Path = Path("./browser_session")
    screenshot_dir: Path = Path("./logs/screenshots")
    headless: bool = False
    browser_channel: str | None = "chrome"
    locale: str = "en-IN"
    timezone_id: str = "Asia/Kolkata"
    auto_apply_enabled: bool = True
    probe_easy_apply: bool = True
    max_daily_auto_applies: int = 15
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    navigation_timeout_ms: int = 45000
    step_timeout_ms: int = 15000
    max_form_steps: int = 12
    login_wait_seconds: int = 300
    unknown_radio_policy: Literal["first_option", "yes", "skip"] = "first_option"
    screening: dict[str, Any] = Field(default_factory=dict)

    @field_validator("max_daily_auto_applies")
    @classmethod
    def _non_negative_cap(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_daily_auto_applies must be >= 0")
        return value

    @field_validator("max_delay_seconds")
    @classmethod
    def _delay_order(cls, value: float, info: Any) -> float:
        minimum = float(info.data.get("min_delay_seconds", 0))
        if value < minimum:
            raise ValueError("max_delay_seconds must be >= min_delay_seconds")
        return value


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_starttls: bool = True
    use_ssl: bool = False
    sender_email: str
    sender_name: str = "Job Hunter"
    recipient_email: str
    password_env: str = "GMAIL_APP_PASSWORD"
    password: str = Field(default="", repr=False)
    timeout_seconds: int = 30
    max_retries: int = 2
    attach_resume_on_fallback: bool = False
    attach_resume_on_success: bool = False
    send_run_summary: bool = True
    send_job_digest: bool = True
    digest_max_jobs: int = 60
    dry_run: bool = False
    outbox_dir: Path = Path("./logs/outbox")
    attachment_filename: str = "Anshuman_Pandey_Resume.pdf"

    @property
    def recipients(self) -> list[str]:
        return [item.strip() for item in self.recipient_email.split(",") if item.strip()]


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: Path = Path("./applications.db")


class WatchConfig(BaseModel):
    """Cadence for `main.py watch` (continuous new-job mode)."""

    model_config = ConfigDict(extra="ignore")

    interval_minutes: int = 45
    jitter_seconds: int = 180
    max_cycles: int = 0
    min_interval_minutes: int = 10

    @field_validator("max_cycles")
    @classmethod
    def _non_negative_cycles(cls, value: int) -> int:
        if value < 0:
            raise ValueError("watch.max_cycles must be >= 0")
        return value


class ScrapingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    linkedin_fetch_description: bool = True
    verbose: int = 0
    proxy: str | None = None
    cache_dir: Path = Path("./data")
    jobspy_enabled: bool = True


class BoardConfig(BaseModel):
    """Limits for the YC and Wellfound public feeds (no login required)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    timeout_seconds: int = 40
    max_retries: int = 2
    page_delay_seconds: float = 0.6
    detail_delay_seconds: float = 0.4
    yc_max_details: int = 60
    wellfound_pages: int = 2
    wellfound_max_jobs: int = 150
    cutshort_pages: int = 2
    cutshort_max_jobs: int = 200
    foundit_page_size: int = 50


class CompanySource(BaseModel):
    """A direct careers-site feed (Workday / SuccessFactors / Capgemini)."""

    model_config = ConfigDict(extra="ignore")

    company: str
    slug: str
    kind: Literal["workday", "successfactors", "capgemini"]
    search_terms: list[str] = Field(default_factory=lambda: ["python"])
    base_url: str = ""
    rss_url: str = ""
    results_wanted: int = 40
    pages: int = 5
    max_details: int = 25
    india_only: bool = True


class ReferralCompany(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    aliases: list[str] = Field(default_factory=list)


class ReferralConfig(BaseModel):
    """Companies where the candidate already has a referral contact."""

    model_config = ConfigDict(extra="ignore")

    companies: list[ReferralCompany] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "INFO"
    file: Path = Path("./logs/job_hunter.log")
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


class AppConfig(BaseModel):
    """Root configuration object used by every module."""

    model_config = ConfigDict(extra="ignore")

    candidate: CandidateConfig
    search: list[SearchQuery]
    filters: FilterConfig = Field(default_factory=FilterConfig)
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    email: EmailConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    boards: BoardConfig = Field(default_factory=BoardConfig)
    company_sources: list[CompanySource] = Field(default_factory=list)
    referrals: ReferralConfig = Field(default_factory=ReferralConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    config_path: Path = Field(default=PROJECT_ROOT / "config.json", exclude=True)
    project_root: Path = Field(default=PROJECT_ROOT, exclude=True)

    @property
    def latest_cache_path(self) -> Path:
        return self.scraping.cache_dir / "latest_jobs.json"

    @property
    def resume_exists(self) -> bool:
        return self.application.resume_path.is_file()

    def referral_for(self, company: str) -> str | None:
        """Return the referral company name if ``company`` is a referral target."""

        text = (company or "").lower()
        if not text:
            return None
        for entry in self.referrals.companies:
            if any(alias.lower() in text for alias in entry.aliases or [entry.name]):
                return entry.name
        return None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        config_path = resolve_path(path or "config.json", PROJECT_ROOT)
        load_env_file(PROJECT_ROOT / ".env")
        load_env_file(config_path.parent / ".env")

        if not config_path.is_file():
            raise ConfigError(
                f"Configuration file not found: {config_path}. "
                "Copy config.json into the project folder or pass --config."
            )

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Could not read {config_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("config.json must contain a JSON object at the top level")

        raw = expand_env_placeholders(raw)
        try:
            config = cls.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"Invalid configuration:\n{exc}") from exc

        config.config_path = config_path
        config.project_root = config_path.parent
        config._resolve_paths(config_path.parent)

        password = config.email.password or os.getenv(config.email.password_env, "")
        password = password.strip()
        if "gmail" in config.email.smtp_host.lower():
            # Gmail displays app passwords in four groups; the spaces are
            # cosmetic and are not part of the credential.
            password = "".join(password.split())
        config.email.password = password
        return config

    def _resolve_paths(self, base: Path) -> None:
        self.database.path = resolve_path(self.database.path, base)
        self.application.resume_path = resolve_path(self.application.resume_path, base)
        self.application.browser_profile_dir = resolve_path(
            self.application.browser_profile_dir, base
        )
        self.application.screenshot_dir = resolve_path(self.application.screenshot_dir, base)
        self.scraping.cache_dir = resolve_path(self.scraping.cache_dir, base)
        self.logging.file = resolve_path(self.logging.file, base)
        self.email.outbox_dir = resolve_path(self.email.outbox_dir, base)
