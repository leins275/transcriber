"""Pydantic models for the transcript document (FR-6) and the HTTP API (FR-2).

These models are pure data shapes: no filesystem or network access, no
imports from the rest of the package other than ``errors.ErrorKind`` (FR-8).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator

from transcription.errors import ErrorKind

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ModelState = Literal["unloaded", "loading", "loaded"]

# The job-type discriminator. `transcribe` is the original (and default) job;
# the rest are the derived-knowledge jobs added by the LLM feature:
# `summarize`/`action_items`/`facts` read a meeting folder's transcript.json,
# `report` reads a whole project folder, and `export` deterministically
# assembles one meeting's existing materials into a PDF (no LLM call).
JobType = Literal["transcribe", "summarize", "action_items", "facts", "report", "export"]


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
    # Set by the diarization pass (FR: speaker classification); `None` both
    # when diarization was off and when no turn could claim the segment.
    speaker: str | None = None

    @model_serializer(mode="wrap")
    def _drop_absent_optionals(self, handler: Any) -> dict[str, Any]:
        # `words` and `speaker` are omitted (not nulled) when absent so a
        # transcript produced with the features off is byte-identical to one
        # produced before they existed.
        data: dict[str, Any] = handler(self)
        for key in ("words", "speaker"):
            if data.get(key) is None:
                data.pop(key, None)
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


class DiarizationInfo(BaseModel):
    """What the transcript records about its diarization pass.

    ``status: "failed"`` is a real, documented state: diarization degrades
    rather than failing the whole job (the transcript is still valuable
    without speakers), and this block is where the failure is attributed so
    it never degrades *silently*.
    """

    status: Literal["succeeded", "failed"]
    model: str
    device: str | None = None
    speaker_count: int | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None


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
    # Present only when diarization ran (succeeded or failed); omitted from
    # the serialized document otherwise, keeping pre-feature readers'
    # documents unchanged.
    diarization: DiarizationInfo | None = None

    @model_serializer(mode="wrap")
    def _drop_diarization_when_absent(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("diarization") is None:
            data.pop("diarization", None)
        return data


class JobCreate(BaseModel):
    """``POST /v1/jobs`` request body (FR-2).

    A ``transcribe`` job (the default, so pre-feature clients are untouched)
    takes ``audio_path``; every other job type takes ``input_path`` -- the
    meeting folder (``summarize``/``action_items``/``facts``/``export``) or
    the project folder (``report``) it reads. Exactly one of the two must be
    supplied, matching the job type.
    """

    job_type: JobType = "transcribe"
    audio_path: str | None = Field(default=None, min_length=1)
    input_path: str | None = Field(default=None, min_length=1)
    output_dir: str = Field(min_length=1)
    # The operator's language universe is exactly {ru, en} (FR-3); anything
    # else -- including `""` -- is rejected here, before `JobManager.submit`,
    # so an invalid request never leaves a ledger row behind (NFR-3).
    # `None` (or an omitted field) means constrained auto-detection (FR-1).
    language: Literal["ru", "en"] | None = None
    provider: str | None = None
    model: str | None = None
    meeting: dict[str, Any] | None = None
    # `None` defers to the service's configured default (`config.diarize`);
    # an explicit true/false overrides it for this job only.
    diarize: bool | None = None

    @model_validator(mode="after")
    def _require_the_path_matching_the_job_type(self) -> JobCreate:
        if self.job_type == "transcribe":
            if not self.audio_path or self.input_path is not None:
                raise ValueError("a transcribe job takes audio_path (and no input_path)")
        elif not self.input_path or self.audio_path is not None:
            raise ValueError(f"a {self.job_type} job takes input_path (and no audio_path)")
        return self


class JobStatus(BaseModel):
    """``GET /v1/jobs/{id}`` response body (FR-2, FR-8)."""

    job_id: str
    status: JobState
    job_type: JobType = "transcribe"
    progress: float
    # Non-fatal degradations (a failed screenshot pass, a failed PDF render)
    # the job survived but the caller should surface.
    warnings: list[str] = Field(default_factory=list)
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
