"""Tests for the first-run diarization runtime and model acquisition.

No network, no torch, no real wheels: in-memory archives stand in for the
pinned packages (the `test_cuda_runtime.py` pattern), a fake snapshot
callable stands in for the hub, and the HTTP surface is exercised through
`TestClient` with injected factories.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transcription import diarization_runtime as dr
from transcription.app import create_app
from transcription.config import Config
from transcription.cuda_runtime import CudaPackage, CudaRuntimeDownload
from transcription.diarization_runtime_packages import DIARIZATION_WHEELS, TOTAL_BYTES
from transcription.errors import ErrorKind
from transcription.model_download import DownloadState

AUTH = {"Authorization": "Bearer test-token"}


# -- the pinned manifest ------------------------------------------------------


def test_the_manifest_holds_the_cuda_torch_pair_and_every_file_once() -> None:
    names = {wheel.name for wheel in DIARIZATION_WHEELS}
    assert {"pyannote-audio", "torch", "torchaudio"} <= names
    by_name = {wheel.name: wheel for wheel in DIARIZATION_WHEELS}
    assert by_name["torch"].version.endswith(f"+{dr.RUNTIME_CUDA_VARIANT}")
    assert by_name["torchaudio"].version.endswith(f"+{dr.RUNTIME_CUDA_VARIANT}")
    assert "download.pytorch.org" in by_name["torch"].url
    filenames = [wheel.filename for wheel in DIARIZATION_WHEELS]
    assert len(filenames) == len(set(filenames))
    assert TOTAL_BYTES == sum(wheel.size for wheel in DIARIZATION_WHEELS)
    assert all(len(wheel.sha256) == 64 for wheel in DIARIZATION_WHEELS)


def test_source_tarballs_name_their_importable_root() -> None:
    tarballs = [wheel for wheel in DIARIZATION_WHEELS if wheel.filename.endswith(".tar.gz")]
    assert tarballs, "the manifest is expected to carry the two wheel-less pure-Python packages"
    for wheel in tarballs:
        assert wheel.archive_root.startswith(wheel.filename.removesuffix(".tar.gz") + "/")


def test_packages_land_in_the_diarization_subdir_whole() -> None:
    assert all(pkg.dest_subdir == dr.DIARIZATION_SUBDIR for pkg in dr.DIARIZATION_PACKAGES)
    wheels = [pkg for pkg in dr.DIARIZATION_PACKAGES if pkg.filename.endswith(".whl")]
    assert all(pkg.extract_prefix == "" and pkg.archive_root == "" for pkg in wheels)


# -- extraction --------------------------------------------------------------


class FakeTransport:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents

    def fetch(self, *, url, dest, resume_from, on_chunk, cancel):
        data = self.contents[Path(dest).name.removesuffix(".incomplete")]
        with open(dest, "wb") as f:
            f.write(data)
        on_chunk(len(data))


def _wheel_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _tarball_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _package(filename: str, data: bytes, **overrides: str) -> CudaPackage:
    return CudaPackage(
        name=filename.split("-")[0],
        version="1.0",
        filename=filename,
        url=f"https://fake.example/{filename}",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        extract_prefix=overrides.get("extract_prefix", ""),
        dest_subdir=dr.DIARIZATION_SUBDIR,
        archive_root=overrides.get("archive_root", ""),
    )


def test_whole_wheels_and_rooted_tarballs_extract_into_one_importable_tree(
    tmp_path: Path,
) -> None:
    wheel = _wheel_bytes(
        {
            "pyannote/audio/__init__.py": b"# pyannote",
            "pyannote_audio-1.0.dist-info/METADATA": b"Name: pyannote-audio",
            "pyannote_audio-1.0.data/scripts/pyannote-cli": b"#!/bin/sh",
        }
    )
    tarball = _tarball_bytes(
        {
            "antlr4-1.0/setup.py": b"# not wanted",
            "antlr4-1.0/src/antlr4/__init__.py": b"# antlr",
        }
    )
    packages = (
        _package("pyannote_audio-1.0-py3-none-any.whl", wheel),
        _package("antlr4-1.0.tar.gz", tarball, archive_root="antlr4-1.0/src/"),
    )
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    download = CudaRuntimeDownload(
        app_dir=app_dir,
        allowed_roots=(app_dir,),
        packages=packages,
        transport=FakeTransport(
            {pkg.filename: data for pkg, data in zip(packages, (wheel, tarball), strict=True)}
        ),
        marker_relpath=f"{dr.DIARIZATION_SUBDIR}/.ready",
    )

    download.start(on_progress=lambda _event: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    runtime = dr.diarization_runtime_dir(app_dir)
    assert (runtime / "pyannote" / "audio" / "__init__.py").read_bytes() == b"# pyannote"
    # dist-info travels (importlib.metadata resolves versions through it);
    # `.data/` trees (console scripts) do not.
    assert (runtime / "pyannote_audio-1.0.dist-info" / "METADATA").is_file()
    assert not (runtime / "pyannote_audio-1.0.data").exists()
    # The tarball's `src/` is the tree root; `setup.py` above it is skipped.
    assert (runtime / "antlr4" / "__init__.py").read_bytes() == b"# antlr"
    assert not (runtime / "setup.py").exists()
    assert dr.is_diarization_runtime_present(app_dir)


def test_activate_runtime_puts_a_fetched_tree_on_sys_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    app_dir = tmp_path / "app"
    assert dr.activate_runtime(app_dir) is None
    runtime = dr.diarization_runtime_dir(app_dir)
    runtime.mkdir(parents=True)
    (runtime / ".ready").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert dr.activate_runtime(app_dir) == runtime
    assert dr.activate_runtime(app_dir) == runtime
    assert sys.path.count(str(runtime)) == 1
    assert sys.path[0] == str(runtime)


# -- the models ---------------------------------------------------------------


class FakeHub:
    """Records snapshot calls; can be scripted to raise per repo."""

    def __init__(self, *, fail: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail = fail or {}

    def __call__(self, repo_id: str, revision: str, cache_dir: Path, token: str | None) -> None:
        self.calls.append((repo_id, revision, token))
        if repo_id in self.fail:
            raise self.fail[repo_id]
        snapshot = cache_dir / dr._repo_folder(repo_id) / "snapshots" / revision
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "config.yaml").write_text("fake", encoding="utf-8")


class GatedRepoError(Exception):
    """Same name as `huggingface_hub.errors.GatedRepoError`; the classifier
    matches by name so the hub package need not be imported here."""


def test_model_download_snapshots_every_repo_pins_main_and_marks_ready(tmp_path: Path) -> None:
    hub = FakeHub()
    cache = tmp_path / "models" / "diarization"
    download = dr.DiarizationModelDownload(cache_dir=cache, token="hf_test", snapshot=hub)  # noqa: S106
    events: list[dict[str, object]] = []

    download.start(on_progress=events.append, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    assert [call[0] for call in hub.calls] == [repo.repo_id for repo in dr.DIARIZATION_MODEL_REPOS]
    assert all(token == "hf_test" for _repo, _rev, token in hub.calls)  # noqa: S105
    for repo in dr.DIARIZATION_MODEL_REPOS:
        ref = cache / dr._repo_folder(repo.repo_id) / "refs" / "main"
        assert ref.read_text(encoding="utf-8") == repo.revision
    assert dr.is_diarization_model_present(tmp_path)
    assert events[-1]["state"] == "complete"
    assert events[-1]["percent"] == 100.0


def test_a_present_marker_short_circuits_without_a_hub_call(tmp_path: Path) -> None:
    hub = FakeHub()
    cache = tmp_path / "models" / "diarization"
    dr.DiarizationModelDownload(cache_dir=cache, token="hf_test", snapshot=hub).start(  # noqa: S106
        on_progress=lambda _e: None
    )
    again = dr.DiarizationModelDownload(cache_dir=cache, token=None, snapshot=hub)

    again.start(on_progress=lambda _e: None)

    assert again.state == DownloadState.COMPLETE
    assert len(hub.calls) == len(dr.DIARIZATION_MODEL_REPOS)


def test_a_repinned_build_no_longer_counts_old_snapshots_as_present(tmp_path: Path) -> None:
    cache = tmp_path / "models" / "diarization"
    cache.mkdir(parents=True)
    (cache / ".ready").write_text(json.dumps({"repos": {"pyannote/old": "abc"}}), encoding="utf-8")

    assert not dr.is_diarization_model_present(tmp_path)


def test_without_a_token_the_gated_repos_are_refused_up_front(tmp_path: Path) -> None:
    hub = FakeHub()
    download = dr.DiarizationModelDownload(cache_dir=tmp_path / "m", token=None, snapshot=hub)

    download.start(on_progress=lambda _e: None)

    assert download.state == DownloadState.ERROR
    assert download.error is not None
    assert download.error.kind is ErrorKind.MODEL_LOAD
    assert "huggingface.co/pyannote/speaker-diarization-3.1" in download.error.message
    assert hub.calls == []


def test_a_gated_refusal_names_the_repo_whose_terms_to_accept(tmp_path: Path) -> None:
    hub = FakeHub(fail={"pyannote/segmentation-3.0": GatedRepoError("401 Client Error")})
    download = dr.DiarizationModelDownload(
        cache_dir=tmp_path / "m",
        token="hf_test",  # noqa: S106
        snapshot=hub,
    )

    download.start(on_progress=lambda _e: None)

    assert download.state == DownloadState.ERROR
    assert download.error is not None
    assert download.error.kind is ErrorKind.MODEL_LOAD
    assert "https://huggingface.co/pyannote/segmentation-3.0" in download.error.message
    assert not dr.is_diarization_model_present(tmp_path)


def test_a_network_failure_is_a_retryable_transfer_error(tmp_path: Path) -> None:
    hub = FakeHub(fail={"pyannote/speaker-diarization-3.1": ConnectionError("dns")})
    download = dr.DiarizationModelDownload(
        cache_dir=tmp_path / "m",
        token="hf_test",  # noqa: S106
        snapshot=hub,
    )

    download.start(on_progress=lambda _e: None)

    assert download.state == DownloadState.ERROR
    assert download.error is not None
    assert download.error.kind is ErrorKind.PROVIDER_UNAVAILABLE
    assert download.error.retryable


def test_cancel_stops_between_repos(tmp_path: Path) -> None:
    hub = FakeHub()
    calls: list[str] = []

    def snapshot(repo_id: str, revision: str, cache_dir: Path, token: str | None) -> None:
        hub(repo_id, revision, cache_dir, token)
        calls.append(repo_id)
        # Cancelled while the first repo is in flight: that snapshot runs
        # to its end, and nothing after it starts.
        download.cancel()

    download = dr.DiarizationModelDownload(
        cache_dir=tmp_path / "m",
        token="hf_test",  # noqa: S106
        snapshot=snapshot,
    )

    download.start(on_progress=lambda _e: None)

    assert download.state == DownloadState.CANCELLED
    assert calls == [dr.DIARIZATION_MODEL_REPOS[0].repo_id]
    assert not dr.is_diarization_model_present(tmp_path)


def test_hub_offline_flips_the_flag_only_for_the_duration() -> None:
    constants = pytest.importorskip("huggingface_hub.constants")
    before = constants.HF_HUB_OFFLINE
    with dr.hub_offline(True):
        assert constants.HF_HUB_OFFLINE is True
    assert constants.HF_HUB_OFFLINE == before
    with dr.hub_offline(False):
        assert constants.HF_HUB_OFFLINE == before


# -- status -------------------------------------------------------------------


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106
        hf_token="hf_test",  # noqa: S106
        diarize=True,
    )


def test_status_reports_every_prerequisite(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr, "gpu_present", lambda: True)
    monkeypatch.setattr(dr, "pyannote_importable", lambda: False)

    status = dr.diarization_status(config)

    assert status == {
        "runtime_present": False,
        "model_present": False,
        "token_present": True,
        "enabled": True,
        "gpu_present": True,
        "runtime_total_bytes": TOTAL_BYTES,
    }

    runtime = dr.diarization_runtime_dir(config.app_dir)
    runtime.mkdir(parents=True)
    (runtime / ".ready").write_text("{}", encoding="utf-8")
    assert dr.diarization_status(config)["runtime_present"] is True


def test_status_route_is_token_guarded_and_mirrors_the_dict(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dr, "gpu_present", lambda: False)
    from fakes import FakeProvider

    from transcription import providers

    providers.register("fake", FakeProvider)
    with TestClient(create_app(config)) as client:
        assert client.get("/v1/diarization/status").status_code == 401
        response = client.get("/v1/diarization/status", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["token_present"] is True
    assert body["enabled"] is True
    assert body["gpu_present"] is False
    assert body["runtime_total_bytes"] == TOTAL_BYTES


def test_the_two_download_slots_answer_on_their_own_routes(config: Config, tmp_path: Path) -> None:
    from fakes import FakeProvider

    from transcription import providers

    providers.register("fake", FakeProvider)
    hub = FakeHub()
    app = create_app(
        config,
        diarization_model_download_factory=lambda: dr.DiarizationModelDownload(
            cache_dir=dr.diarization_cache_dir(config.app_dir),
            token=config.hf_token,
            snapshot=hub,
        ),
    )
    with TestClient(app) as client:
        idle = client.get("/v1/diarization-runtime/download", headers=AUTH)
        assert idle.status_code == 200
        assert idle.json()["state"] == "idle"

        started = client.post("/v1/diarization-model/download", headers=AUTH)
        assert started.status_code == 202
        deadline = 50
        while deadline:
            status = client.get("/v1/diarization-model/download", headers=AUTH).json()
            if status["state"] == "complete":
                break
            deadline -= 1
            import time

            time.sleep(0.02)
        assert status["state"] == "complete"
        assert client.get("/v1/diarization/status", headers=AUTH).json()["model_present"] is True


# -- the CLI (what the release build runs to bake the models) -----------------


def test_the_cli_bakes_the_snapshots_into_out_with_the_env_token(
    tmp_app_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from transcription import cli

    hub = FakeHub()
    monkeypatch.setattr(dr, "_hub_snapshot", hub)
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))
    monkeypatch.setenv("HF_TOKEN", "hf_from_env")
    out = tmp_path / "bundle" / "models" / "diarization"

    code = cli.main(["download-diarization-models", "--out", str(out)])

    assert code == 0
    assert [call[2] for call in hub.calls] == ["hf_from_env"] * len(dr.DIARIZATION_MODEL_REPOS)
    assert (out / ".ready").is_file()
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["state"] == "complete"
    assert summary["models_dir"] == str(out)


def test_the_cli_fails_with_the_model_load_code_without_a_token(
    tmp_app_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from transcription import cli
    from transcription.cli import EXIT_CODES

    monkeypatch.setattr(dr, "_hub_snapshot", FakeHub())
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))
    for name in ("HF_TOKEN", "TRANSCRIBER_HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    code = cli.main(["download-diarization-models", "--out", str(tmp_path / "m")])

    assert code == EXIT_CODES[ErrorKind.MODEL_LOAD]
    assert "huggingface.co/settings/tokens" in capsys.readouterr().err
