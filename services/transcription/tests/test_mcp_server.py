"""Tests for the standalone stdio MCP server (`mcp_server.py`).

Tools are exercised in-process via `FastMCP.call_tool` -- no stdio, no
network, no models (the embedding GGUF is absent, so search runs
text-only; FR-15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder

from transcription.config import Config
from transcription.errors import ServiceError
from transcription.mcp_server import _NO_INDEX_MESSAGE, _Vault, build_server
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

LONG_RU = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    meeting = root / "ACME" / "260831 - Weekly sync"
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "text": LONG_RU,
                "segments": [
                    {"id": 0, "start": 0.0, "end": 5.0, "text": LONG_RU, "speaker": "Speaker 1"},
                    {"id": 1, "start": 100.0, "end": 105.0, "text": "Later remark."},
                ],
            }
        ),
        encoding="utf-8",
    )
    (meeting / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Даниил"}}), encoding="utf-8"
    )
    (meeting / "summary.md").write_text("# Summary\n\nDeadline moved.", encoding="utf-8")
    (root / "reports").mkdir()
    return root


@pytest.fixture
def config(tmp_app_dir: Path, vault_root: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        allowed_roots=(str(tmp_app_dir),),
        index_db_path=str(tmp_app_dir / "data" / "index.sqlite3"),
        vault_root=str(vault_root),
        llm_model_path=str(tmp_app_dir / "models" / "llm"),  # no GGUF: text-only search
    )


def _build_index(config: Config, vault_root: Path) -> None:
    db = IndexDb(
        config.index_db_path,
        embedding_model=config.embedding_model,
        embedding_dim=FakeEmbedder.DIM,
    )
    index_vault(vault_root, db, FakeEmbedder())
    db.close()


async def _call(server: Any, tool: str, arguments: dict[str, Any]) -> str:
    """Normalizes `call_tool`'s content shape (which varies across mcp
    versions) into one searchable string."""
    result = await server.call_tool(tool, arguments)
    return str(result)


async def test_hybrid_search_without_an_index_names_the_fix(config: Config) -> None:
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн"})

    assert "index has not been built" in answer
    assert answer.find(_NO_INDEX_MESSAGE[:30]) != -1


async def test_hybrid_search_finds_text_without_any_model(config: Config, vault_root: Path) -> None:
    _build_index(config, vault_root)
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн"})

    assert "260831 - Weekly sync" in answer


async def test_listings_report_projects_meetings_and_artifacts(
    config: Config, vault_root: Path
) -> None:
    server = build_server(config)

    projects = await _call(server, "list_projects", {})
    meetings = await _call(server, "list_meetings", {})

    assert "ACME" in projects
    assert "reports" not in projects  # reserved dirs are not projects
    assert "260831 - Weekly sync" in meetings
    assert "'has_summary': True" in meetings or '"has_summary": true' in meetings
    assert "'has_note': False" in meetings or '"has_note": false' in meetings


async def test_read_transcript_applies_speaker_names_and_the_time_window(
    config: Config, vault_root: Path
) -> None:
    server = build_server(config)

    full = await _call(server, "read_transcript", {"meeting_dir": "ACME/260831 - Weekly sync"})
    windowed = await _call(
        server,
        "read_transcript",
        {"meeting_dir": "ACME/260831 - Weekly sync", "start_sec": 90, "end_sec": 120},
    )

    assert "Даниил" in full  # the operator's rename outranks the raw label
    assert "Later remark." in full
    assert "Later remark." in windowed
    assert "Даниил" not in windowed


async def test_read_summary_and_a_missing_note_answer_honestly(
    config: Config, vault_root: Path
) -> None:
    server = build_server(config)

    summary = await _call(server, "read_summary", {"meeting_dir": "ACME/260831 - Weekly sync"})
    note = await _call(server, "read_note", {"meeting_dir": "ACME/260831 - Weekly sync"})

    assert "Deadline moved." in summary
    assert "No note" in note


def test_meeting_dir_resolution_refuses_traversal(config: Config) -> None:
    vault = _Vault(config)

    with pytest.raises(ServiceError):
        vault.meeting_dir("..\\..\\secrets")
    with pytest.raises(ServiceError):
        vault.meeting_dir("../outside")
