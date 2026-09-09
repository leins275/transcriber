"""Per-call speaker bounds on the diarization engine (FR-1, T1).

A job may cap how many voices pyannote is allowed to find; the shell derives
that cap from the project roster and the service only ever sees the number.
What is observable at this seam is the kwargs the (stubbed) pyannote pipeline
is called with, so that is what these tests assert -- the same stubs
`tests/test_diarizer.py` already wires, imported rather than copied, so the
real pyannote/torch stack is never touched (NFR-1).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_diarizer import FakePipeline, FakePipelineClass, _config, _wire

from transcription.diarizer import PyannoteDiarizer
from transcription.providers.base import CancelToken


def test_a_per_call_max_speakers_is_the_only_bound_the_pipeline_receives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken(), max_speakers=3)

    assert pipeline.calls == [("meeting.wav", {"max_speakers": 3})]


def test_a_per_call_bound_overrides_its_config_key_and_leaves_the_other_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config(diarization_min_speakers=2, diarization_max_speakers=6))
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken(), max_speakers=3)

    assert pipeline.calls == [("meeting.wav", {"min_speakers": 2, "max_speakers": 3})]


def test_both_per_call_bounds_reach_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken(), min_speakers=1, max_speakers=1)

    assert pipeline.calls == [("meeting.wav", {"min_speakers": 1, "max_speakers": 1})]


def test_without_per_call_bounds_the_config_bounds_still_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config(diarization_min_speakers=2, diarization_max_speakers=4))
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert pipeline.calls == [("meeting.wav", {"min_speakers": 2, "max_speakers": 4})]


def test_a_pipeline_without_embedding_support_receives_the_bounds_on_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The `return_embeddings=True` call raises TypeError before the fake
    # records it, so the one recorded call *is* the retry.
    pipeline = FakePipeline(tracks=[(0.0, 2.0, "SPEAKER_00")], supports_embeddings=False)
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken(), max_speakers=2)

    assert pipeline.calls == [("meeting.wav", {"max_speakers": 2})]
