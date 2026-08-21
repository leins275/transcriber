"""Opt-in GPU integration test (FR-3, NFR-2, NFR-3, FR-15).

The one `@pytest.mark.gpu` test in this suite: a real end-to-end
transcription against the real model weights on a CUDA device, using the
real `LocalWhisperProvider` (no fakes, no monkeypatching of the model or the
CUDA probe). Deselected by default (`addopts = -m "not gpu"`); self-skips
cleanly here when no sample is configured -- it never downloads a model or
requires CUDA to be present for the default suite to pass.

Run explicitly with a real sample and a CUDA-capable machine:

    uv run pytest -m gpu

See ``tests/data/README.md`` for how to point at a sample.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from transcription.config import Config
from transcription.jobs import JobManager
from transcription.ledger import Ledger
from transcription.schema import TranscriptDoc

pytestmark = pytest.mark.gpu


def _resolve_sample() -> Path:
    env_sample = os.environ.get("TRANSCRIBER_TEST_SAMPLE")
    if env_sample:
        candidate = Path(env_sample)
        if not candidate.is_file():
            pytest.skip(f"TRANSCRIBER_TEST_SAMPLE={env_sample!r} does not point at a file")
        return candidate

    default = Path(__file__).parent / "data" / "sample.wav"
    if default.is_file():
        return default

    pytest.skip(
        "no GPU sample configured: set TRANSCRIBER_TEST_SAMPLE or drop a short wav at "
        "tests/data/sample.wav -- see tests/data/README.md"
    )


async def _wait_for_terminal(manager: JobManager, job_id: str, *, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if status.status in {"succeeded", "failed", "cancelled"}:
            return
        await asyncio.sleep(0.25)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout_sec}s")


async def test_real_local_transcription_end_to_end_on_cuda(tmp_app_dir: Path) -> None:
    sample = _resolve_sample()

    config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        device="cuda",
        model=os.environ.get("TRANSCRIBER_TEST_MODEL", "large-v3"),
        model_path=os.environ.get("TRANSCRIBER_MODEL_PATH", str(tmp_app_dir / "models")),
        allowed_roots=(str(sample.parent), str(tmp_app_dir)),
    )

    ledger = Ledger(tmp_app_dir / "data" / "jobs.sqlite3")
    manager = JobManager(config, ledger)
    await manager.start()

    # Count real model constructions across two jobs without preventing the
    # real construction -- proves the second job logs no model-load event
    # (FR-3 acceptance) while still exercising the real provider end to end.
    import transcription.providers.local_whisper as local_whisper_mod

    construction_count = 0
    real_whisper_model = local_whisper_mod.WhisperModel

    class _CountingWhisperModel(real_whisper_model):  # type: ignore[misc,valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal construction_count
            construction_count += 1
            super().__init__(*args, **kwargs)

    local_whisper_mod.WhisperModel = _CountingWhisperModel  # type: ignore[misc]

    try:
        output_dir_1 = tmp_app_dir / "out-1"
        output_dir_1.mkdir()
        job_id_1 = await manager.submit(audio_path=str(sample), output_dir=str(output_dir_1))
        await _wait_for_terminal(manager, job_id_1, timeout_sec=300)

        status_1 = manager.status(job_id_1)
        assert status_1.status == "succeeded", status_1.error_message

        transcript_path = output_dir_1 / "transcript.json"
        assert transcript_path.is_file()
        doc = TranscriptDoc.model_validate(json.loads(transcript_path.read_text("utf-8")))
        assert doc.provider.device == "cuda"
        assert doc.stats.realtime_factor > 0

        row_1 = ledger.get_job(job_id_1)
        assert row_1 is not None
        assert row_1["status"] == "succeeded"
        assert row_1["cost_usd"] is None
        assert construction_count == 1

        # Second job, same process: the provider instance is cached by the
        # job manager, so the model must not be constructed a second time.
        output_dir_2 = tmp_app_dir / "out-2"
        output_dir_2.mkdir()
        job_id_2 = await manager.submit(audio_path=str(sample), output_dir=str(output_dir_2))
        await _wait_for_terminal(manager, job_id_2, timeout_sec=300)

        status_2 = manager.status(job_id_2)
        assert status_2.status == "succeeded", status_2.error_message
        assert construction_count == 1, "second job re-constructed the model"
    finally:
        local_whisper_mod.WhisperModel = real_whisper_model  # type: ignore[misc]
        await manager.aclose()
        ledger.close()
