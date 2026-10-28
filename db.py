"""SQLite persistence: deduplication, status tracking and an audit trail."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

VALID_STATUSES = {"APPLIED_AUTO", "FAILED_AUTO", "EMAIL_SENT_EXTERNAL"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    job_url     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('APPLIED_AUTO', 'FAILED_AUTO', 'EMAIL_SENT_EXTERNAL')),
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_applications_status
    ON applications (status);

CREATE INDEX IF NOT EXISTS idx_applications_applied_at
    ON applications (applied_at);

CREATE TABLE IF NOT EXISTS application_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    status      TEXT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES applications (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_job_id
    ON application_events (job_id);

CREATE INDEX IF NOT EXISTS idx_events_created_at
    ON application_events (created_at);

CREATE TABLE IF NOT EXISTS seen_jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    job_url     TEXT NOT NULL,
    site        TEXT,
    first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    emailed_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_seen_jobs_emailed_at
    ON seen_jobs (emailed_at);
"""


class Database:
    """Thin, thread-safe wrapper around the local applications database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def exists(self, job_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM applications WHERE id = ? LIMIT 1", (job_id,))
            return cursor.fetchone() is not None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM applications WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def known_ids(self, job_ids: Iterable[str]) -> set[str]:
        """Return ids already recorded as an application or an emailed alert."""

        ids = [job_id for job_id in job_ids if job_id]
        found: set[str] = set()
        if not ids:
            return found
        with self._cursor() as cursor:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor.execute(
                    f"SELECT id FROM applications WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
                found.update(row["id"] for row in cursor.fetchall())
                cursor.execute(
                    f"SELECT id FROM seen_jobs WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
                found.update(row["id"] for row in cursor.fetchall())
        return found & set(ids)

    def unseen_ids(self, job_ids: Iterable[str]) -> set[str]:
        """Return the subset of ids that are not stored yet (batched query)."""

        ids = [job_id for job_id in job_ids if job_id]
        return set(ids) - self.known_ids(ids)

    def mark_seen(
        self,
        job_id: str,
        title: str,
        company: str,
        job_url: str,
        site: str = "",
        emailed: bool = True,
    ) -> None:
        """Remember a job so the same alert is not emailed again."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO seen_jobs (id, title, company, job_url, site, emailed_at)
                VALUES (?, ?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                ON CONFLICT(id) DO UPDATE SET
                    title      = excluded.title,
                    company    = excluded.company,
                    job_url    = excluded.job_url,
                    site       = excluded.site,
                    emailed_at = CASE
                        WHEN ? THEN CURRENT_TIMESTAMP
                        ELSE seen_jobs.emailed_at
                    END
                """,
                (
                    job_id,
                    title,
                    company,
                    job_url,
                    site,
                    1 if emailed else 0,
                    1 if emailed else 0,
                ),
            )

    def count_by_status(self, status: str) -> int:
        self._validate_status(status)
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM applications WHERE status = ?", (status,))
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def count_today(self, status: str = "APPLIED_AUTO", on_date: date | None = None) -> int:
        """Count applications for a local calendar day (defaults to today)."""

        self._validate_status(status)
        if on_date is None:
            date_clause = "date(applied_at, 'localtime') = date('now', 'localtime')"
            params: Sequence[Any] = (status,)
        else:
            date_clause = "date(applied_at, 'localtime') = ?"
            params = (status, on_date.isoformat())
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM applications WHERE status = ? AND {date_clause}",
                tuple(params),
            )
            row = cursor.fetchone()
            return int(row["total"]) if row else 0

    def count_applied_today(self) -> int:
        return self.count_today("APPLIED_AUTO")

    def stats(self) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM applications
                GROUP BY status
                """
            )
            by_status = {row["status"]: int(row["total"]) for row in cursor.fetchall()}
            cursor.execute("SELECT COUNT(*) AS total FROM applications")
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                """
                SELECT COUNT(*) AS total FROM applications
                WHERE status = 'APPLIED_AUTO'
                  AND date(applied_at, 'localtime') = date('now', 'localtime')
                """
            )
            today = int(cursor.fetchone()["total"])
            cursor.execute("SELECT COUNT(*) AS total FROM seen_jobs")
            seen = int(cursor.fetchone()["total"])
        return {
            "total": total,
            "applied_auto": by_status.get("APPLIED_AUTO", 0),
            "failed_auto": by_status.get("FAILED_AUTO", 0),
            "email_sent_external": by_status.get("EMAIL_SENT_EXTERNAL", 0),
            "applied_today": today,
            "seen_jobs": seen,
            "by_status": by_status,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, company, job_url, status, applied_at, notes
                FROM applications
                ORDER BY datetime(applied_at) DESC, rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, status, notes, created_at
                FROM application_events
                WHERE job_id = ?
                ORDER BY event_id ASC
                """,
                (job_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def record(
        self,
        job_id: str,
        title: str,
        company: str,
        job_url: str,
        status: str,
        notes: str | None = None,
    ) -> None:
        """Insert a job result or replace the current state if it already exists."""

        self._validate_status(status)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO applications (id, title, company, job_url, status, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title      = excluded.title,
                    company    = excluded.company,
                    job_url    = excluded.job_url,
                    status     = excluded.status,
                    applied_at = CURRENT_TIMESTAMP,
                    notes      = excluded.notes
                """,
                (job_id, title, company, job_url, status, notes),
            )
            self._connection.execute(
                """
                INSERT INTO application_events (job_id, status, notes)
                VALUES (?, ?, ?)
                """,
                (job_id, status, notes),
            )

    def update_status(
        self,
        job_id: str,
        status: str,
        note: str | None = None,
        append_note: bool = True,
    ) -> bool:
        """Update status and optionally append to the existing notes."""

        self._validate_status(status)
        with self._lock, self._connection:
            cursor = self._connection.cursor()
            cursor.execute("SELECT notes FROM applications WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.close()
                return False
            existing = row["notes"] or ""
            if note and append_note and existing:
                combined = f"{existing}; {note}"
            elif note:
                combined = note
            else:
                combined = existing
            cursor.execute(
                """
                UPDATE applications
                SET status = ?, notes = ?, applied_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, combined, job_id),
            )
            self._connection.execute(
                """
                INSERT INTO application_events (job_id, status, notes)
                VALUES (?, ?, ?)
                """,
                (job_id, status, note),
            )
            cursor.close()
        return True

    def add_note(self, job_id: str, note: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.cursor()
            cursor.execute("SELECT notes FROM applications WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.close()
                return False
            combined = f"{row['notes']}; {note}" if row["notes"] else note
            cursor.execute("UPDATE applications SET notes = ? WHERE id = ?", (combined, job_id))
            cursor.close()
        return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Unsupported status {status!r}. Expected one of {sorted(VALID_STATUSES)}"
            )
