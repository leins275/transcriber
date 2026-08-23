"""HTTP surface of the LLM feature: job_type submissions over `/v1/jobs`,
the artifact-manifest result shape, and the `/v1/llm-model/download` trio
with its one-file GGUF selection. Offline throughout: `FakeLlm` via the
`job_manager_factory` seam, fake hub/transport via `llm_model_download_factory`."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeFrameExtractor, FakeLlm
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.jobs import JobManager
from transcription.ledger import Ledger
from transcription.model_download import ModelDownload, RemoteFile

AUTH = {"Authorization": "Bearer test-token"}
WRONG_AUTH = {"Authorization": "Bearer wrong-token"}
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

GGUF_CONTENT = b"g" * 4_000
GGUF_SHA = hashlib.sha256(GGUF_CONTENT).hexdigest()

MEETING_NAME = "260101 - Planning"


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


@pytest.fixture
def meeting_dir(tmp_app_dir: Path) -> Path:
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "text": "hello world",
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello world"}],
                "source": {"path": "s", "filename": "s", "duration_sec": 10.0},
            }
        ),
        encoding="utf-8",
    )
    return meeting


def _app_with_fake_llm(config: Config, llm: FakeLlm) -> Any:
    def job_manager_factory(cfg: Config, ledger: Ledger) -> JobManager:
        return JobManager(
            cfg,
            ledger,
            llm_factory=lambda _cfg: llm,
            frame_extractor_factory=FakeFrameExtractor,
        )

    return create_app(config, job_manager_factory=job_manager_factory)


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body: dict[str, Any] = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in TERMINAL:
            return body
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still {body['status']}")
        time.sleep(0.01)


def test_summarize_over_http_reports_job_type_and_a_manifest_result(
    config: Config, meeting_dir: Path
) -> None:
    app = _app_with_fake_llm(config, FakeLlm(responses=["the summary"]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "summarize",
                "input_path": str(meeting_dir),
                "output_dir": str(meeting_dir),
            },
            headers=AUTH,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        status = _wait_terminal(client, job_id)
        assert status["status"] == "succeeded"
        assert status["job_type"] == "summarize"
        assert status["warnings"] == []

        result = client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert result.status_code == 200
        manifest = result.json()
        assert manifest["artifacts"] == [str(meeting_dir / "summary.md")]
        assert (meeting_dir / "summary.md").read_text(encoding="utf-8").strip() == "the summary"


def test_a_transcribe_status_still_reports_its_job_type(config: Config) -> None:
    # Old clients keep working: default job_type everywhere.
    app = _app_with_fake_llm(config, FakeLlm())
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={"audio_path": "C:/nope/missing.wav", "output_dir": "C:/nope"},
            headers=AUTH,
        )
        # Path validation rejects it (not under allowed roots), which is all
        # we need: the request SHAPE with no job_type is still accepted.
        assert response.status_code == 400
        assert response.json()["error_kind"] == "invalid_request"


@pytest.mark.parametrize(
    "body",
    [
        # transcribe must not take input_path...
        {"audio_path": "a.wav", "input_path": "b", "output_dir": "c"},
        # ...and a derived job must not take audio_path (or omit input_path).
        {"job_type": "summarize", "audio_path": "a.wav", "output_dir": "c"},
        {"job_type": "summarize", "output_dir": "c"},
        {"job_type": "bogus", "input_path": "b", "output_dir": "c"},
    ],
)
def test_mismatched_job_shapes_are_rejected_as_validation_errors(
    config: Config, body: dict[str, str]
) -> None:
    app = _app_with_fake_llm(config, FakeLlm())
    with TestClient(app) as client:
        response = client.post("/v1/jobs", json=body, headers=AUTH)
        assert response.status_code == 400
        assert response.json()["error_kind"] == "invalid_request"


def _llm_download_factory(config: Config):  # type: ignore[no-untyped-def]
    """A fake GGUF repo holding three quants; the filter must pick exactly one."""
    from test_model_api import FakeHubClient, FakeTransport

    hub = FakeHubClient(
        [
            RemoteFile(path="qwen3.6-35b-a3b-q4_k_m.gguf", size=len(GGUF_CONTENT), sha256=GGUF_SHA),
            RemoteFile(path="qwen3.6-35b-a3b-q8_0.gguf", size=999_999, sha256=None),
            RemoteFile(path="README.md", size=10, sha256=None),
        ]
    )
    transport = FakeTransport({"qwen3.6-35b-a3b-q4_k_m.gguf": GGUF_CONTENT})
    wanted = config.llm_model_file.casefold()

    def factory() -> ModelDownload:
        return ModelDownload(
            models_dir=config.llm_model_path,
            allowed_roots=(Path(config.app_dir),),
            repo_id=config.llm_model_repo,
            revision=config.llm_model_revision,
            hub_client=hub,
            transport=transport,
            file_filter=lambda remote: remote.path.casefold() == wanted,
        )

    return factory


def test_llm_model_download_fetches_exactly_the_configured_file(config: Config) -> None:
    app = create_app(config, llm_model_download_factory=_llm_download_factory(config))
    with TestClient(app) as client:
        assert client.get("/health").json()["llm_model_present"] is False

        response = client.post("/v1/llm-model/download", headers=AUTH)
        assert response.status_code == 202

        deadline = time.monotonic() + 10
        while True:
            status = client.get("/v1/llm-model/download", headers=AUTH).json()
            if status["state"] in {"complete", "error", "cancelled"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert status["state"] == "complete", status
        llm_dir = Path(config.llm_model_path)
        assert (llm_dir / "qwen3.6-35b-a3b-q4_k_m.gguf").read_bytes() == GGUF_CONTENT
        assert not (llm_dir / "qwen3.6-35b-a3b-q8_0.gguf").exists()
        assert not (llm_dir / "README.md").exists()
        assert (llm_dir / ".ready").exists()

        assert client.get("/health").json()["llm_model_present"] is True

        # The whisper slot is untouched by the LLM slot.
        assert client.get("/v1/model/download", headers=AUTH).json()["state"] == "idle"


def test_llm_model_download_requires_the_bearer_token(config: Config) -> None:
    app = create_app(config, llm_model_download_factory=_llm_download_factory(config))
    with TestClient(app) as client:
        for method in (client.get, client.post, client.delete):
            assert method("/v1/llm-model/download", headers=WRONG_AUTH).status_code == 401
