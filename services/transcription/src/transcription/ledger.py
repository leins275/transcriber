"""SQLite job ledger (FR-5, NFR-7, NFR-8).

One row per job, inserted at submission time and only ever ``UPDATE``d --
never re-inserted, never deleted. WAL mode lets the single worker thread
write while the event loop reads job status concurrently without hitting
``database is locked``.

Deliberately dependency-free: this module does not import ``errors.py`` or
anything else from this package. ``error_kind`` is stored as the plain
string value handed to ``finish_failed`` (an ``ErrorKind`` member happens to
already be a ``str`` subclass, so callers may pass either).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription import __version__

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'transcribe',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    device TEXT NOT NULL,
    source_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    audio_duration_sec REAL,
    elapsed_sec REAL,
    realtime_factor REAL,
    cost_usd REAL,
    currency TEXT,
    language TEXT,
    segment_count INTEGER,
    filtered_segment_count INTEGER,
    error_kind TEXT,
    error_message TEXT,
    result_json TEXT,
    meeting_json TEXT,
    service_version TEXT NOT NULL
)
"""

# Columns added by each schema version bump, applied in order to a database
# created by an older build. `ALTER TABLE ... ADD COLUMN` is the entire
# migration story on purpose: rows are never rewritten, so a v1 row keeps
# its meaning (`job_type` backfills to 'transcribe' via the column default).
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    # v1 -> v2: job-type discriminator + the result manifest for non-transcribe
    # jobs (LLM summaries/extractions write artifacts, not transcript.json).
    2: (
        "ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'transcribe'",
        "ALTER TABLE jobs ADD COLUMN result_json TEXT",
    ),
}


class LedgerError(Exception):
    """Raised when the ledger database cannot be opened safely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error_kind_value(kind: Any) -> str:
    value = getattr(kind, "value", None)
    return value if isinstance(value, str) else str(kind)


class Ledger:
    """The sqlite-backed job ledger. One instance per open connection."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        # `sqlite3.connect` does not create the parent directory (FR-10
        # acceptance: a fresh app folder with no `data/` yet must still work).
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        except sqlite3.OperationalError as exc:
            raise LedgerError(f"unable to open ledger database {self.db_path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            current_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SCHEMA_VERSION:
                raise LedgerError(
                    f"{self.db_path}: ledger schema version {current_version} is newer than "
                    f"this build supports (max {SCHEMA_VERSION})"
                )
            # Migrate an existing older database before the CREATE TABLE IF
            # NOT EXISTS below (a no-op once the table exists) so the DDL and
            # the migrated table agree on the column set. `user_version` 0
            # means a fresh database with no table yet -- nothing to migrate.
            if current_version > 0:
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    for statement in _MIGRATIONS[version]:
                        self._conn.execute(statement)
            self._conn.execute(_DDL)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            if current_version < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_job(
        self,
        job_id: str,
        *,
        provider: str,
        model: str,
        device: str,
        source_path: str,
        output_path: str,
        job_type: str = "transcribe",
        language: str | None = None,
        meeting_json: str | None = None,
    ) -> None:
        """Insert the one row for this job, in ``queued`` status."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, created_at, status, job_type, provider, model, device,
                    source_path, output_path, language, meeting_json, service_version
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    _now(),
                    job_type,
                    provider,
                    model,
                    device,
                    source_path,
                    output_path,
                    language,
                    meeting_json,
                    __version__,
                ),
            )
            self._conn.commit()

    def mark_running(self, job_id: str, *, device: str | None = None) -> None:
        """Flip a row to ``running``, optionally also recording its resolved
        ``device`` (E15: no longer known at insert time -- resolving the
        provider is deferred off the event loop until the job actually
        starts; see ``jobs.py``'s ``_run_job``)."""
        with self._lock:
            if device is not None:
                self._conn.execute(
                    "UPDATE jobs SET status='running', started_at=?, device=? WHERE job_id=?",
                    (_now(), device, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
                    (_now(), job_id),
                )
            self._conn.commit()

    def finish_succeeded(
        self,
        job_id: str,
        *,
        elapsed_sec: float,
        audio_duration_sec: float | None = None,
        segment_count: int | None = None,
        filtered_segment_count: int = 0,
        cost_usd: float | None = None,
        currency: str | None = None,
        result_json: str | None = None,
        language: str | None = None,
    ) -> None:
        """Flip a row to ``succeeded``.

        ``audio_duration_sec``/``segment_count`` describe a transcription's
        output and stay ``NULL`` for the LLM job types, which instead record
        their artifact manifest in ``result_json``.

        ``language`` is the language a transcribe job actually decoded in,
        which is only known once the job has run and may differ from the
        (possibly absent) language requested at insert time. ``COALESCE``
        keeps the write additive: passing ``None`` leaves whatever the row
        already carries, so the LLM job types -- which never pass one --
        are untouched.
        """
        realtime_factor = elapsed_sec / audio_duration_sec if audio_duration_sec else None
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET
                    status='succeeded',
                    finished_at=?,
                    elapsed_sec=?,
                    audio_duration_sec=?,
                    realtime_factor=?,
                    segment_count=?,
                    filtered_segment_count=?,
                    cost_usd=?,
                    currency=?,
                    result_json=?,
                    language=COALESCE(?, language)
                WHERE job_id=?
                """,
                (
                    _now(),
                    elapsed_sec,
                    audio_duration_sec,
                    realtime_factor,
                    segment_count,
                    filtered_segment_count,
                    cost_usd,
                    currency,
                    result_json,
                    language,
                    job_id,
                ),
            )
            self._conn.commit()

    def finish_failed(
        self,
        job_id: str,
        *,
        elapsed_sec: float | None,
        kind: Any,
        message: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET
                    status='failed',
                    finished_at=?,
                    elapsed_sec=?,
                    error_kind=?,
                    error_message=?
                WHERE job_id=?
                """,
                (_now(), elapsed_sec, _error_kind_value(kind), message, job_id),
            )
            self._conn.commit()

    def finish_cancelled(self, job_id: str, *, elapsed_sec: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET
                    status='cancelled', finished_at=?, elapsed_sec=?, error_kind='cancelled'
                WHERE job_id=?
                """,
                (_now(), elapsed_sec, job_id),
            )
            self._conn.commit()

    def reconcile_interrupted(self) -> int:
        """Flip rows left in ``queued``/``running`` to ``failed`` (NFR-7)."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs SET
                    status='failed',
                    finished_at=?,
                    error_kind='internal',
                    error_message='job was interrupted by a service restart'
                WHERE status IN ('queued', 'running')
                """,
                (_now(),),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_jobs(
        self, *, limit: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, rowid DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
