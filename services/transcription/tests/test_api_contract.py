# Test pattern adapted from Vexa (Vexa-ai/vexa), Apache-2.0 -- origin:
# core/meetings/services/transcription/tests/conftest.py
"""HTTP contract tests: auth, health, validation (FR-2, FR-8, FR-9).

Adapts vexa's GPU-free pattern: the app is built with a config pointing at
`tmp_app_dir`, `TestClient(app)` is used **without** the `with` block so
lifespan never runs (no ledger reconcile, no worker started) -- this file
exercises only the parts of the contract that do not require the job to
actually execute. `FakeProvider` is registered so no model/GPU/network is
ever touched (FR-15).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient

from transcription import providers
from transcription.app import create_app
from transcription.config import Config
from transcription.ledger import Ledger

AUTH = {"Authorization": "Bearer test-token"}
WRONG_AUTH = {"Authorization": "Bearer wrong-token"}


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    providers.register("fake", FakeProvider)
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def client(config: Config) -> TestClient:
    app = create_app(config)
    # Deliberately no `with` block: lifespan never runs here (see module docstring).
    return TestClient(app)


@pytest.fixture
def valid_job_body(tmp_app_dir: Path) -> dict[str, str]:
    audio = tmp_app_dir / "audio.wav"
    audio.write_bytes(b"fake-audio-bytes")
    return {"audio_path": str(audio), "output_dir": str(tmp_app_dir / "out")}


def test_health_returns_ok_with_unloaded_model_state_before_any_job(
    client: TestClient, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # E13: pin the GPU-presence probe so this shape assertion is not
    # machine-dependent (a real `nvidia-smi` on this test runner's `PATH`
    # would otherwise flip `cuda_runtime_present` from `None` to `False`).
    from transcription.api import model_routes

    monkeypatch.setattr(model_routes, "_nvidia_gpu_present", lambda: False)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    # Before anything has resolved the provider (no job has run, and
    # `/health` itself must never construct -- let alone import -- one:
    # E15), `/health` reports the *unresolved* config literals ("large-v3"/
    # "auto", this fixture's defaults beyond `provider`) with
    # `model_state: "unloaded"` -- still correct, since nothing has loaded
    # anything yet. Once a job actually runs (see test_api_jobs.py), the
    # provider is cached and subsequent calls report its live, resolved
    # `describe()` instead. `model_present` is windows-installer-build T11's
    # addition (FR-17): no model has been downloaded into this fixture's
    # `tmp_app_dir`, so it reads `False`. `cuda_runtime_present` is E13's
    # addition: `None` on this GPU-less-probed host.
    assert body == {
        "status": "ok",
        "version": "0.1.0",
        "provider": "fake",
        "model": "large-v3",
        "device": "auto",
        "model_state": "unloaded",
        "model_present": False,
        "cuda_runtime_present": None,
    }


def test_health_never_imports_a_provider_library(tmp_app_dir: Path) -> None:
    """E15: `/health` must never pay for a `faster_whisper`/`litellm`
    import -- not even on its very first call, and not even when the
    *real* `local` registry entry (not a fake) is configured."""
    for name in ("faster_whisper", "transcription.providers.local_whisper"):
        sys.modules.pop(name, None)

    real_config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="local",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )
    app = create_app(real_config)
    real_client = TestClient(app)  # no lifespan -- see module docstring

    response = real_client.get("/health")

    assert response.status_code == 200
    assert response.json()["model_state"] == "unloaded"
    assert "faster_whisper" not in sys.modules
    assert "transcription.providers.local_whisper" not in sys.modules


def test_submit_stays_under_budget_with_the_real_provider_registry(
    tmp_app_dir: Path, valid_job_body: dict[str, str]
) -> None:
    """E15: `POST /v1/jobs`'s 200 ms budget (FR-2 acceptance) must hold even
    for the very first submission against the *real* `local` provider --
    resolving/importing it must never sit on this request's path."""
    real_config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="local",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )
    app = create_app(real_config)

    with TestClient(app) as real_client:
        started = time.perf_counter()
        response = real_client.post("/v1/jobs", json=valid_job_body, headers=AUTH)
        elapsed = time.perf_counter() - started

    assert response.status_code == 202, response.text
    assert elapsed < 0.2


def test_health_body_contains_no_token_and_no_api_key(client: TestClient, config: Config) -> None:
    response = client.get("/health")

    body_text = response.text
    assert config.token not in body_text
    assert "token" not in response.json()
    assert "api_key" not in response.json()


def test_health_is_reachable_without_a_token(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs"),
        ("GET", "/v1/jobs/does-not-exist"),
        ("GET", "/v1/jobs/does-not-exist/result"),
        ("DELETE", "/v1/jobs/does-not-exist"),
    ],
)
def test_v1_routes_require_bearer_token(
    client: TestClient, method: str, path: str, valid_job_body: dict[str, str]
) -> None:
    json_body = valid_job_body if method == "POST" else None

    no_auth = client.request(method, path, json=json_body)
    assert no_auth.status_code == 401

    wrong_auth = client.request(method, path, json=json_body, headers=WRONG_AUTH)
    assert wrong_auth.status_code == 401

    right_auth = client.request(method, path, json=json_body, headers=AUTH)
    assert right_auth.status_code != 401


def test_bearer_token_comparison_uses_constant_time_compare(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E12: token comparison must go through `secrets.compare_digest`, not a
    short-circuiting `!=`."""
    from transcription import app as app_module

    calls: list[tuple[str, str]] = []
    original = app_module.secrets.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(app_module.secrets, "compare_digest", spy)

    response = client.get("/v1/jobs", headers=AUTH)

    assert response.status_code == 200
    assert calls  # `compare_digest` was actually invoked


def test_non_ascii_authorization_header_returns_401_not_500(client: TestClient) -> None:
    """E16: Starlette decodes header values as latin-1, so a raw non-ASCII
    byte sequence in `Authorization` reaches `require_token` as a non-ASCII
    `str`. `secrets.compare_digest(str, str)` raises `TypeError` outside
    ASCII -- any wrong token, ASCII or not, must still 401, never crash to
    a 500."""
    response = client.get(
        "/v1/jobs",
        headers=[(b"authorization", b"Bearer \xe9\xe9")],
    )

    assert response.status_code == 401


def test_post_job_with_traversal_path_returns_400_invalid_request(
    client: TestClient, tmp_app_dir: Path
) -> None:
    body = {
        "audio_path": r"..\..\Windows\System32\config\SAM",
        "output_dir": str(tmp_app_dir / "out"),
    }

    response = client.post("/v1/jobs", json=body, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error_kind"] == "invalid_request"


def test_post_job_with_traversal_path_creates_no_ledger_row(
    client: TestClient, config: Config, tmp_app_dir: Path
) -> None:
    body = {
        "audio_path": r"..\..\Windows\System32\config\SAM",
        "output_dir": str(tmp_app_dir / "out"),
    }

    client.post("/v1/jobs", json=body, headers=AUTH)

    ledger = Ledger(config.db_path)
    try:
        assert ledger.list_jobs() == []
    finally:
        ledger.close()


def test_post_job_malformed_body_returns_400_never_500(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"output_dir": "x"}, headers=AUTH)

    assert response.status_code in (400, 422)
    assert response.status_code != 500
    assert response.json()["error_kind"] == "invalid_request"


def test_get_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/v1/jobs/does-not-exist", headers=AUTH)

    assert response.status_code == 404


def test_app_is_created_by_a_factory_with_no_module_level_instance() -> None:
    import transcription.app as app_module

    assert hasattr(app_module, "create_app")
    assert not hasattr(app_module, "app")
