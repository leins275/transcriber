"""Speaker-scoped retrieval through the stdio MCP server (`mcp_server.py`).

Tools run in-process via `FastMCP.call_tool` -- no stdio, no network, no
models: the index is built with `FakeEmbedder` and no embedding GGUF is
installed, so search answers text-only (FR-4, FR-3).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder

from transcription.config import Config
from transcription.mcp_server import _NO_INDEX_MESSAGE, build_server
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

LONG_RU = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "

IVAN = "Иван Петров"
ANNA = "Анна Кузнецова"
IVANS_MEETING = "260831 - Weekly sync"  # Иван and Анна speak
ANNAS_MEETING = "260901 - Budget call"  # only Анна speaks

PRE_SPEAKER_TAGS_SCHEMA_VERSION = 1


def _write_transcript(meeting_dir: Path, speakers: list[str]) -> None:
    meeting_dir.mkdir(parents=True)
    segments = [
        {
            "id": index,
            "start": index * 5.0,
            "end": index * 5.0 + 5.0,
            "text": LONG_RU,
            "speaker": speakers[index % len(speakers)],
        }
        for index in range(4)
    ]
    (meeting_dir / "transcript.json").write_text(
        json.dumps({"schema_version": 1, "text": LONG_RU * 4, "segments": segments}),
        encoding="utf-8",
    )


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    _write_transcript(root / "ACME" / IVANS_MEETING, [IVAN, ANNA])
    _write_transcript(root / "ACME" / ANNAS_MEETING, [ANNA])
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


def _stamp_schema_version(index_path: Path, version: int) -> None:
    """Rewrite the index file's `PRAGMA user_version` by hand -- the only
    way to produce a file an older release would have left behind."""
    conn = sqlite3.connect(index_path)
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
    conn.close()


async def _call(server: Any, tool: str, arguments: dict[str, Any]) -> str:
    """Normalizes `call_tool`'s content shape (which varies across mcp
    versions) into one searchable string."""
    result = await server.call_tool(tool, arguments)
    return str(result)


async def test_hybrid_search_scoped_to_a_speaker_returns_only_that_speakers_meeting(
    config: Config, vault_root: Path
) -> None:
    _build_index(config, vault_root)
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн", "speaker": IVAN})

    assert IVANS_MEETING in answer
    assert ANNAS_MEETING not in answer


async def test_hybrid_search_without_a_speaker_returns_every_matching_meeting(
    config: Config, vault_root: Path
) -> None:
    _build_index(config, vault_root)
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн"})

    assert IVANS_MEETING in answer
    assert ANNAS_MEETING in answer


async def test_hybrid_search_for_a_speaker_nobody_knows_finds_nothing_instead_of_failing(
    config: Config, vault_root: Path
) -> None:
    _build_index(config, vault_root)
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн", "speaker": "Пётр Сидоров"})

    assert IVANS_MEETING not in answer
    assert ANNAS_MEETING not in answer
    assert _NO_INDEX_MESSAGE[:30] not in answer  # empty by the filter, not by degradation


async def test_hybrid_search_over_an_older_schema_index_asks_for_the_app_instead_of_answering(
    config: Config, vault_root: Path
) -> None:
    _build_index(config, vault_root)
    _stamp_schema_version(Path(config.index_db_path), PRE_SPEAKER_TAGS_SCHEMA_VERSION)
    server = build_server(config)

    answer = await _call(server, "hybrid_search", {"query": "дедлайн"})

    assert "index has not been built" in answer
    assert IVANS_MEETING not in answer


async def test_the_hybrid_search_tool_documents_its_speaker_argument(config: Config) -> None:
    server = build_server(config)

    tools = {tool.name: tool for tool in await server.list_tools()}

    assert "speaker" in (tools["hybrid_search"].description or "")
