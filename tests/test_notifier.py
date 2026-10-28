"""Notifier tests run fully offline in dry-run mode (no SMTP, no real CV)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import AppConfig  # noqa: E402
from models import Job, MatchResult  # noqa: E402
from notifier import Notifier  # noqa: E402


def sample_job() -> Job:
    return Job(
        id="test:email",
        title="Backend Engineer",
        company="Example Cloud",
        location="Remote - Worldwide",
        is_remote=True,
        job_url="https://www.linkedin.com/jobs/view/42",
        description="Python Django FastAPI",
        is_easy_apply=False,
        site="linkedin",
    )


class NotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.config = AppConfig.load(PROJECT_ROOT / "config.json")
        self.config.email.dry_run = True
        self.config.email.outbox_dir = self.temp_path

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manual_email_is_written_without_attachment_by_default(self) -> None:
        self.config.email.attach_resume_on_fallback = False
        notifier = Notifier(self.config)
        self.assertTrue(notifier.send_manual_apply(sample_job(), "External link"))
        messages = list(self.temp_path.glob("*.eml"))
        self.assertEqual(len(messages), 1)
        data = messages[0].read_bytes()
        self.assertIn(b"Manual application required", data)
        self.assertNotIn(b"application/pdf", data)

    def test_attachment_uses_configured_pdf(self) -> None:
        dummy_pdf = self.temp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4\n% test fixture\n")
        self.config.application.resume_path = dummy_pdf
        self.config.email.attach_resume_on_fallback = True
        notifier = Notifier(self.config)
        self.assertTrue(notifier.send_manual_apply(sample_job(), "Test"))
        data = next(self.temp_path.glob("*.eml")).read_bytes()
        self.assertIn(b"application/pdf", data)
        self.assertIn(b"Anshuman_Pandey_Resume.pdf", data)

    def test_job_digest_includes_every_apply_link(self) -> None:
        first = sample_job()
        second = sample_job()
        second.id = "test:email:2"
        second.title = "Data Engineer"
        second.company = "Example Data"
        second.job_url = "https://wellfound.com/jobs/99"
        second.job_url_direct = "https://boards.greenhouse.io/example/jobs/99"
        second.site = "wellfound"

        matches = [
            (first, MatchResult(accepted=True, score=80)),
            (second, MatchResult(accepted=True, score=75)),
        ]
        notifier = Notifier(self.config)
        self.assertTrue(notifier.send_job_digest(matches))

        raw = next(self.temp_path.glob("*.eml")).read_bytes().decode("utf-8", "replace")
        self.assertIn("https://www.linkedin.com/jobs/view/42", raw)
        self.assertIn("https://boards.greenhouse.io/example/jobs/99", raw)
        self.assertIn("https://wellfound.com/jobs/99", raw)
        self.assertIn("ALL APPLY LINKS", raw)
        self.assertIn("Apply link:", raw)


if __name__ == "__main__":
    unittest.main()
