"""Tests for the sqlite job ledger (FR-5, NFR-7, NFR-8)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from transcription.errors import ErrorKind
from transcription.ledger import Ledger

FR5_COLUMNS = {
    "job_id",
    "created_at",
    "started_at",
    "finished_at",
    "status",
    "provider",
    "model",
    "device",
    "source_path",
    "output_path",
    "audio_duration_sec",
    "elapsed_sec",
    "realtime_factor",
    "cost_usd",
    "currency",
    "language",
    "segment_count",
    "filtered_segment_count",
    "error_kind",
    "error_message",
    "meeting_json",
    "service_version",
    # Schema v2 (LLM job types).
    "job_type",
    "result_json",
}


def _insert(
    ledger: Ledger,
    job_id: str = "job-1",
    *,
    provider: str = "local",
    model: str = "large-v3",
    device: str = "cpu",
    source_path: str = "C:/vault/in.wav",
    output_path: str = "C:/vault/out",
    language: str | None = None,
) -> None:
    ledger.insert_job(
        job_id,
        provider=provider,
        model=model,
        device=device,
        source_path=source_path,
        output_path=output_path,
        language=language,
    )


def test_open_fresh_path_creates_file_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    assert not db_path.exists()

    ledger = Ledger(db_path)
    try:
        assert db_path.exists()

        raw = sqlite3.connect(db_path)
        try:
            journal_mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
            user_version = raw.execute("PRAGMA user_version").fetchone()[0]
            assert journal_mode.lower() == "wal"
            assert user_version == 2

            columns = {row[1] for row in raw.execute("PRAGMA table_info(jobs)").fetchall()}
            assert FR5_COLUMNS <= columns
        finally:
            raw.close()
    finally:
        ledger.close()


def test_open_path_with_missing_parent_directory_creates_it(tmp_path: Path) -> None:
    """E3 / FR-10 acceptance: a fresh app folder has no `data/` yet -- the
    ledger must create its own parent directory rather than crashing with a
    raw `sqlite3.OperationalError`."""
    db_path = tmp_path / "does" / "not" / "exist" / "jobs.sqlite3"
    assert not db_path.parent.exists()

    ledger = Ledger(db_path)
    try:
        assert db_path.exists()
    finally:
        ledger.close()


def test_schema_has_indexes_on_created_at_and_status(tmp_path: Path) -> None:
    """E10: `list_jobs`'s documented query pattern (recent jobs, filtered by
    status) needs an index or every call scans the whole table."""
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        raw = sqlite3.connect(ledger.db_path)
        try:
            index_sql = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
            ).fetchall()
            joined = " ".join(row[0] or "" for row in index_sql)
            assert "created_at" in joined
            assert "status" in joined
        finally:
            raw.close()
    finally:
        ledger.close()


def test_reopen_preserves_rows_without_destructive_ddl(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"

    ledger = Ledger(db_path)
    _insert(ledger, "job-1")
    ledger.close()

    reopened = Ledger(db_path)
    try:
        rows = reopened.list_jobs()
        assert len(rows) == 1
        assert rows[0]["job_id"] == "job-1"
    finally:
        reopened.close()


def test_delete_and_restart_recreates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"

    ledger = Ledger(db_path)
    _insert(ledger, "job-1")
    ledger.close()

    db_path.unlink()
    assert not db_path.exists()

    fresh = Ledger(db_path)
    try:
        assert db_path.exists()
        assert fresh.list_jobs() == []
    finally:
        fresh.close()


def test_local_success_leaves_one_succeeded_row(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-1", provider="local")
        ledger.mark_running("job-1")
        ledger.finish_succeeded(
            "job-1",
            elapsed_sec=2.5,
            audio_duration_sec=5.0,
            segment_count=3,
        )

        rows = ledger.list_jobs()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "succeeded"
        assert row["elapsed_sec"] > 0
        assert row["realtime_factor"] > 0
        assert row["cost_usd"] is None
    finally:
        ledger.close()


def test_finish_failed_records_error_kind_and_elapsed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-1")
        ledger.mark_running("job-1")
        ledger.finish_failed(
            "job-1",
            elapsed_sec=1.2,
            kind=ErrorKind.PROVIDER_AUTH,
            message="provider rejected credentials",
        )

        rows = ledger.list_jobs()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "failed"
        assert row["error_kind"] == "provider_auth"
        assert row["elapsed_sec"] is not None
    finally:
        ledger.close()


def test_finish_cancelled_records_error_kind_cancelled(tmp_path: Path) -> None:
    """E9: a cancelled row must carry `error_kind='cancelled'`, not `NULL`."""
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-1")
        ledger.mark_running("job-1")
        ledger.finish_cancelled("job-1", elapsed_sec=0.5)

        row = ledger.list_jobs()[0]
        assert row["status"] == "cancelled"
        assert row["error_kind"] == "cancelled"
    finally:
        ledger.close()


def test_reported_cost_is_stored_and_an_unreported_one_stays_null(tmp_path: Path) -> None:
    """The `cost_usd`/`currency` columns are part of the ledger contract the
    desktop reads. A job that reports a cost stores it; a job that reports
    none (every local run) leaves the column NULL -- never 0.0."""
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-with-cost", provider="local")
        ledger.mark_running("job-with-cost")
        ledger.finish_succeeded(
            "job-with-cost",
            elapsed_sec=1.0,
            audio_duration_sec=2.0,
            segment_count=1,
            cost_usd=0.006,
            currency="USD",
        )

        _insert(ledger, "job-without-cost", provider="local")
        ledger.mark_running("job-without-cost")
        ledger.finish_succeeded(
            "job-without-cost",
            elapsed_sec=1.0,
            audio_duration_sec=2.0,
            segment_count=1,
        )

        raw = sqlite3.connect(ledger.db_path)
        try:
            costed_row = raw.execute(
                "SELECT cost_usd, currency FROM jobs WHERE job_id = ?", ("job-with-cost",)
            ).fetchone()
            assert isinstance(costed_row[0], float)
            assert costed_row[0] == pytest.approx(0.006)
            assert costed_row[1] == "USD"

            (is_null,) = raw.execute(
                "SELECT cost_usd IS NULL FROM jobs WHERE job_id = ?", ("job-without-cost",)
            ).fetchone()
            assert is_null == 1
        finally:
            raw.close()
    finally:
        ledger.close()


def test_reconcile_interrupted_flips_non_terminal_rows(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-queued")
        _insert(ledger, "job-running")
        ledger.mark_running("job-running")
        _insert(ledger, "job-done")
        ledger.mark_running("job-done")
        ledger.finish_succeeded(
            "job-done", elapsed_sec=1.0, audio_duration_sec=1.0, segment_count=1
        )

        count = ledger.reconcile_interrupted()
        assert count == 2

        by_id = {row["job_id"]: row for row in ledger.list_jobs()}
        assert by_id["job-queued"]["status"] == "failed"
        assert by_id["job-queued"]["error_kind"] == "internal"
        assert "interrupt" in by_id["job-queued"]["error_message"].lower()
        assert by_id["job-running"]["status"] == "failed"
        assert by_id["job-running"]["error_kind"] == "internal"
        assert by_id["job-done"]["status"] == "succeeded"
    finally:
        ledger.close()


def test_list_jobs_newest_first_and_filters(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "jobs.sqlite3")
    try:
        _insert(ledger, "job-1")
        ledger.mark_running("job-1")
        ledger.finish_succeeded("job-1", elapsed_sec=1.0, audio_duration_sec=1.0, segment_count=1)

        _insert(ledger, "job-2")

        _insert(ledger, "job-3")
        ledger.mark_running("job-3")
        ledger.finish_succeeded("job-3", elapsed_sec=1.0, audio_duration_sec=1.0, segment_count=1)

        all_jobs = ledger.list_jobs()
        assert [row["job_id"] for row in all_jobs] == ["job-3", "job-2", "job-1"]

        limited = ledger.list_jobs(limit=2)
        assert [row["job_id"] for row in limited] == ["job-3", "job-2"]

        succeeded = ledger.list_jobs(status="succeeded")
        assert [row["job_id"] for row in succeeded] == ["job-3", "job-1"]
    finally:
        ledger.close()


def test_concurrent_read_during_write_does_not_raise_locked(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    writer = Ledger(db_path)
    reader = Ledger(db_path)
    try:
        _insert(writer, "job-1")

        errors: list[BaseException] = []

        def write_many() -> None:
            try:
                for i in range(20):
                    _insert(writer, f"job-w{i}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def read_many() -> None:
            try:
                for _ in range(20):
                    reader.list_jobs()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=write_many)
        t2 = threading.Thread(target=read_many)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
    finally:
        writer.close()
        reader.close()


_V1_DDL = """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
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
    meeting_json TEXT,
    service_version TEXT NOT NULL
)
"""


def _build_v1_db(db_path: Path) -> None:
    """A database exactly as a v1 (pre-LLM) build would leave it, with one row."""
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(_V1_DDL)
        raw.execute(
            """
            INSERT INTO jobs (
                job_id, created_at, status, provider, model, device,
                source_path, output_path, service_version
            ) VALUES ('old-job', '2026-01-01T00:00:00+00:00', 'succeeded',
                      'local', 'large-v3', 'cpu', 'C:/vault/in.wav',
                      'C:/vault/out', '0.4.0')
            """
        )
        raw.execute("PRAGMA user_version=1")
        raw.commit()
    finally:
        raw.close()


def test_v1_database_migrates_in_place_and_old_rows_stay_readable(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    _build_v1_db(db_path)

    ledger = Ledger(db_path)
    try:
        raw = sqlite3.connect(db_path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
            columns = {row[1] for row in raw.execute("PRAGMA table_info(jobs)").fetchall()}
            assert {"job_type", "result_json"} <= columns
        finally:
            raw.close()

        old_row = ledger.get_job("old-job")
        assert old_row is not None
        assert old_row["job_type"] == "transcribe"
        assert old_row["result_json"] is None

        # A new-style row round-trips through the migrated table.
        ledger.insert_job(
            "new-job",
            job_type="summarize",
            provider="llama_cpp",
            model="qwen",
            device="cpu",
            source_path="C:/vault/ELS/260101 - m",
            output_path="C:/vault/ELS/260101 - m",
        )
        ledger.finish_succeeded(
            "new-job", elapsed_sec=1.5, result_json='{"artifacts": ["summary.md"]}'
        )
        new_row = ledger.get_job("new-job")
        assert new_row is not None
        assert new_row["job_type"] == "summarize"
        assert new_row["result_json"] == '{"artifacts": ["summary.md"]}'
        assert new_row["audio_duration_sec"] is None
        assert new_row["realtime_factor"] is None
    finally:
        ledger.close()


def test_a_newer_schema_version_is_still_rejected(tmp_path: Path) -> None:
    from transcription.ledger import LedgerError

    db_path = tmp_path / "jobs.sqlite3"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA user_version=3")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(LedgerError):
        Ledger(db_path)
