"""Database behavior tests using a temporary SQLite file."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from db import Database  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def record(self, job_id: str = "linkedin:1", status: str = "APPLIED_AUTO") -> None:
        self.db.record(
            job_id=job_id,
            title="Backend Engineer",
            company="Example",
            job_url="https://example.com/jobs/1",
            status=status,
            notes="score=90",
        )

    def test_dedupe_and_stats(self) -> None:
        self.assertFalse(self.db.exists("linkedin:1"))
        self.record()
        self.assertTrue(self.db.exists("linkedin:1"))
        self.assertEqual(self.db.count_applied_today(), 1)
        stats = self.db.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["applied_auto"], 1)

    def test_update_status_creates_event_history(self) -> None:
        self.record()
        changed = self.db.update_status(
            "linkedin:1", "EMAIL_SENT_EXTERNAL", "manual fallback"
        )
        self.assertTrue(changed)
        row = self.db.get("linkedin:1")
        assert row is not None
        self.assertEqual(row["status"], "EMAIL_SENT_EXTERNAL")
        self.assertIn("manual fallback", row["notes"])
        self.assertEqual(len(self.db.events("linkedin:1")), 2)

    def test_unseen_ids(self) -> None:
        self.record("linkedin:1")
        unseen = self.db.unseen_ids(["linkedin:1", "linkedin:2"])
        self.assertEqual(unseen, {"linkedin:2"})

    def test_mark_seen_dedupes_digest_jobs(self) -> None:
        self.db.mark_seen(
            "wellfound:1",
            "Data Engineer",
            "Example",
            "https://wellfound.com/jobs/1",
            site="wellfound",
        )
        self.assertEqual(self.db.known_ids(["wellfound:1", "wellfound:2"]), {"wellfound:1"})
        self.assertEqual(self.db.unseen_ids(["wellfound:1", "wellfound:2"]), {"wellfound:2"})
        self.assertEqual(self.db.stats()["seen_jobs"], 1)

    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.record(status="MAYBE")

    def test_recent_returns_latest(self) -> None:
        self.record("linkedin:1")
        self.record("linkedin:2", status="EMAIL_SENT_EXTERNAL")
        rows = self.db.recent(10)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
