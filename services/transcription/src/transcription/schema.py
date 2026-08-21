"""Pydantic models for the transcript document (FR-6) and the HTTP API (FR-2).

These models are pure data shapes: no filesystem or network access, no
imports from the rest of the package other than ``errors.ErrorKind`` (FR-8).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer

from transcription.errors import ErrorKind

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ModelState = Literal["unloaded", "loading", "loaded"]


class Segment(BaseModel):
    """One transcript segment, in vexa's ``verbose_json`` mapper shape (FR-6).

    The three confidence fields are ``Optional``: the local provider always
    populates them, but a cloud STT provider legitimately omits them when
    the upstream API doesn't return them -- they must never be fabricated
    (FR-6, FR-4 acceptance: cloud and local jobs share one schema).
    """

    id: int
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    words: list[dict[str, Any]] | None = None

    @model_serializer(mode="wrap")
    def _drop_words_when_absent(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("words") is None:
            data.pop("words", None)
        return data


class Source(BaseModel):
    path: str
    filename: str
    duration_sec: float


class ProviderInfo(BaseModel):
    name: str
    model: str
    device: str
    compute_type: str


class Stats(BaseModel):
    elapsed_sec: float
    realtime_factor: float
    cost_usd: float | None
    currency: str | None


class TranscriptDoc(BaseModel):
    """The ``transcript.json`` v1 document (FR-6)."""

    schema_version: Literal[1] = 1
    created_at: str
    source: Source
    provider: ProviderInfo
    language: str | None
    language_probability: float | None
    text: str
    segments: list[Segment]
    stats: Stats


class JobCreate(BaseModel):
    """``POST /v1/jobs`` request body (FR-2)."""

    audio_path: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    language: str | None = None
    provider: str | None = None
    model: str | None = None
    meeting: dict[str, Any] | None = None


class JobStatus(BaseModel):
    """``GET /v1/jobs/{id}`` response body (FR-2, FR-8)."""

    job_id: str
    status: JobState
    progress: float
    elapsed_sec: float | None = None
    audio_duration_sec: float | None = None
    provider: str | None = None
    cost_usd: float | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None

    @field_validator("progress")
    @classmethod
    def _clamp_progress(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class Health(BaseModel):
    """``GET /health`` response body (FR-2)."""

    status: str
    version: str
    provider: str
    model: str
    device: str
    model_state: ModelState
