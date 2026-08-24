"""Serial job manager: queue, progress, cancel, ledger, transcript.

One FIFO ``asyncio.Queue`` and a single long-lived worker coroutine await
``provider.transcribe`` on a single-worker ``ThreadPoolExecutor`` so
inference never blocks the event loop (NFR-4). Path validation happens in
``submit()``, before anything is written to sqlite, so a request outside the
configured allowlist creates no ledger row (FR-9). Exactly one ledger row is
inserted per job at submission time and only ever ``UPDATE``d afterwards
(NFR-7). ``transcript.json`` is only ever produced by ``transcript.write_atomic``,
called once, after a successful, uncancelled result (FR-11).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcription import artifacts, exporting, frames, paths, transcript
from transcription.config import Config
from transcription.diarization import label_segments
from transcription.diarizer import DiarizerProtocol, PyannoteDiarizer
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.frame_extractor import FrameExtractorProtocol, PyAvFrameExtractor
from transcription.ledger import Ledger
from transcription.llm import BUILTIN_ENGINE, get_llm_provider, validate_llm_provider_name
from transcription.llm.base import LlmProvider, Message
from transcription.llm.chunking import chunk_lines
from transcription.llm.extraction import merge_items, snap_timestamps
from transcription.llm.prompts import (
    action_items_messages,
    facts_messages,
    render_transcript_lines,
    repair_messages,
)
from transcription.llm.reasoning import split_reasoning
from transcription.llm.report import collect_project_materials, report_from_materials
from transcription.llm.shapes import (
    ActionItemsOut,
    FactsOut,
    LlmOutputError,
    parse_llm_json,
)
from transcription.llm.summarize import summarize_chunks
from transcription.pdf import PdfRenderError, render_pdf
from transcription.providers import get_provider, validate_provider_name
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptionProvider
from transcription.schema import DiarizationInfo, Segment

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Every job type this manager can run. All of them share the single serial
# worker: an LLM job queued behind a transcription waits, and vice versa --
# which is also the RAM guarantee that whisper and the LLM never infer
# concurrently.
KNOWN_JOB_TYPES = frozenset(
    {"transcribe", "summarize", "action_items", "facts", "report", "export"}
)

_logger = logging.getLogger("transcription")


class JobNotFoundError(Exception):
    """Raised by `status()`/`cancel()` for an unknown job id."""


@dataclass
class JobState:
    """In-memory state for one job -- the fast half of `GET /v1/jobs/{id}`."""

    job_id: str
    status: str
    provider: str
    model: str
    source_path: str
    output_path: str
    job_type: str = "transcribe"
    language: str | None = None
    diarize: bool = False
    progress: float = 0.0
    elapsed_sec: float | None = None
    audio_duration_sec: float | None = None
    cost_usd: float | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    # The artifact manifest a non-transcribe job leaves behind (JSON text).
    result_json: str | None = None
    # Non-fatal degradations (failed screenshots, failed PDF render).
    warnings: list[str] = field(default_factory=list)
    cancel_token: CancelToken = field(default_factory=CancelToken)


class JobManager:
    """FIFO queue, one serial worker, progress, cancel, ledger, transcript (FR-2)."""

    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        *,
        diarizer_factory: Callable[[Config], DiarizerProtocol] | None = None,
        llm_factory: Callable[[Config], LlmProvider] | None = None,
        frame_extractor_factory: Callable[[], FrameExtractorProtocol] | None = None,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._jobs: dict[str, JobState] = {}
        self._providers: dict[str, TranscriptionProvider] = {}
        # The diarization engine, cached like the providers so its pipeline
        # loads once per process. `diarizer_factory` is a test seam mirroring
        # `create_app`'s `model_download_factory`: production callers never
        # pass it.
        self._diarizer: DiarizerProtocol | None = None
        self._diarizer_factory: Callable[[Config], DiarizerProtocol] = (
            diarizer_factory if diarizer_factory is not None else PyannoteDiarizer
        )
        # The LLM engine and the frame extractor, cached the same way;
        # `llm_factory`/`frame_extractor_factory` are the matching test seams.
        self._llm: LlmProvider | None = None
        self._llm_factory: Callable[[Config], LlmProvider] = (
            llm_factory
            if llm_factory is not None
            else (lambda config: get_llm_provider(BUILTIN_ENGINE, config))
        )
        self._frame_extractor: FrameExtractorProtocol | None = None
        self._frame_extractor_factory: Callable[[], FrameExtractorProtocol] = (
            frame_extractor_factory if frame_extractor_factory is not None else PyAvFrameExtractor
        )
        # Guards `self._providers` against two threads racing to construct
        # the same not-yet-cached provider (E15: resolution now happens off
        # the event loop, on arbitrary `asyncio.to_thread` worker threads).
        self._provider_lock = threading.Lock()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the single long-lived worker coroutine."""
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def aclose(self) -> None:
        """Stop the worker task and release the executor."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._executor.shutdown(wait=False)

    def _get_provider(self, name: str) -> TranscriptionProvider:
        """Resolve (and cache) the provider instance for `name`.

        Cached across jobs so a provider that lazily loads a model on first
        `transcribe()` only pays that cost once per process, not once per
        job. This constructs the provider -- and, the first time, lazily
        imports its underlying library, which can cost seconds -- so every
        caller on the event loop must run this via
        `asyncio.to_thread`/`run_in_executor`, never directly (E15).
        """
        with self._provider_lock:
            if name not in self._providers:
                self._providers[name] = get_provider(name, self._config)
            return self._providers[name]

    def _get_diarizer(self) -> DiarizerProtocol:
        """Resolve (and cache) the diarization engine.

        Same off-event-loop rule as `_get_provider` (E15): constructing the
        engine is cheap, but its first `diarize()` lazily imports the torch
        stack, so every caller reaches this via `asyncio.to_thread`.
        """
        with self._provider_lock:
            if self._diarizer is None:
                self._diarizer = self._diarizer_factory(self._config)
            return self._diarizer

    def _get_llm(self) -> LlmProvider:
        """Resolve (and cache) the LLM engine.

        Same off-event-loop rule as `_get_provider` (E15): construction can
        lazily import an LLM library, so callers reach this via
        `asyncio.to_thread`. The instance is cached even with
        `llm_keep_loaded=false` -- what unloads after a job is the model
        weights (`provider.unload()`), not the provider object.
        """
        with self._provider_lock:
            if self._llm is None:
                self._llm = self._llm_factory(self._config)
            return self._llm

    def _get_frame_extractor(self) -> FrameExtractorProtocol:
        with self._provider_lock:
            if self._frame_extractor is None:
                self._frame_extractor = self._frame_extractor_factory()
            return self._frame_extractor

    def llm_info(self) -> dict[str, Any]:
        """A cheap `/health` snapshot of the LLM engine's state (E15-safe).

        Mirrors `provider_info()`: never constructs an engine. `model_present`
        reports whether the configured GGUF file is on disk -- the built-in
        llama.cpp engine is the only one there is, so there is no engine
        selector to report.
        """
        model_file = Path(self._config.llm_model_path) / self._config.llm_model_file
        return {
            "llm_model": self._config.llm_model,
            "llm_model_present": model_file.is_file(),
        }

    async def submit(
        self,
        *,
        job_type: str = "transcribe",
        audio_path: str | None = None,
        input_path: str | None = None,
        output_dir: str,
        language: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        meeting: dict[str, Any] | None = None,
        diarize: bool | None = None,
    ) -> str:
        """Validate, insert the ledger row and enqueue a job (FR-2, FR-9).

        Returns the new job id immediately, before the job runs. Raises
        `ServiceError(invalid_request)` and creates no ledger row when the
        input/output paths fall outside the configured allowlist, when the
        job type is unknown, or when a derived job's input directory holds
        no ``transcript.json`` to work from.
        """
        if job_type not in KNOWN_JOB_TYPES:
            known = ", ".join(sorted(KNOWN_JOB_TYPES))
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                f"unknown job type {job_type!r}; known job types: {known}",
            )

        allowed_roots = [Path(root) for root in self._config.allowed_roots]
        if job_type == "transcribe":
            if not audio_path:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST, "a transcribe job requires audio_path"
                )
            resolved_source = paths.resolve_under_roots(audio_path, allowed_roots, must_exist=True)
        else:
            if not input_path:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST, f"a {job_type} job requires input_path"
                )
            resolved_source = paths.resolve_under_roots(input_path, allowed_roots, must_exist=True)
            if not resolved_source.is_dir():
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"input_path must be a directory: {resolved_source.name}",
                )
            # The per-meeting derived jobs read the transcript; reject a
            # meeting that has none before any ledger row exists. A report's
            # input is a whole project directory, checked in its runner
            # (some meetings legitimately lack transcripts).
            if (
                job_type in ("summarize", "action_items", "facts", "export")
                and not (resolved_source / "transcript.json").is_file()
            ):
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"no transcript.json in {resolved_source.name}; transcribe first",
                )
        resolved_output = paths.ensure_output_dir(output_dir, allowed_roots)

        if job_type == "transcribe":
            provider_name = provider or self._config.provider
            model_name = model or self._config.model
        elif job_type == "export":
            # Deterministic assembly: no model runs at all.
            provider_name = "none"
            model_name = "none"
        else:
            # The built-in llama.cpp engine is the only shipping one, so an
            # unnamed LLM job always lands there; there is no config-file
            # engine selector to consult (FR-3).
            provider_name = provider or BUILTIN_ENGINE
            model_name = model or self._config.llm_model
            validate_llm_provider_name(provider_name)

        # Defense in depth (field report): `self._config.model` is only
        # ever a plain string once `config.py::load_config` has parsed
        # correctly, but a bug there once let a malformed config.json
        # (F3's nested `"model": {"id": ..., "path": ...}` shape, copied
        # verbatim instead of unpacked) leak a raw `dict` all the way to
        # here. Left unchecked, that reached `self._ledger.insert_job`
        # below, whose sqlite bind raised an unclassified
        # `sqlite3.ProgrammingError` -- an unhandled exception this
        # request handler never expected, surfaced to the caller as a
        # bare HTTP 500 `internal` with no indication the *model
        # configuration* was the actual problem (FR-8: every failure must
        # be attributed to a taxonomy kind, never a generic message).
        # Catching the shape here, before any ledger write, reports it as
        # `model_load` instead -- config.py's own fix is the real root
        # cause fix; this is the backstop so a similar future config bug
        # degrades to a classified error, not a raw 500.
        if not isinstance(model_name, str) or not model_name:
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"configured model must be a non-empty string, got "
                f"{type(model_name).__name__!r}: {model_name!r}",
            )

        # Reject an unknown provider name here, before any job/ledger row
        # exists, instead of killing the worker on the queued job later
        # (FR-2, FR-8, NFR-7). Checking registry membership never imports a
        # provider module, so this is cheap enough for the event loop (E1).
        # The provider itself is deliberately *not* resolved here: doing so
        # can mean a multi-second lazy import of its underlying library,
        # which must never sit on this request's 200 ms budget (FR-2
        # acceptance, NFR-1, E15). `_run_job` resolves it off the event loop
        # once the job is actually dequeued, and the ledger's `device`
        # column is filled in then (`mark_running`), not at submission.
        # (LLM provider names were validated above, same rule via
        # `validate_llm_provider_name`.)
        if job_type == "transcribe":
            validate_provider_name(provider_name)

        job_id = uuid.uuid4().hex
        job = JobState(
            job_id=job_id,
            status="queued",
            job_type=job_type,
            provider=provider_name,
            model=model_name,
            source_path=str(resolved_source),
            output_path=str(resolved_output),
            language=language,
            # Per-job flag wins; `None` defers to the configured default.
            diarize=self._config.diarize if diarize is None else bool(diarize),
        )
        self._jobs[job_id] = job

        self._ledger.insert_job(
            job_id,
            job_type=job_type,
            provider=provider_name,
            model=model_name,
            # Placeholder until `_run_job` resolves the real provider and
            # corrects it via `mark_running(..., device=...)` (E15); never
            # surfaced as a job's final/terminal device.
            device=self._config.device,
            source_path=str(resolved_source),
            output_path=str(resolved_output),
            language=language,
            meeting_json=json.dumps(meeting) if meeting is not None else None,
        )

        await self._queue.put(job_id)
        return job_id

    def status(self, job_id: str) -> JobState:
        """The fast, in-memory half of `GET /v1/jobs/{id}` (NFR-4).

        Falls back to the ledger on a cache miss -- a job this process never
        held in memory (e.g. one that finished before a restart) still
        answers here instead of a spurious 404, per plan.md's "in-memory job
        state merged with ledger row".
        """
        job = self._jobs.get(job_id)
        if job is not None:
            return job

        row = self._ledger.get_job(job_id)
        if row is None:
            raise JobNotFoundError(f"unknown job id: {job_id}")
        hydrated = _job_state_from_ledger_row(row)
        self._jobs[job_id] = hydrated
        return hydrated

    def provider_info(self) -> ProviderInfo:
        """A cheap, event-loop-safe snapshot of the default provider's state,
        for `/health`.

        Never constructs (and so never imports) a provider library (E15):
        if the default provider has already been resolved -- by an earlier
        job's `_run_job`, run on this or a prior process's request -- this
        returns its live, cached `describe()`. Otherwise nothing has
        touched it yet, so this reports the *unresolved* config values with
        `model_state: "unloaded"` (still correct: nothing has loaded)
        instead of paying for a provider-library import on `/health`'s
        request path (FR-2 acceptance, NFR-1).
        """
        provider = self._providers.get(self._config.provider)
        if provider is not None:
            return provider.describe()
        return ProviderInfo(
            name=self._config.provider,
            model=self._config.model,
            device=self._config.device,
            compute_type=self._config.compute_type,
            model_state="unloaded",
        )

    async def cancel(self, job_id: str) -> None:
        """Cancel a queued or running job (FR-11); a no-op on a terminal job."""
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"unknown job id: {job_id}")

        if job.status in TERMINAL_STATUSES:
            return

        job.cancel_token.set()
        if job.status == "queued":
            # Not yet dequeued by the worker: end it here so the provider is
            # never called for a job that was cancelled while queued.
            job.status = "cancelled"
            job.error_kind = ErrorKind.CANCELLED
            self._ledger.finish_cancelled(job_id, elapsed_sec=None)
        # If already running, the provider's cooperative cancel check will
        # raise ServiceError(CANCELLED) and _run_job finishes the row.

    async def _worker_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                # Already resolved -- e.g. cancelled while still queued.
                continue
            try:
                # Looked up per job (not captured at construction) so the
                # test seam of replacing `_run_job` on an instance keeps
                # working.
                if job.job_type == "transcribe":
                    await self._run_job(job, loop)
                else:
                    await self._run_derived_job(job, loop)
            except Exception as exc:  # noqa: BLE001 - the worker must never die (NFR-7)
                # `_run_job` already attributes everything it can reach; this
                # is defense in depth for a bug that still escapes it. Catching
                # `Exception` (not `BaseException`) deliberately lets
                # `asyncio.CancelledError` through so `aclose()` can still
                # cancel this task during shutdown.
                self._finish_as_internal_failure(job, exc)

    def _finish_as_internal_failure(self, job: JobState, exc: Exception) -> None:
        """Terminate `job` as `failed`/`internal` without ever raising itself.

        The one place this service refuses to let a single job's exception
        take the worker loop down with it (FR-8, NFR-7).
        """
        job.status = "failed"
        job.error_kind = ErrorKind.INTERNAL
        job.error_message = str(exc)
        try:
            self._ledger.finish_failed(
                job.job_id, elapsed_sec=job.elapsed_sec, kind=ErrorKind.INTERNAL, message=str(exc)
            )
        except Exception:  # noqa: BLE001 - the ledger write itself must never crash the loop
            _logger.error("failed to record job %s as failed after a worker crash", job.job_id)

    async def _diarize_segments(
        self,
        job: JobState,
        loop: asyncio.AbstractEventLoop,
        segments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], DiarizationInfo]:
        """Run the diarization pass and label `segments` with speakers.

        Degrades rather than failing the job: a transcript without speakers
        is still the deliverable, so any non-cancellation failure here is
        recorded in the returned `DiarizationInfo` (and logged) while the
        segments pass through unlabelled. Cancellation propagates -- the
        operator asked the whole job to stop.
        """
        try:
            diarizer = await asyncio.to_thread(self._get_diarizer)
            turns = await loop.run_in_executor(
                self._executor,
                functools.partial(diarizer.diarize, Path(job.source_path), cancel=job.cancel_token),
            )
            labelled, speaker_count = label_segments(segments, turns)
            return labelled, DiarizationInfo(
                status="succeeded",
                model=diarizer.model,
                device=diarizer.device,
                speaker_count=speaker_count,
            )
        except ServiceError as exc:
            if exc.kind is ErrorKind.CANCELLED:
                raise
            kind, message = exc.kind, exc.message
        except Exception as exc:  # noqa: BLE001 - degraded, never job-fatal (FR-8)
            kind, message = ErrorKind.INTERNAL, str(exc)

        _logger.warning(
            "diarization failed for job %s; transcript written without speakers",
            job.job_id,
            extra={"event": "diarization_failed", "error_kind": kind.value},
        )
        return segments, DiarizationInfo(
            status="failed",
            model=self._config.diarization_model,
            error_kind=kind,
            error_message=redact(message),
        )

    async def _run_job(self, job: JobState, loop: asyncio.AbstractEventLoop) -> None:
        start = time.monotonic()

        # With a diarization pass still ahead, transcription owns only the
        # first 90% of the progress bar, so the bar never reads "done" while
        # the job is visibly still running.
        progress_scale = 0.9 if job.diarize else 1.0

        def on_progress(fraction: float) -> None:
            job.progress = fraction * progress_scale

        try:
            # Resolve (and, the first time, import) the provider off the
            # event loop -- constructing it can mean a multi-second lazy
            # import of its underlying library, and that must never block
            # anything else running on this process's single event
            # loop, including status polling for other jobs and `/health`
            # (NFR-4, E15). A genuine failure to resolve/construct it here
            # (e.g. the provider library genuinely cannot be imported) is
            # this job's failure, not a crash that kills the worker loop
            # (FR-8): report it as `model_load`, exactly like a failure to
            # load the model itself.
            try:
                provider_instance = await asyncio.to_thread(self._get_provider, job.provider)
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - reclassified below, not swallowed
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"failed to load provider {job.provider!r}: {redact(str(exc))}",
                ) from exc

            job.status = "running"
            self._ledger.mark_running(job.job_id, device=provider_instance.describe().device)

            result = await loop.run_in_executor(
                self._executor,
                functools.partial(
                    provider_instance.transcribe,
                    Path(job.source_path),
                    language=job.language,
                    on_progress=on_progress,
                    cancel=job.cancel_token,
                ),
            )

            segment_dicts = result.segments
            diarization_info: DiarizationInfo | None = None
            if job.diarize:
                segment_dicts, diarization_info = await self._diarize_segments(
                    job, loop, segment_dicts
                )

            elapsed = time.monotonic() - start
            realtime_factor = elapsed / result.duration_sec if result.duration_sec else 0.0
            segments = [Segment(**seg) for seg in segment_dicts]

            doc = transcript.build_document(
                source_path=job.source_path,
                duration_sec=result.duration_sec,
                provider_name=job.provider,
                model=result.model,
                device=result.device,
                compute_type=result.compute_type or "",
                language=result.language,
                language_probability=result.language_probability,
                text=result.text,
                segments=segments,
                elapsed_sec=elapsed,
                realtime_factor=realtime_factor,
                cost_usd=result.cost_usd,
                currency=result.currency,
                diarization=diarization_info,
            )
            transcript.write_atomic(doc, job.output_path)

            job.status = "succeeded"
            job.progress = 1.0
            job.elapsed_sec = elapsed
            job.audio_duration_sec = result.duration_sec
            job.cost_usd = result.cost_usd
            # FR-4: record the language the decode actually used, not the one
            # the request asked for -- on an auto job the row went in as NULL
            # and the provider's constrained detection picked the winner.
            job.language = result.language
            self._ledger.finish_succeeded(
                job.job_id,
                elapsed_sec=elapsed,
                audio_duration_sec=result.duration_sec,
                segment_count=len(segments),
                filtered_segment_count=result.filtered_segment_count,
                cost_usd=result.cost_usd,
                currency=result.currency,
                language=result.language,
            )
        except ServiceError as exc:
            elapsed = time.monotonic() - start
            job.elapsed_sec = elapsed
            if exc.kind is ErrorKind.CANCELLED:
                job.status = "cancelled"
                job.error_kind = ErrorKind.CANCELLED
                self._ledger.finish_cancelled(job.job_id, elapsed_sec=elapsed)
            else:
                job.status = "failed"
                job.error_kind = exc.kind
                job.error_message = exc.message
                self._ledger.finish_failed(
                    job.job_id, elapsed_sec=elapsed, kind=exc.kind, message=exc.message
                )
        except Exception as exc:  # noqa: BLE001 - anything unclassified is `internal` (FR-8)
            elapsed = time.monotonic() - start
            job.status = "failed"
            job.error_kind = ErrorKind.INTERNAL
            job.error_message = str(exc)
            job.elapsed_sec = elapsed
            self._ledger.finish_failed(
                job.job_id, elapsed_sec=elapsed, kind=ErrorKind.INTERNAL, message=str(exc)
            )

    # ------------------------------------------------------------------
    # Derived jobs (summarize / action_items / facts / report / export)
    # ------------------------------------------------------------------

    def _llm_budget_tokens(self) -> int:
        """The chunker's token budget: half the context window, leaving the
        other half for the prompt scaffolding and the answer."""
        return max(1024, self._config.llm_ctx // 2)

    async def _resolve_llm(self, job: JobState) -> LlmProvider:
        """Resolve the LLM engine off the event loop; failures are this
        job's `model_load`, never a worker crash (the `_run_job` rule)."""
        try:
            return await asyncio.to_thread(self._get_llm)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - reclassified, not swallowed
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"failed to load llm provider {job.provider!r}: {redact(str(exc))}",
            ) from exc

    async def _run_derived_job(self, job: JobState, loop: asyncio.AbstractEventLoop) -> None:
        """Run one non-transcribe job with the shared success/failure tail.

        The job body runs as one synchronous function on the single-worker
        executor (the same thread whisper inference uses), so LLM inference,
        frame decoding and artifact writes never touch the event loop, and
        no two jobs' heavy work ever overlaps.
        """
        start = time.monotonic()
        uses_llm = job.job_type != "export"
        try:
            if uses_llm:
                provider = await self._resolve_llm(job)
                job.status = "running"
                self._ledger.mark_running(job.job_id, device=provider.describe().device)
                if job.job_type == "summarize":
                    body = functools.partial(self._summarize_sync, job, provider)
                elif job.job_type in ("action_items", "facts"):
                    body = functools.partial(self._extract_sync, job, provider)
                else:
                    body = functools.partial(self._report_sync, job, provider)
            else:
                job.status = "running"
                self._ledger.mark_running(job.job_id)
                body = functools.partial(self._export_sync, job)
            manifest = await loop.run_in_executor(self._executor, body)

            elapsed = time.monotonic() - start
            job.status = "succeeded"
            job.progress = 1.0
            job.elapsed_sec = elapsed
            job.result_json = json.dumps(manifest, ensure_ascii=False)
            self._ledger.finish_succeeded(
                job.job_id, elapsed_sec=elapsed, result_json=job.result_json
            )
        except ServiceError as exc:
            elapsed = time.monotonic() - start
            job.elapsed_sec = elapsed
            if exc.kind is ErrorKind.CANCELLED:
                job.status = "cancelled"
                job.error_kind = ErrorKind.CANCELLED
                self._ledger.finish_cancelled(job.job_id, elapsed_sec=elapsed)
            else:
                job.status = "failed"
                job.error_kind = exc.kind
                job.error_message = exc.message
                self._ledger.finish_failed(
                    job.job_id, elapsed_sec=elapsed, kind=exc.kind, message=exc.message
                )
        except Exception as exc:  # noqa: BLE001 - anything unclassified is `internal` (FR-8)
            elapsed = time.monotonic() - start
            job.status = "failed"
            job.error_kind = ErrorKind.INTERNAL
            job.error_message = str(exc)
            job.elapsed_sec = elapsed
            self._ledger.finish_failed(
                job.job_id, elapsed_sec=elapsed, kind=ErrorKind.INTERNAL, message=str(exc)
            )
        finally:
            # Release the model weights unless the operator opted to keep
            # them resident: a ~20 GB working set must not sit around while
            # whisper jobs run (reloading is mmap-fast).
            if uses_llm and not self._config.llm_keep_loaded and self._llm is not None:
                try:
                    self._llm.unload()
                except Exception:  # noqa: BLE001 - unload must never take the worker down
                    _logger.warning("failed to unload the LLM after job %s", job.job_id)

    def _load_transcript_lines(self, meeting_dir: Path) -> tuple[list[str], dict[str, Any]]:
        """The meeting's transcript as `[m:ss] Speaker: text` lines, with the
        operator's manual speaker labels applied over the diarized ones."""
        data = exporting.load_transcript(meeting_dir)
        if data is None:
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                f"transcript.json is missing or unreadable in {meeting_dir.name}",
            )
        segments_raw = data.get("segments")
        segments = segments_raw if isinstance(segments_raw, list) else []
        lines = render_transcript_lines(segments, exporting.load_speaker_overrides(meeting_dir))
        if not lines:
            raise ServiceError(ErrorKind.UNSUPPORTED_INPUT, "the transcript is empty")
        return lines, data

    def _complete_text(
        self,
        job: JobState,
        provider: LlmProvider,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        on_progress: Callable[[float], None] | None = None,
        reasoning_sink: list[str] | None = None,
    ) -> str:
        """One completion, with the model's chain-of-thought split off.

        Reasoning never reaches an artifact or the UI: it lands in
        `reasoning_sink` when the caller wants to keep it (the summary and
        report runners write it to a `*.reasoning.md` sidecar) and is
        discarded otherwise.
        """
        completion = provider.complete(
            messages,
            json_schema=json_schema,
            max_tokens=self._config.llm_max_output_tokens,
            temperature=self._config.llm_temperature,
            on_progress=on_progress if on_progress is not None else (lambda fraction: None),
            cancel=job.cancel_token,
        )
        answer, reasoning = split_reasoning(completion.text)
        if reasoning and reasoning_sink is not None:
            reasoning_sink.append(reasoning)
        return answer

    def _summarize_sync(self, job: JobState, provider: LlmProvider) -> dict[str, Any]:
        meeting_dir = Path(job.source_path)
        lines, _ = self._load_transcript_lines(meeting_dir)
        chunks = chunk_lines(lines, self._llm_budget_tokens())
        total_calls = len(chunks) + (1 if len(chunks) > 1 else 0)
        calls_done = 0
        reasoning: list[str] = []

        def complete(messages: list[Message]) -> str:
            nonlocal calls_done
            text = self._complete_text(
                job,
                provider,
                messages,
                on_progress=lambda fraction: setattr(
                    job, "progress", min(0.99, (calls_done + fraction) / total_calls)
                ),
                reasoning_sink=reasoning,
            )
            calls_done += 1
            job.progress = min(0.99, calls_done / total_calls)
            return text

        summary = summarize_chunks(chunks, complete)
        summary_path = artifacts.write_text_atomic(
            summary + "\n", Path(job.output_path) / "summary.md"
        )
        # The model's chain-of-thought, kept out of the summary (and the
        # UI) but not thrown away: a sidecar next to it, for the curious.
        if reasoning:
            artifacts.write_text_atomic(
                "\n\n---\n\n".join(reasoning) + "\n",
                Path(job.output_path) / "summary.reasoning.md",
            )
        return {"artifacts": [str(summary_path)]}

    def _constrained_items(
        self,
        job: JobState,
        provider: LlmProvider,
        messages: list[Message],
        wrapper_cls: type[ActionItemsOut] | type[FactsOut],
        on_progress: Callable[[float], None],
    ) -> list[Any]:
        """One schema-constrained completion with the one bounded repair retry."""
        schema = wrapper_cls.model_json_schema()
        text = self._complete_text(
            job, provider, messages, json_schema=schema, on_progress=on_progress
        )
        try:
            return list(parse_llm_json(text, wrapper_cls).items)
        except LlmOutputError as first_error:
            repair = repair_messages(messages, first_error.raw, str(first_error))
            text = self._complete_text(
                job, provider, repair, json_schema=schema, on_progress=on_progress
            )
            try:
                return list(parse_llm_json(text, wrapper_cls).items)
            except LlmOutputError as second_error:
                raise ServiceError(
                    ErrorKind.LLM_OUTPUT,
                    f"the model returned invalid {job.job_type} output even after a "
                    f"repair attempt: {second_error}",
                ) from second_error

    @staticmethod
    def _find_source_file(meeting_dir: Path) -> Path | None:
        """The meeting's `source.<ext>` recording, if any."""
        try:
            for entry in meeting_dir.iterdir():
                if entry.is_file() and entry.stem.casefold() == "source":
                    return entry
        except OSError:
            return None
        return None

    def _extract_sync(self, job: JobState, provider: LlmProvider) -> dict[str, Any]:
        meeting_dir = Path(job.source_path)
        lines, data = self._load_transcript_lines(meeting_dir)
        chunks = chunk_lines(lines, self._llm_budget_tokens())

        if job.job_type == "action_items":
            wrapper_cls: type[ActionItemsOut] | type[FactsOut] = ActionItemsOut
            messages_fn: Callable[[str], list[Message]] = action_items_messages
            type_key = "type"
        else:
            wrapper_cls = FactsOut
            messages_fn = facts_messages
            type_key = "kind"

        # The LLM owns the first 80% of the progress bar; screenshots and
        # artifact writes own the rest (the diarization progress-split rule).
        llm_share = 0.8

        def progress_for(base: float, span: float) -> Callable[[float], None]:
            def on_progress(fraction: float) -> None:
                job.progress = min(0.99, base + fraction * span)

            return on_progress

        per_chunk: list[list[Any]] = []
        for index, chunk in enumerate(chunks):
            job.cancel_token.raise_if_cancelled()
            per_chunk.append(
                self._constrained_items(
                    job,
                    provider,
                    messages_fn(chunk),
                    wrapper_cls,
                    on_progress=progress_for(
                        (index / len(chunks)) * llm_share, llm_share / len(chunks)
                    ),
                )
            )
        items = merge_items(per_chunk)

        segments_raw = data.get("segments")
        segments = segments_raw if isinstance(segments_raw, list) else []
        segment_starts = [float(seg.get("start", 0.0)) for seg in segments if isinstance(seg, dict)]
        source_info = data.get("source")
        duration = None
        if isinstance(source_info, dict):
            raw_duration = source_info.get("duration_sec")
            duration = float(raw_duration) if isinstance(raw_duration, int | float) else None

        source_file = self._find_source_file(meeting_dir)
        project_name = Path(job.output_path).parent.name
        created = datetime.now(UTC).isoformat()

        md_paths: list[Path] = []
        screenshots_broken = False
        for index, item in enumerate(items):
            job.cancel_token.raise_if_cancelled()
            snapped = snap_timestamps(list(item.timestamps), segment_starts, duration)
            images: list[tuple[str, bytes]] = []
            screenshots_status = "none"
            planned = frames.plan_screenshots(snapped, duration)
            if source_file is not None and planned and not screenshots_broken:
                try:
                    extracted = self._get_frame_extractor().extract(
                        source_file, planned, cancel=job.cancel_token
                    )
                    images = [(frames.screenshot_name(stamp), png) for stamp, png in extracted]
                    screenshots_status = "succeeded" if images else "none"
                except ServiceError as exc:
                    if exc.kind is ErrorKind.CANCELLED:
                        raise
                    screenshots_broken = True
                    screenshots_status = f"failed ({exc.kind.value})"
                    job.warnings.append(
                        f"screenshots failed ({exc.kind.value}): {exc.message}; "
                        "items were written without images"
                    )
                except Exception as exc:  # noqa: BLE001 - degraded, never job-fatal
                    screenshots_broken = True
                    screenshots_status = "failed (internal)"
                    job.warnings.append(
                        f"screenshots failed: {exc}; items were written without images"
                    )
            elif screenshots_broken:
                screenshots_status = "failed"

            meta: dict[str, Any] = {
                type_key: getattr(item, type_key),
                "title": item.title,
                "source_project": project_name,
                "source_meeting": meeting_dir.name,
                "source_recording": source_file.name if source_file is not None else None,
                "timestamps": snapped,
                "created": created,
                "model": job.model,
                "job_id": job.job_id,
                "screenshots": screenshots_status,
            }
            md_paths.append(
                artifacts.write_item(
                    Path(job.output_path),
                    title=item.title,
                    meta=meta,
                    body_md=item.description_md,
                    images=images,
                )
            )
            job.progress = min(
                0.99, llm_share + ((index + 1) / max(1, len(items))) * (1 - llm_share)
            )

        return {"artifacts": [str(path) for path in md_paths], "item_count": len(md_paths)}

    def _report_sync(self, job: JobState, provider: LlmProvider) -> dict[str, Any]:
        project_dir = Path(job.source_path)
        materials = collect_project_materials(project_dir)
        if not materials.strip():
            raise ServiceError(
                ErrorKind.UNSUPPORTED_INPUT,
                f"project {project_dir.name} has no transcripts, summaries or items to report on",
            )

        calls_done = 0
        reasoning: list[str] = []

        def complete(messages: list[Message]) -> str:
            nonlocal calls_done
            text = self._complete_text(
                job,
                provider,
                messages,
                on_progress=lambda fraction: setattr(
                    job,
                    "progress",
                    min(0.85, 0.05 + (calls_done + fraction) * 0.2),
                ),
                reasoning_sink=reasoning,
            )
            calls_done += 1
            return text

        report_md = report_from_materials(
            materials,
            project_dir.name,
            complete,
            budget_tokens=self._llm_budget_tokens(),
        )

        out_dir = Path(job.output_path)
        md_path = artifacts.write_text_atomic(report_md + "\n", out_dir / "report.md")
        if reasoning:
            artifacts.write_text_atomic(
                "\n\n---\n\n".join(reasoning) + "\n", out_dir / "report.reasoning.md"
            )
        artifact_paths = [str(md_path)]
        job.progress = 0.9
        try:
            pdf_path = render_pdf(report_md, out_dir / "report.pdf", base_dir=out_dir)
            artifact_paths.append(str(pdf_path))
        except PdfRenderError as exc:
            job.warnings.append(f"PDF render failed: {exc}; report.md was written")
        return {"artifacts": artifact_paths}

    def _export_sync(self, job: JobState) -> dict[str, Any]:
        meeting_dir = Path(job.source_path)
        export_dir = Path(job.output_path)
        parent = meeting_dir.parent
        project_dir = None if parent.name.casefold() == "unsorted" else parent

        job.progress = 0.1
        export_md, warnings = exporting.build_export_md(
            meeting_dir=meeting_dir,
            meeting_name=meeting_dir.name,
            project_dir=project_dir,
            export_dir=export_dir,
        )
        job.warnings.extend(warnings)
        md_path = artifacts.write_text_atomic(export_md, export_dir / "export.md")
        artifact_paths = [str(md_path)]
        job.progress = 0.5
        job.cancel_token.raise_if_cancelled()
        try:
            pdf_path = render_pdf(export_md, export_dir / "export.pdf", base_dir=export_dir)
            artifact_paths.append(str(pdf_path))
        except PdfRenderError as exc:
            job.warnings.append(f"PDF render failed: {exc}; export.md was written")
        return {"artifacts": artifact_paths}


def _job_state_from_ledger_row(row: dict[str, Any]) -> JobState:
    """Reconstruct a terminal job's `JobState` from its ledger row.

    Used by `JobManager.status()` on an in-memory cache miss -- e.g. after a
    process restart, when the job predates this process but its ledger row
    still exists.
    """
    error_kind_raw = row.get("error_kind")
    return JobState(
        job_id=row["job_id"],
        status=row["status"],
        job_type=row.get("job_type") or "transcribe",
        provider=row["provider"],
        model=row["model"],
        source_path=row["source_path"],
        output_path=row["output_path"],
        language=row.get("language"),
        progress=1.0 if row["status"] == "succeeded" else 0.0,
        elapsed_sec=row.get("elapsed_sec"),
        audio_duration_sec=row.get("audio_duration_sec"),
        cost_usd=row.get("cost_usd"),
        error_kind=ErrorKind(error_kind_raw) if error_kind_raw else None,
        error_message=row.get("error_message"),
        result_json=row.get("result_json"),
    )
