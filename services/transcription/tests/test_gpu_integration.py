"""Opt-in GPU integration tests (FR-3, NFR-2, NFR-3, FR-15; F2 FR-1, FR-4).

The `@pytest.mark.gpu` tests in this suite: real end-to-end transcriptions
against the real model weights on a CUDA device, using the real
`LocalWhisperProvider` (no fakes, no monkeypatching of the model or the CUDA
probe). Deselected by default (`addopts = -m "not gpu"`); every one of them
self-skips cleanly here when no sample is configured -- they never download a
model or require CUDA to be present for the default suite to pass.

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


def _resolve_language_sample(language: str) -> Path:
    """A speech sample known to be spoken in ``language`` (F2 FR-1).

    Configured out of band -- ``TRANSCRIBER_TEST_SAMPLE_EN`` /
    ``TRANSCRIBER_TEST_SAMPLE_RU``, or ``tests/data/sample-<lang>.wav`` -- so
    no audio is ever committed to the repository.
    """
    env_name = f"TRANSCRIBER_TEST_SAMPLE_{language.upper()}"
    env_sample = os.environ.get(env_name)
    if env_sample:
        candidate = Path(env_sample)
        if not candidate.is_file():
            pytest.skip(f"{env_name}={env_sample!r} does not point at a file")
        return candidate

    default = Path(__file__).parent / "data" / f"sample-{language}.wav"
    if default.is_file():
        return default

    pytest.skip(
        f"no {language} speech sample configured: set {env_name} or drop a short wav at "
        f"tests/data/sample-{language}.wav -- see tests/data/README.md"
    )


def _script_counts(text: str) -> tuple[int, int]:
    """(latin letters, cyrillic letters) in ``text``.

    A script census is how "English text" / "Russian text" is asserted
    without pinning the fixture's wording: a Russian decode of English speech
    (the bug F2 fixes) is overwhelmingly Cyrillic, and vice versa.
    """
    latin = sum(1 for char in text if "a" <= char.lower() <= "z")
    cyrillic = sum(1 for char in text if "а" <= char.lower() <= "я")
    return latin, cyrillic


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


@pytest.mark.parametrize("expected_language", ["en", "ru"])
async def test_auto_detection_decodes_in_the_spoken_language_on_cuda(
    tmp_app_dir: Path, expected_language: str
) -> None:
    """Real speech, no requested language: the decode language is the spoken
    one and it is recorded everywhere (F2 FR-1 acceptance bullet 1, FR-4).

    This is the regression the feature exists for: before the constrained
    detection pass, an English recording could be -- and was -- decoded as
    Russian, because faster-whisper free-detected over its full language set.
    """
    sample = _resolve_language_sample(expected_language)

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

    try:
        output_dir = tmp_app_dir / f"out-{expected_language}"
        output_dir.mkdir()
        # No `language=`: the request carries nothing, exactly like the
        # desktop app's default "Auto" control.
        job_id = await manager.submit(audio_path=str(sample), output_dir=str(output_dir))
        await _wait_for_terminal(manager, job_id, timeout_sec=600)

        status = manager.status(job_id)
        assert status.status == "succeeded", status.error_message

        doc = TranscriptDoc.model_validate(
            json.loads((output_dir / "transcript.json").read_text("utf-8"))
        )
        assert doc.language == expected_language
        # FR-4: a constrained-detection probability, not an empty field.
        assert doc.language_probability is not None
        assert doc.language_probability > 0.0

        latin, cyrillic = _script_counts(doc.text)
        assert latin + cyrillic > 0, "no decoded text to judge the language of"
        if expected_language == "en":
            assert latin > cyrillic, f"expected English text, got {doc.text[:200]!r}"
        else:
            assert cyrillic > latin, f"expected Russian text, got {doc.text[:200]!r}"

        row = ledger.get_job(job_id)
        assert row is not None
        assert row["status"] == "succeeded"
        # The row went in with NULL (nothing was requested) and must be
        # updated to the language the decode actually ran in (FR-4).
        assert row["language"] == expected_language
    finally:
        await manager.aclose()
        ledger.close()
