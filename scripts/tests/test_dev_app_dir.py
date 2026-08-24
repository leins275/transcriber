"""Tests for scripts/dev_app_dir.py.

Self-contained by design: there is deliberately no scripts/tests/conftest.py
(see plan.md), so this module derives the repo root itself.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev_app_dir.py"

_spec = importlib.util.spec_from_file_location("dev_app_dir", SCRIPT)
dev_app_dir = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["dev_app_dir"] = dev_app_dir
_spec.loader.exec_module(dev_app_dir)


def _fake_payload(root: Path) -> Path:
    """A staged engine payload, in the shape stage_engine_payload.py leaves."""
    staged = root / "staged"
    (staged / "runtime" / "engine" / "backends").mkdir(parents=True)
    (staged / "payload-manifest.json").write_text("{}", encoding="utf-8")
    (staged / "whisper.dll").write_bytes(b"dll")
    (staged / "runtime" / "engine" / "backends" / "ggml-cpu-x64.dll").write_bytes(b"dll")
    (staged / ".gitkeep").write_bytes(b"")
    return staged


def _fake_weights(root: Path) -> Path:
    weights = root / "weights"
    weights.mkdir()
    (weights / dev_app_dir.WHISPER_WEIGHTS).write_bytes(b"w")
    (weights / dev_app_dir.WHISPER_VAD).write_bytes(b"v")
    return weights


def test_ready_marker_sits_beside_the_payload_not_in_the_directory(tmp_path):
    """The Rust engine's `models::ready_marker` appends `.ready` to the file's
    own path. The Python service wrote a single bare `.ready` per directory
    instead, so getting this wrong silently re-downloads the model."""
    payload = tmp_path / "ggml-large-v3.bin"
    payload.write_bytes(b"x")

    dev_app_dir.mark_ready(payload)

    assert (tmp_path / "ggml-large-v3.bin.ready").is_file()
    assert not (tmp_path / ".ready").exists()


def test_build_links_payload_and_weights_and_marks_them_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_app_dir, "STAGED_PAYLOAD", _fake_payload(tmp_path))
    weights = _fake_weights(tmp_path)
    dev = tmp_path / "devapp"

    dev_app_dir.build(dev, weights, None)

    assert (dev / "whisper.dll").is_file()
    assert (dev / "runtime/engine/backends/ggml-cpu-x64.dll").is_file()
    # `.gitkeep` is staging bookkeeping, not payload.
    assert not (dev / ".gitkeep").exists()
    for name in (dev_app_dir.WHISPER_WEIGHTS, dev_app_dir.WHISPER_VAD):
        assert (dev / "models/whisper" / name).is_file()
        assert (dev / "models/whisper" / f"{name}.ready").is_file()


def test_build_is_idempotent_and_survives_the_source_disappearing(tmp_path, monkeypatch):
    """Re-running after the weights moved must not wipe what is already
    linked -- `make dev` calls this on every run."""
    monkeypatch.setattr(dev_app_dir, "STAGED_PAYLOAD", _fake_payload(tmp_path))
    weights = _fake_weights(tmp_path)
    dev = tmp_path / "devapp"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv(dev_app_dir.WHISPER_SRC_ENV, raising=False)

    dev_app_dir.build(dev, weights, None)
    for name in (dev_app_dir.WHISPER_WEIGHTS, dev_app_dir.WHISPER_VAD):
        (weights / name).unlink()

    notes = dev_app_dir.build(dev, None, None)

    assert any("already present" in note for note in notes)
    for name in (dev_app_dir.WHISPER_WEIGHTS, dev_app_dir.WHISPER_VAD):
        assert (dev / "models/whisper" / name).is_file()


def test_missing_vad_model_is_an_error_not_a_silent_skip(tmp_path, monkeypatch):
    """Without the VAD model whisper.cpp transcribes silence -- measured as
    73.5% word agreement against 85.7% with it. Never proceed quietly."""
    monkeypatch.setattr(dev_app_dir, "STAGED_PAYLOAD", _fake_payload(tmp_path))
    weights = _fake_weights(tmp_path)
    (weights / dev_app_dir.WHISPER_VAD).unlink()

    with pytest.raises(dev_app_dir.DevAppDirError, match="not optional"):
        dev_app_dir.build(tmp_path / "devapp", weights, None)


def test_missing_staged_payload_names_the_script_that_produces_it(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_app_dir, "STAGED_PAYLOAD", tmp_path / "absent")

    with pytest.raises(dev_app_dir.DevAppDirError, match="stage_engine_payload"):
        dev_app_dir.build(tmp_path / "devapp", _fake_weights(tmp_path), None)


def test_llm_is_marked_ready_in_place_never_copied(tmp_path, monkeypatch):
    """The GGUF is 20 GB and usually on another volume. It stays put; only a
    zero-byte marker is written beside it."""
    monkeypatch.setattr(dev_app_dir, "STAGED_PAYLOAD", _fake_payload(tmp_path))
    llm = tmp_path / "llm"
    llm.mkdir()
    (llm / dev_app_dir.LLM_GGUF).write_bytes(b"gguf")
    dev = tmp_path / "devapp"

    dev_app_dir.build(dev, _fake_weights(tmp_path), llm)

    assert (llm / f"{dev_app_dir.LLM_GGUF}.ready").is_file()
    assert not (dev / "models" / "llm").exists()


def test_env_exports_name_the_dev_dir_and_the_out_of_tree_llm(tmp_path):
    env = dev_app_dir.env_exports(tmp_path / "devapp", tmp_path / "llm")

    assert env["TRANSCRIBER_DEV_APP_DIR"] == str(tmp_path / "devapp")
    # The desktop resolves its own app dir and injects TRANSCRIBER_APP_DIR for
    # the engine; both have to agree or the two halves disagree about where
    # models live.
    assert env["TRANSCRIBER_APP_DIR"] == env["TRANSCRIBER_DEV_APP_DIR"]
    assert env["TRANSCRIBER_LLM_MODEL_PATH"] == str(tmp_path / "llm")


def test_env_exports_omits_the_llm_when_there_is_none(tmp_path):
    env = dev_app_dir.env_exports(tmp_path / "devapp", None)

    assert "TRANSCRIBER_LLM_MODEL_PATH" not in env


def test_no_personal_paths_are_baked_into_the_script():
    """A default that only resolves on one machine is worse than an error:
    it works for the author and fails for everyone else."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "D:/Local" not in source
    assert "D:\\\\Local" not in source
