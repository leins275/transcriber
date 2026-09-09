"""Startup cleanup of the orphaned pre-0.18 app-dir search index (FR-1, FR-2).

Before 0.18 the search index lived at ``<app_dir>/data/index.sqlite3``; it now
lives inside the vault it describes, so an upgraded install keeps an orphan
nobody reads. These tests pin the removal: real tmp files, the real FastAPI
lifespan, no model, no network, no mocks of in-process collaborators (the
``OSError`` branch is provoked with a real un-unlinkable path rather than a
patched ``unlink``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient

from transcription import providers
from transcription.app import create_app
from transcription.config import Config
from transcription.search import index_db

LEGACY_BYTES = b"SQLite format 3\x00pre-0.18 app-dir index"
VAULT_BYTES = b"SQLite format 3\x00the live in-vault index"


def _seed_legacy_index(app_dir: Path) -> Path:
    """Write the pre-0.18 index and both of its SQLite sidecars."""
    legacy = app_dir / "data" / "index.sqlite3"
    legacy.write_bytes(LEGACY_BYTES)
    legacy.with_name("index.sqlite3-wal").write_bytes(b"wal")
    legacy.with_name("index.sqlite3-shm").write_bytes(b"shm")
    return legacy


def _seed_vault_index(vault_root: Path) -> Path:
    live = vault_root / ".transcriber" / "index.sqlite3"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(VAULT_BYTES)
    return live


def _events(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    return [record for record in records if getattr(record, "event", None) == event]


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    root.mkdir()
    return root


@pytest.fixture
def vault_index_path(vault_root: Path) -> Path:
    return vault_root / ".transcriber" / "index.sqlite3"


@pytest.fixture
def log_records() -> Iterator[list[logging.LogRecord]]:
    """Every record the service's own ``transcription`` logger emits.

    A dedicated handler rather than ``caplog``: ``configure_logging`` sets
    ``propagate = False`` on this logger, so records may never reach the root
    handler ``caplog`` installs.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("transcription")
    handler = _Collector(level=logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.fixture
def config(tmp_app_dir: Path, vault_root: Path, vault_index_path: Path) -> Config:
    """A vault-rooted config: the live index is inside the vault, not the app dir."""
    providers.register("fake", FakeProvider)
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        index_db_path=str(vault_index_path),
        vault_root=str(vault_root),
        token="test-token",  # noqa: S106 -- test fixture
    )


# --- the helper (FR-1, FR-2) -------------------------------------------------


def test_legacy_app_dir_index_is_deleted_with_its_wal_and_shm_sidecars(
    tmp_app_dir: Path, vault_index_path: Path
) -> None:
    legacy = _seed_legacy_index(tmp_app_dir)

    removed = index_db.remove_legacy_app_dir_index(tmp_app_dir, vault_index_path)

    assert removed is True
    assert not legacy.exists()
    assert not legacy.with_name("index.sqlite3-wal").exists()
    assert not legacy.with_name("index.sqlite3-shm").exists()


def test_the_in_vault_index_survives_the_cleanup_byte_for_byte(
    tmp_app_dir: Path, vault_root: Path
) -> None:
    _seed_legacy_index(tmp_app_dir)
    live = _seed_vault_index(vault_root)

    index_db.remove_legacy_app_dir_index(tmp_app_dir, live)

    assert live.read_bytes() == VAULT_BYTES


@pytest.mark.parametrize(
    "spell",
    [
        pytest.param(lambda legacy: str(legacy), id="the vault-less default path"),
        pytest.param(
            lambda legacy: str(legacy.parent / ".." / "data" / legacy.name),
            id="a non-normalised path naming the same file",
        ),
    ],
)
def test_an_app_dir_index_that_is_still_the_configured_one_is_kept(
    tmp_app_dir: Path, spell: Callable[[Path], str]
) -> None:
    legacy = _seed_legacy_index(tmp_app_dir)

    removed = index_db.remove_legacy_app_dir_index(tmp_app_dir, spell(legacy))

    assert removed is False
    assert legacy.read_bytes() == LEGACY_BYTES


def test_sidecars_orphaned_without_their_main_file_are_swept_too(
    tmp_app_dir: Path, vault_index_path: Path
) -> None:
    # A crash mid-checkpoint on the pre-0.18 build can leave the WAL pair
    # behind without the database itself; the three files are one unit.
    wal = tmp_app_dir / "data" / "index.sqlite3-wal"
    shm = tmp_app_dir / "data" / "index.sqlite3-shm"
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")

    removed = index_db.remove_legacy_app_dir_index(tmp_app_dir, vault_index_path)

    # No main file existed, so there is nothing to report as removed...
    assert removed is False
    # ...but the orphaned sidecars are gone all the same.
    assert not wal.exists()
    assert not shm.exists()


def test_nothing_to_clean_up_is_reported_as_no_removal(
    tmp_app_dir: Path, vault_index_path: Path
) -> None:
    removed = index_db.remove_legacy_app_dir_index(tmp_app_dir, vault_index_path)

    assert removed is False


def test_an_unremovable_legacy_index_is_reported_as_a_warning_instead_of_raising(
    tmp_app_dir: Path, vault_index_path: Path, log_records: list[logging.LogRecord]
) -> None:
    # A non-empty directory in the legacy file's place: `unlink` cannot remove
    # it, which is the same OSError family as a file locked by another process.
    blocked = tmp_app_dir / "data" / "index.sqlite3"
    blocked.mkdir()
    (blocked / "held-open.bin").write_bytes(b"locked")

    removed = index_db.remove_legacy_app_dir_index(tmp_app_dir, vault_index_path)

    assert removed is False
    assert [record.levelno for record in _events(log_records, "legacy_index_remove_failed")] == [
        logging.WARNING
    ]


# --- the service startup (FR-1 c3/c4, FR-2 c2/c3) ----------------------------


def test_service_startup_removes_the_orphaned_app_dir_index(
    config: Config, tmp_app_dir: Path
) -> None:
    legacy = _seed_legacy_index(tmp_app_dir)

    with TestClient(create_app(config)):
        pass

    assert not legacy.exists()


def test_service_startup_logs_the_removal_exactly_once(
    config: Config, tmp_app_dir: Path, log_records: list[logging.LogRecord]
) -> None:
    _seed_legacy_index(tmp_app_dir)

    with TestClient(create_app(config)):
        pass

    assert len(_events(log_records, "legacy_index_removed")) == 1


def test_service_startup_keeps_stdout_free_of_the_removal(
    config: Config, tmp_app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_legacy_index(tmp_app_dir)

    with TestClient(create_app(config)):
        pass

    assert capsys.readouterr().out == ""


def test_service_startup_stays_silent_when_there_is_no_orphan(
    config: Config, log_records: list[logging.LogRecord]
) -> None:
    with TestClient(create_app(config)):
        pass

    assert _events(log_records, "legacy_index_removed") == []


def test_service_starts_even_when_the_orphan_cannot_be_removed(
    config: Config, tmp_app_dir: Path
) -> None:
    blocked = tmp_app_dir / "data" / "index.sqlite3"
    blocked.mkdir()
    (blocked / "held-open.bin").write_bytes(b"locked")

    with TestClient(create_app(config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
