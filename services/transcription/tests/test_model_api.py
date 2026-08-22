"""HTTP + CLI surface over the model download core (FR-12, FR-17, F2 FR-9/10).

No network, no GPU, no real weights: `FakeHubClient`/`FakeTransport` (the
same seams `test_model_download.py` exercises) stand in for
`huggingface_hub`. HTTP tests use `TestClient` with an injected
`model_download_factory` so `create_app` never touches the real hub; CLI
tests monkeypatch the default `HuggingFaceHubClient`/`HttpTransport`
classes inside `transcription.model_download` so `cli.main` stays
offline too.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transcription import cli
from transcription.app import create_app
from transcription.config import Config
from transcription.model_download import ModelDownload, RemoteFile

AUTH = {"Authorization": "Bearer test-token"}
WRONG_AUTH = {"Authorization": "Bearer wrong-token"}

CONTENT = b"m" * 5_000
SHA = hashlib.sha256(CONTENT).hexdigest()


class FakeHubClient:
    def __init__(self, files: list[RemoteFile]) -> None:
        self.files = files
        self.list_calls = 0

    def list_files(self, repo_id: str, revision: str) -> list[RemoteFile]:
        self.list_calls += 1
        return self.files

    def file_url(self, repo_id: str, revision: str, filename: str) -> str:
        return f"https://fake.example/{repo_id}/{revision}/{filename}"


class FakeTransport:
    """Writes pre-baked content in chunks; `chunk_sleep` lets a test observe
    an in-progress transfer through the HTTP status endpoint."""

    def __init__(
        self, contents: dict[str, bytes], *, chunk_size: int = 500, chunk_sleep: float = 0.0
    ) -> None:
        self.contents = contents
        self.chunk_size = chunk_size
        self.chunk_sleep = chunk_sleep

    def fetch(self, *, url, dest, resume_from, on_chunk, cancel):
        filename = Path(dest).name.removesuffix(".incomplete")
        data = self.contents[filename]
        mode = "ab" if resume_from else "wb"
        with open(dest, mode) as f:
            offset = resume_from
            while offset < len(data):
                if cancel.is_set():
                    return
                if self.chunk_sleep:
                    time.sleep(self.chunk_sleep)
                chunk = data[offset : offset + self.chunk_size]
                f.write(chunk)
                offset += len(chunk)
                on_chunk(len(chunk))


def _factory(models_dir: Path, app_dir: Path, *, chunk_sleep: float = 0.0):
    hub = FakeHubClient([RemoteFile(path="model.bin", size=len(CONTENT), sha256=SHA)])
    transport = FakeTransport({"model.bin": CONTENT}, chunk_sleep=chunk_sleep)

    def factory() -> ModelDownload:
        return ModelDownload(
            models_dir=models_dir,
            allowed_roots=(app_dir,),
            hub_client=hub,
            transport=transport,
        )

    return factory, hub


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        model_path=str(tmp_app_dir / "models"),
        token="test-token",  # noqa: S106 -- test fixture
    )


def test_post_download_returns_202_immediately_without_blocking(
    config: Config, tmp_app_dir: Path
) -> None:
    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir, chunk_sleep=0.05)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    started = time.perf_counter()
    response = client.post("/v1/model/download", headers=AUTH)
    elapsed = time.perf_counter() - started

    assert response.status_code == 202
    assert elapsed < 0.5
    body = response.json()
    assert body["state"] in {"downloading", "idle"}


def test_get_download_status_shape_and_monotonic_progress(
    config: Config, tmp_app_dir: Path
) -> None:
    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir, chunk_sleep=0.02)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    client.post("/v1/model/download", headers=AUTH)

    last_downloaded = -1
    deadline = time.monotonic() + 5.0
    saw_progress = False
    while time.monotonic() < deadline:
        response = client.get("/v1/model/download", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "state",
            "downloaded_bytes",
            "total_bytes",
            "percent",
            "error_kind",
            "error_message",
        }
        assert body["downloaded_bytes"] >= last_downloaded
        if body["downloaded_bytes"] > 0:
            saw_progress = True
        last_downloaded = body["downloaded_bytes"]
        if body["state"] == "complete":
            break
        time.sleep(0.01)
    else:
        raise TimeoutError("download did not complete in time")

    assert saw_progress
    assert last_downloaded == len(CONTENT)


def test_delete_download_cancels_and_status_reads_cancelled_and_is_retryable(
    config: Config, tmp_app_dir: Path
) -> None:
    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir, chunk_sleep=0.05)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    client.post("/v1/model/download", headers=AUTH)
    time.sleep(0.06)  # let the background thread write at least one chunk

    cancel_response = client.delete("/v1/model/download", headers=AUTH)
    assert cancel_response.status_code == 200

    deadline = time.monotonic() + 2.0
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get("/v1/model/download", headers=AUTH).json()
        if body["state"] == "cancelled":
            break
        time.sleep(0.01)
    assert body["state"] == "cancelled"

    # Retryable: a fresh POST starts a brand new (fast, uninterrupted) transfer.
    retry_factory, _retry_hub = _factory(tmp_app_dir / "models", tmp_app_dir)
    app.state.model_download_manager._factory = retry_factory
    retry_response = client.post("/v1/model/download", headers=AUTH)
    assert retry_response.status_code == 202


def test_second_post_while_downloading_does_not_start_a_parallel_transfer(
    config: Config, tmp_app_dir: Path
) -> None:
    factory, hub = _factory(tmp_app_dir / "models", tmp_app_dir, chunk_sleep=0.05)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    first = client.post("/v1/model/download", headers=AUTH)
    second = client.post("/v1/model/download", headers=AUTH)

    assert first.status_code == 202
    assert second.status_code == 202
    assert hub.list_calls == 1


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_model_download_routes_require_bearer_token(
    config: Config, tmp_app_dir: Path, method: str
) -> None:
    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    no_auth = client.request(method, "/v1/model/download")
    assert no_auth.status_code == 401

    wrong_auth = client.request(method, "/v1/model/download", headers=WRONG_AUTH)
    assert wrong_auth.status_code == 401


def test_health_reports_model_absent_before_download_and_present_after(
    config: Config, tmp_app_dir: Path
) -> None:
    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    before = client.get("/health")
    assert before.status_code == 200
    assert before.json()["model_present"] is False

    client.post("/v1/model/download", headers=AUTH)

    deadline = time.monotonic() + 5.0
    present = False
    while time.monotonic() < deadline:
        if client.get("/health").json()["model_present"]:
            present = True
            break
        time.sleep(0.02)

    assert present
    ready_marker = tmp_app_dir / "models" / ".ready"
    assert ready_marker.exists()


def test_health_reports_cuda_runtime_present_only_when_a_gpu_is_on_the_machine(
    config: Config, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E13: `cuda_runtime_present` is `None` on a GPU-less host (never prompt
    about a runtime that machine could never use), and otherwise reports
    `is_cuda_runtime_present()` verbatim."""
    from transcription.api import model_routes

    factory, _hub = _factory(tmp_app_dir / "models", tmp_app_dir)
    app = create_app(config, model_download_factory=factory)
    client = TestClient(app)

    monkeypatch.setattr(model_routes, "_nvidia_gpu_present", lambda: False)
    no_gpu = client.get("/health")
    assert no_gpu.json()["cuda_runtime_present"] is None

    monkeypatch.setattr(model_routes, "_nvidia_gpu_present", lambda: True)
    gpu_no_runtime = client.get("/health")
    assert gpu_no_runtime.json()["cuda_runtime_present"] is False

    runtime_dir = tmp_app_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / ".ready").write_text("{}", encoding="utf-8")
    gpu_with_runtime = client.get("/health")
    assert gpu_with_runtime.json()["cuda_runtime_present"] is True


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[int, str, str]:
    import io

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    exit_code = cli.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_cli_download_model_prints_one_json_object_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_app_dir: Path
) -> None:
    hub = FakeHubClient([RemoteFile(path="model.bin", size=len(CONTENT), sha256=SHA)])
    transport = FakeTransport({"model.bin": CONTENT})
    monkeypatch.setattr("transcription.model_download.HuggingFaceHubClient", lambda: hub)
    monkeypatch.setattr("transcription.model_download.HttpTransport", lambda: transport)

    out_dir = tmp_app_dir / "models"
    config_path = tmp_app_dir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    argv = [
        "download-model",
        "--out",
        str(out_dir),
        "--config",
        str(config_path),
    ]
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))

    exit_code, stdout, stderr = _run_cli(monkeypatch, argv)

    assert exit_code == 0, stderr
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["state"] == "complete"
    assert summary["downloaded_bytes"] == len(CONTENT)
    assert stderr  # progress went to stderr


def test_cli_download_model_rejects_out_dir_outside_app_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_app_dir: Path
) -> None:
    hub = FakeHubClient([RemoteFile(path="model.bin", size=len(CONTENT), sha256=SHA)])
    transport = FakeTransport({"model.bin": CONTENT})
    monkeypatch.setattr("transcription.model_download.HuggingFaceHubClient", lambda: hub)
    monkeypatch.setattr("transcription.model_download.HttpTransport", lambda: transport)

    outside = tmp_app_dir.parent / "elsewhere-models"
    config_path = tmp_app_dir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    argv = ["download-model", "--out", str(outside), "--config", str(config_path)]
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))

    exit_code, stdout, stderr = _run_cli(monkeypatch, argv)

    assert exit_code == cli.EXIT_CODES[cli.ErrorKind.INVALID_REQUEST]
    assert stdout.strip() == ""
    assert stderr.strip() != ""


def test_cli_download_model_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["download-model", "--help"])
    assert excinfo.value.code == 0
