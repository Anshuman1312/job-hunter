"""Unit tests for the strict matching rules (no network access)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import AppConfig  # noqa: E402
from matcher import JobMatcher  # noqa: E402
from models import Job  # noqa: E402


def make_job(**overrides: object) -> Job:
    data = {
        "id": "test:1",
        "title": "Backend Engineer",
        "company": "Example",
        "location": "Bengaluru, India",
        "is_remote": False,
        "job_url": "https://www.linkedin.com/jobs/view/1",
        "description": (
            "3-5 years of experience building Python services with Django, "
            "PostgreSQL and AWS."
        ),
        "is_easy_apply": True,
        "site": "linkedin",
    }
    data.update(overrides)
    return Job(**data)


class MatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = AppConfig.load(PROJECT_ROOT / "config.json")
        cls.matcher = JobMatcher(config.filters)

    def test_accepts_backend_django_job(self) -> None:
        result = self.matcher.match(make_job())
        self.assertTrue(result.accepted, result.summary)
        self.assertGreaterEqual(result.score, 60)
        self.assertEqual((result.required_min_years, result.required_max_years), (3, 5))

    def test_rejects_six_plus_years(self) -> None:
        job = make_job(description="7-10 years of experience in Python, Django and AWS.")
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("requires at least", result.summary)

    def test_rejects_internship(self) -> None:
        job = make_job(title="Python Developer Intern", job_level="Internship")
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("entry-level", result.summary)

    def test_rejects_entry_level_range(self) -> None:
        job = make_job(
            title="Python Developer",
            description="0-2 years of experience with Python and Django.",
        )
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)

    def test_accepts_remote_worldwide(self) -> None:
        job = make_job(location="Remote - Worldwide", is_remote=True)
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)

    def test_rejects_non_allowed_onsite_location(self) -> None:
        job = make_job(location="New York, NY, United States", is_remote=False)
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("location", result.summary)

    def test_rejects_frontend_title(self) -> None:
        job = make_job(title="Frontend React Developer")
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)

    def test_rejects_missing_framework(self) -> None:
        job = make_job(
            description="3-5 years of experience with Python and strong communication."
        )
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("framework", result.summary)

    def test_accepts_two_to_three_year_range(self) -> None:
        job = make_job(description="2-3 years of experience with Python, Django and AWS.")
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)
        self.assertEqual((result.required_min_years, result.required_max_years), (2, 3))

    def test_accepts_three_and_half_years(self) -> None:
        job = make_job(
            description="3.5+ years of experience with Python, Django and AWS."
        )
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)

    def test_rejects_four_plus_years(self) -> None:
        job = make_job(description="4+ years of experience with Python, Django and AWS.")
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("requires at least", result.summary)

    def test_accepts_data_engineer_job(self) -> None:
        job = make_job(
            title="Data Engineer",
            location="Remote - Worldwide",
            is_remote=True,
            description=(
                "3+ years of experience building data pipelines with Python, SQL, "
                "Airflow, dbt and Snowflake."
            ),
        )
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)
        self.assertIn("data engineering", result.summary)

    def test_rejects_us_only_remote_role(self) -> None:
        job = make_job(location="Remote (US)", is_remote=True)
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("restricted", result.summary)

    def test_rejects_foreign_city_remote_role(self) -> None:
        job = make_job(location="Berlin (Remote)", is_remote=True)
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("remote role", result.summary)

    def test_accepts_india_remote_role(self) -> None:
        job = make_job(location="Pune (Remote - India)", is_remote=True)
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)

    def test_rejects_senior_without_years(self) -> None:
        job = make_job(
            title="Senior Python Engineer",
            description="Build Python APIs with Django and PostgreSQL.",
        )
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)

    def test_accepts_senior_with_explicit_3_years(self) -> None:
        job = make_job(
            title="Senior Python Engineer",
            description="3+ years of experience with Python, FastAPI and AWS.",
        )
        result = self.matcher.match(job)
        self.assertTrue(result.accepted, result.summary)

    def test_extract_yoe_ignores_preferred_high_range(self) -> None:
        minimum, maximum, _ = self.matcher._extract_yoe(
            "8+ years preferred. 3-5 years of experience required with Python and Django."
        )
        self.assertEqual((minimum, maximum), (3, 5))

    def test_extract_yoe_hard_six_plus(self) -> None:
        minimum, maximum, _ = self.matcher._extract_yoe(
            "Minimum 6 years of experience with Python required."
        )
        self.assertEqual(minimum, 6)
        self.assertIsNone(maximum)

    def test_hard_requirement_wins_over_lower_range(self) -> None:
        job = make_job(
            description=(
                "3-5 years of experience with Python and Django preferred, "
                "but 7+ years of experience is required for this role."
            )
        )
        result = self.matcher.match(job)
        self.assertFalse(result.accepted)
        self.assertIn("requires at least", result.summary)


if __name__ == "__main__":
    unittest.main()
