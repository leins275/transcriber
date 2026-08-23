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
from pathlib import Path
from typing import Any

from transcription import paths, transcript
from transcription.config import Config
from transcription.diarization import label_segments
from transcription.diarizer import DiarizerProtocol, PyannoteDiarizer
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.ledger import Ledger
from transcription.providers import get_provider, validate_provider_name
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptionProvider
from transcription.schema import DiarizationInfo, Segment

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

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
    language: str | None = None
    diarize: bool = False
    progress: float = 0.0
    elapsed_sec: float | None = None
    audio_duration_sec: float | None = None
    cost_usd: float | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    cancel_token: CancelToken = field(default_factory=CancelToken)


class JobManager:
    """FIFO queue, one serial worker, progress, cancel, ledger, transcript (FR-2)."""

    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        *,
        diarizer_factory: Callable[[Config], DiarizerProtocol] | None = None,
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

    async def submit(
        self,
        *,
        audio_path: str,
        output_dir: str,
        language: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        meeting: dict[str, Any] | None = None,
        diarize: bool | None = None,
    ) -> str:
        """Validate, insert the ledger row and enqueue a job (FR-2, FR-9).

        Returns the new job id immediately, before the job runs. Raises
        `ServiceError(invalid_request)` and creates no ledger row when
        `audio_path`/`output_dir` fall outside the configured allowlist.
        """
        allowed_roots = [Path(root) for root in self._config.allowed_roots]
        resolved_audio = paths.resolve_under_roots(audio_path, allowed_roots, must_exist=True)
        resolved_output = paths.ensure_output_dir(output_dir, allowed_roots)

        provider_name = provider or self._config.provider
        model_name = model or self._config.model

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
        validate_provider_name(provider_name)

        job_id = uuid.uuid4().hex
        job = JobState(
            job_id=job_id,
            status="queued",
            provider=provider_name,
            model=model_name,
            source_path=str(resolved_audio),
            output_path=str(resolved_output),
            language=language,
            # Per-job flag wins; `None` defers to the configured default.
            diarize=self._config.diarize if diarize is None else bool(diarize),
        )
        self._jobs[job_id] = job

        self._ledger.insert_job(
            job_id,
            provider=provider_name,
            model=model_name,
            # Placeholder until `_run_job` resolves the real provider and
            # corrects it via `mark_running(..., device=...)` (E15); never
            # surfaced as a job's final/terminal device.
            device=self._config.device,
            source_path=str(resolved_audio),
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
                await self._run_job(job, loop)
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
            self._ledger.finish_succeeded(
                job.job_id,
                elapsed_sec=elapsed,
                audio_duration_sec=result.duration_sec,
                segment_count=len(segments),
                filtered_segment_count=result.filtered_segment_count,
                cost_usd=result.cost_usd,
                currency=result.currency,
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
    )
