"""The curated LLM catalog surface: `/v1/llm-models` listing, per-model
download slots, delete with its guards, and the per-file already-present
semantics that let several GGUFs coexist in one `models/llm` directory.
Offline throughout: fake hub/transport via the `llm_models_factory_for`
seam, exactly like `test_api_llm.py`'s single-slot tests."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_model_api import FakeHubClient, FakeTransport

from transcription import llm_catalog
from transcription.api.model_routes import LlmGgufDownload, LlmModelsManager
from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ServiceError
from transcription.model_download import ModelDownload, RemoteFile

AUTH = {"Authorization": "Bearer test-token"}
WRONG_AUTH = {"Authorization": "Bearer wrong-token"}

CONTENT = {entry.file: entry.id.encode() * 100 for entry in llm_catalog.CATALOG}

DEFAULT = llm_catalog.DEFAULT_MODEL_ID
LEGACY = llm_catalog.LEGACY_MODEL_ID


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        llm_model_path=str(tmp_app_dir / "models" / "llm"),
        token="test-token",  # noqa: S106 -- test fixture
    )


def _factory_for(config: Config, *, chunk_sleep: float = 0.0):  # type: ignore[no-untyped-def]
    """One fake download per catalog entry: a hub listing every quant, a
    transport with per-file content -- the filter must pick exactly one."""

    def factory(entry: llm_catalog.CatalogEntry | None) -> ModelDownload:
        assert entry is not None, "these tests only exercise catalog models"
        files = [
            RemoteFile(path=file, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            for file, data in CONTENT.items()
        ]
        wanted = entry.file.casefold()
        return LlmGgufDownload(
            target_file=entry.file,
            models_dir=config.llm_model_path,
            allowed_roots=(Path(config.app_dir),),
            repo_id=entry.repo,
            revision=entry.revision,
            hub_client=FakeHubClient(files),
            transport=FakeTransport(dict(CONTENT), chunk_sleep=chunk_sleep),
            file_filter=lambda remote: remote.path.casefold() == wanted,
        )

    return factory


def _wait_terminal(client: TestClient, model_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get("/v1/llm-models", headers=AUTH).json()
        row = next(m for m in body["models"] if m["id"] == model_id)
        if row["download"]["state"] in {"complete", "error", "cancelled"}:
            return row
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_llm_models_lists_the_catalog_with_the_default_active(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        body = client.get("/v1/llm-models", headers=AUTH).json()
        assert body["active"] == DEFAULT
        assert [m["id"] for m in body["models"]] == list(llm_catalog.known_ids())
        for row in body["models"]:
            entry = llm_catalog.get(row["id"])
            assert entry is not None
            assert row["file"] == entry.file
            assert row["size_bytes"] == entry.size_bytes
            assert row["catalog"] is True
            assert row["present"] is False
            assert row["active"] is (row["id"] == DEFAULT)
            assert row["download"]["state"] == "idle"


def test_llm_models_routes_require_the_bearer_token(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        assert client.get("/v1/llm-models", headers=WRONG_AUTH).status_code == 401
        assert (
            client.post(f"/v1/llm-models/{LEGACY}/download", headers=WRONG_AUTH).status_code == 401
        )
        assert (
            client.delete(f"/v1/llm-models/{LEGACY}/download", headers=WRONG_AUTH).status_code
            == 401
        )
        assert client.delete(f"/v1/llm-models/{LEGACY}", headers=WRONG_AUTH).status_code == 401


def test_llm_models_download_fetches_only_the_requested_model(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        response = client.post(f"/v1/llm-models/{LEGACY}/download", headers=AUTH)
        assert response.status_code == 202

        row = _wait_terminal(client, LEGACY)
        assert row["download"]["state"] == "complete"
        assert row["present"] is True

        llm_dir = Path(config.llm_model_path)
        legacy_entry = llm_catalog.get(LEGACY)
        default_entry = llm_catalog.get(DEFAULT)
        assert legacy_entry is not None and default_entry is not None
        assert (llm_dir / legacy_entry.file).read_bytes() == CONTENT[legacy_entry.file]
        assert not (llm_dir / default_entry.file).exists()

        # The other row is untouched -- and the active model is still absent.
        body = client.get("/v1/llm-models", headers=AUTH).json()
        default_row = next(m for m in body["models"] if m["id"] == DEFAULT)
        assert default_row["present"] is False
        assert default_row["download"]["state"] == "idle"


def test_llm_models_refuses_a_second_concurrent_transfer(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config, chunk_sleep=0.05))
    with TestClient(app) as client:
        assert client.post(f"/v1/llm-models/{LEGACY}/download", headers=AUTH).status_code == 202
        try:
            response = client.post(f"/v1/llm-models/{DEFAULT}/download", headers=AUTH)
            assert response.status_code == 400, response.text
            assert response.json()["error_kind"] == "invalid_request"
        finally:
            client.delete(f"/v1/llm-models/{LEGACY}/download", headers=AUTH)
            _wait_terminal(client, LEGACY)


def test_llm_models_download_of_an_unknown_id_is_refused(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        response = client.post("/v1/llm-models/no-such-model/download", headers=AUTH)
        assert response.status_code == 400
        assert response.json()["error_kind"] == "invalid_request"


def test_llm_models_delete_removes_a_non_active_model(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        client.post(f"/v1/llm-models/{LEGACY}/download", headers=AUTH)
        _wait_terminal(client, LEGACY)

        legacy_entry = llm_catalog.get(LEGACY)
        assert legacy_entry is not None
        gguf = Path(config.llm_model_path) / legacy_entry.file
        assert gguf.is_file()

        body = client.delete(f"/v1/llm-models/{LEGACY}", headers=AUTH).json()
        assert not gguf.exists()
        row = next(m for m in body["models"] if m["id"] == LEGACY)
        assert row["present"] is False


def test_llm_models_delete_of_the_active_model_is_refused(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        response = client.delete(f"/v1/llm-models/{DEFAULT}", headers=AUTH)
        assert response.status_code == 400
        assert "active" in response.json()["error_message"]


def test_llm_models_delete_is_refused_while_an_llm_job_is_active(config: Config) -> None:
    manager = LlmModelsManager(
        config, factory_for=_factory_for(config), has_active_llm_job=lambda: True
    )
    with pytest.raises(ServiceError, match="jobs are running"):
        manager.delete(LEGACY)


def test_llm_models_delete_is_refused_while_that_model_is_downloading(config: Config) -> None:
    app = create_app(config, llm_models_factory_for=_factory_for(config, chunk_sleep=0.05))
    with TestClient(app) as client:
        client.post(f"/v1/llm-models/{LEGACY}/download", headers=AUTH)
        try:
            response = client.delete(f"/v1/llm-models/{LEGACY}", headers=AUTH)
            assert response.status_code == 400
            assert "download" in response.json()["error_message"]
        finally:
            client.delete(f"/v1/llm-models/{LEGACY}/download", headers=AUTH)
            _wait_terminal(client, LEGACY)


def test_gguf_already_present_means_the_target_file_not_the_shared_ready_marker(
    config: Config,
) -> None:
    """Regression for the multi-model directory: the shared `.ready` marker
    (left by the pre-catalog single-model download) must not make an *absent*
    model read as present -- that would short-circuit its SetupDownload model
    phase and never download it -- and a model whose file *is* on disk must
    read as present even with no marker at all (hand-copied GGUFs)."""
    llm_dir = Path(config.llm_model_path)
    llm_dir.mkdir(parents=True)
    (llm_dir / ".ready").touch()
    legacy_entry = llm_catalog.get(LEGACY)
    default_entry = llm_catalog.get(DEFAULT)
    assert legacy_entry is not None and default_entry is not None
    (llm_dir / legacy_entry.file).write_bytes(b"weights")

    factory = _factory_for(config)
    assert factory(legacy_entry).already_present() is True
    assert factory(default_entry).already_present() is False


def test_the_legacy_single_slot_routes_share_the_active_models_slot(config: Config) -> None:
    """`/v1/llm-model/download` (the first-run assistant flow) and
    `/v1/llm-models/<active>/download` must be one slot, never two racing
    transfers of the same file."""
    app = create_app(config, llm_models_factory_for=_factory_for(config))
    with TestClient(app) as client:
        client.post("/v1/llm-model/download", headers=AUTH)
        row = _wait_terminal(client, DEFAULT)
        assert row["download"]["state"] == "complete"
        assert row["present"] is True
        legacy_status = client.get("/v1/llm-model/download", headers=AUTH).json()
        assert legacy_status["state"] == "complete"
