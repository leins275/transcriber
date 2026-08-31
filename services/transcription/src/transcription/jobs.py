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

from transcription import artifacts, exporting, llm_catalog, paths, transcript
from transcription.config import Config
from transcription.diarization import label_segments
from transcription.diarizer import DiarizerProtocol, PyannoteDiarizer
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.ledger import Ledger
from transcription.llm import (
    BUILTIN_ENGINE,
    get_embedder,
    get_llm_provider,
    validate_llm_provider_name,
)
from transcription.llm.base import EmbeddingProvider, LlmProvider, LlmTruncatedError, Message
from transcription.llm.chunking import chunk_lines, input_budget_tokens
from transcription.llm.prompts import render_transcript_lines
from transcription.llm.reasoning import split_reasoning
from transcription.llm.summarize import summarize_chunks
from transcription.pdf import PdfRenderError, render_pdf
from transcription.providers import get_provider, validate_provider_name
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptionProvider
from transcription.schema import DiarizationInfo, Segment
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Every job type this manager can run. All of them share the single serial
# worker: an LLM job queued behind a transcription waits, and vice versa --
# which is also the RAM guarantee that whisper and the LLM never infer
# concurrently (the index job's embedder is CPU-only on top of that).
KNOWN_JOB_TYPES = frozenset({"transcribe", "summarize", "export", "index"})

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
    # Non-fatal degradations (a failed diarization, a failed PDF render).
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
        embedder_factory: Callable[[Config], EmbeddingProvider] | None = None,
        index_db_factory: Callable[[Config], IndexDb] | None = None,
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
        # The LLM engine, cached the same way; `llm_factory` is the
        # matching test seam.
        self._llm: LlmProvider | None = None
        self._llm_factory: Callable[[Config], LlmProvider] = (
            llm_factory
            if llm_factory is not None
            else (lambda config: get_llm_provider(BUILTIN_ENGINE, config))
        )
        # The search-index pair (embedder + database), cached the same way;
        # both factories are the matching test seams.
        self._embedder: EmbeddingProvider | None = None
        self._embedder_factory: Callable[[Config], EmbeddingProvider] = (
            embedder_factory if embedder_factory is not None else get_embedder
        )
        self._index_db: IndexDb | None = None
        self._index_db_factory: Callable[[Config], IndexDb] = (
            index_db_factory
            if index_db_factory is not None
            else (
                lambda config: IndexDb(
                    config.index_db_path,
                    embedding_model=config.embedding_model,
                    embedding_dim=llm_catalog.EMBEDDING_DIM,
                )
            )
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
        if self._index_db is not None:
            self._index_db.close()
            self._index_db = None

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

    def _get_embedder(self) -> EmbeddingProvider:
        """Resolve (and cache) the embedding engine (same off-event-loop
        rule as `_get_provider`)."""
        with self._provider_lock:
            if self._embedder is None:
                self._embedder = self._embedder_factory(self._config)
            return self._embedder

    def _get_index_db(self) -> IndexDb:
        """Resolve (and cache) the index database. One long-lived instance:
        the index job writes through it and search reads through it, all on
        the serial executor."""
        with self._provider_lock:
            if self._index_db is None:
                self._index_db = self._index_db_factory(self._config)
            return self._index_db

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

    def has_active_llm_job(self) -> bool:
        """Whether any model-loading LLM job (summarize -- not export, which
        never touches the model) is queued or running. Guards catalog model
        deletion: never pull a GGUF out from under a job that may be about
        to mmap it.
        """
        return any(
            job.job_type == "summarize" and job.status in ("queued", "running")
            for job in self._jobs.values()
        )

    async def submit(
        self,
        *,
        job_type: str = "transcribe",
        audio_path: str | None = None,
        input_path: str | None = None,
        output_dir: str | None = None,
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
        elif job_type == "index":
            # Vault-wide, no request paths: source is the configured vault
            # root, "output" is the index database (the ledger's columns are
            # NOT NULL). Debounced -- an already-queued index job will see
            # any change a second submission was announcing, so its id is
            # answered instead of queueing duplicate work.
            if not self._config.vault_root:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    "index jobs require a configured vault_root",
                )
            for existing in self._jobs.values():
                if existing.job_type == "index" and existing.status == "queued":
                    return existing.job_id
            resolved_source = Path(self._config.vault_root)
            if not resolved_source.is_dir():
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"vault_root is not a directory: {resolved_source}",
                )
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
            # meeting that has none before any ledger row exists.
            if (
                job_type in ("summarize", "export")
                and not (resolved_source / "transcript.json").is_file()
            ):
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"no transcript.json in {resolved_source.name}; transcribe first",
                )
        if job_type == "index":
            resolved_output = Path(self._config.index_db_path)
        else:
            if not output_dir:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST, f"a {job_type} job requires output_dir"
                )
            resolved_output = paths.ensure_output_dir(output_dir, allowed_roots)

        if job_type == "transcribe":
            provider_name = provider or self._config.provider
            model_name = model or self._config.model
        elif job_type == "index":
            # No LLM, no whisper: the CPU embedder is the only model here.
            provider_name = "none"
            model_name = self._config.embedding_model
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
            output = await loop.run_in_executor(
                self._executor,
                functools.partial(diarizer.diarize, Path(job.source_path), cancel=job.cancel_token),
            )
            labelled, speaker_count, label_mapping = label_segments(segments, output.turns)
            # Embeddings arrive keyed by the diarizer's raw cluster labels;
            # the document stores them under the normalized display labels
            # ("Speaker 1"...) so they join against `speakers.json` renames.
            speaker_embeddings: dict[str, list[float]] | None = None
            if output.embeddings:
                speaker_embeddings = {
                    label_mapping[raw]: vector
                    for raw, vector in output.embeddings.items()
                    if raw in label_mapping
                } or None
            return labelled, DiarizationInfo(
                status="succeeded",
                model=diarizer.model,
                device=diarizer.device,
                speaker_count=speaker_count,
                speaker_embeddings=speaker_embeddings,
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
    # Derived jobs (summarize / export)
    # ------------------------------------------------------------------

    def _llm_budget_tokens(self) -> int:
        """The chunker's token budget: the context window minus everything
        else that shares it (the answer, the reasoning headroom, prompt
        scaffolding), so a fitting chunk can never overflow ``n_ctx``."""
        return input_budget_tokens(
            self._config.llm_ctx,
            self._config.llm_max_output_tokens,
            self._config.llm_think_headroom_tokens,
        )

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
        executor (the same thread whisper inference uses), so LLM inference
        and artifact writes never touch the event loop, and no two jobs'
        heavy work ever overlaps.
        """
        start = time.monotonic()
        uses_llm = job.job_type == "summarize"
        try:
            if uses_llm:
                provider = await self._resolve_llm(job)
                job.status = "running"
                self._ledger.mark_running(job.job_id, device=provider.describe().device)
                body = functools.partial(self._summarize_sync, job, provider)
            elif job.job_type == "index":
                # The embedder is constructed here (cheap); its lazy weight
                # load -- and any failure of it -- happens inside the walk,
                # where the indexer degrades to text-only rows.
                embedder = await asyncio.to_thread(self._get_embedder)
                job.status = "running"
                self._ledger.mark_running(job.job_id)
                body = functools.partial(self._index_sync, job, embedder)
            elif job.job_type == "export":
                job.status = "running"
                self._ledger.mark_running(job.job_id)
                body = functools.partial(self._export_sync, job)
            else:
                # Unreachable in practice -- the pydantic `JobType` literal
                # and `KNOWN_JOB_TYPES` both gate the type long before the
                # worker sees it. Kept explicit so a future job type cannot
                # silently inherit another's path.
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"unsupported job type {job.job_type!r}",
                )
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
            # The embedder always unloads after an index job (~700 MB RSS,
            # mmap-fast reload; no keep-loaded semantics on purpose).
            if job.job_type == "index" and self._embedder is not None:
                try:
                    self._embedder.unload()
                except Exception:  # noqa: BLE001 - unload must never take the worker down
                    _logger.warning("failed to unload the embedder after job %s", job.job_id)

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
        `reasoning_sink` when the caller wants to keep it (the summary
        runner writes it to a `*.reasoning.md` sidecar) and is discarded
        otherwise.

        Free-text calls get the think-headroom on top of the output cap so
        the reasoning block cannot eat the whole answer budget; JSON calls
        are grammar-constrained (no thinking) and keep the plain cap. A
        completion that stops at the cap raises `LlmTruncatedError` -- the
        cut-off text must never pass as an answer (a truncated summary, or
        chain-of-thought whose `</think>` was never emitted) and truncated
        JSON cannot be repaired textually; callers split the input and retry.
        """
        max_tokens = self._config.llm_max_output_tokens
        if json_schema is None:
            max_tokens += self._config.llm_think_headroom_tokens
        completion = provider.complete(
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=self._config.llm_temperature,
            on_progress=on_progress if on_progress is not None else (lambda fraction: None),
            cancel=job.cancel_token,
        )
        if completion.finish_reason == "length":
            raise LlmTruncatedError(
                f"the completion stopped at the {max_tokens}-token output limit"
            )
        answer, reasoning = split_reasoning(completion.text)
        if reasoning and reasoning_sink is not None:
            reasoning_sink.append(reasoning)
        return answer

    def _summarize_sync(self, job: JobState, provider: LlmProvider) -> dict[str, Any]:
        meeting_dir = Path(job.source_path)
        lines, data = self._load_transcript_lines(meeting_dir)
        budget = self._llm_budget_tokens()
        chunks = chunk_lines(lines, budget, count_tokens=provider.count_tokens)
        # The transcript's own language, pinned into the single-chunk call,
        # every map call and the reduce. Missing, null or unsupported values
        # are the prompt builder's problem: it falls back to the soft rule.
        language = data.get("language")
        # Split-retries and reduce rounds can add calls beyond this plan, so
        # progress is clamped monotone against a denominator that grows with
        # the actual call count instead of ever running backwards.
        planned_calls = len(chunks) + (1 if len(chunks) > 1 else 0)
        calls_done = 0
        reasoning: list[str] = []

        def advance(fraction: float) -> None:
            total = max(planned_calls, calls_done + 1)
            job.progress = min(0.99, max(job.progress, (calls_done + fraction) / total))

        def complete(messages: list[Message]) -> str:
            nonlocal calls_done
            job.cancel_token.raise_if_cancelled()
            text = self._complete_text(
                job,
                provider,
                messages,
                on_progress=advance,
                reasoning_sink=reasoning,
            )
            calls_done += 1
            advance(0.0)
            return text

        def split_chunk(chunk: str, depth: int) -> list[str]:
            # Halved against what the chunk actually holds, not just the
            # configured budget: a transcript far below the budget would
            # otherwise come back as one identical piece and the retry
            # ladder would never run (the 260825 field report).
            piece_budget = max(64, min(budget >> (depth + 1), provider.count_tokens(chunk) // 2))
            return chunk_lines(chunk.splitlines(), piece_budget, count_tokens=provider.count_tokens)

        try:
            summary = summarize_chunks(
                chunks,
                complete,
                language,
                reduce_budget_tokens=budget,
                count_tokens=provider.count_tokens,
                split_chunk=split_chunk,
            )
        except LlmTruncatedError as truncated:
            raise ServiceError(
                ErrorKind.LLM_OUTPUT,
                "the model's summary output hit the token limit even after the "
                "transcript was split into minimal chunks; raising "
                "llm_max_output_tokens in the service config may help",
            ) from truncated
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

    def _index_sync(self, job: JobState, embedder: EmbeddingProvider) -> dict[str, Any]:
        """One incremental index pass over the vault (runs on the serial
        executor -- embedding never overlaps whisper/LLM inference)."""
        db = self._get_index_db()

        def on_progress(fraction: float) -> None:
            job.progress = max(0.0, min(1.0, fraction))

        stats = index_vault(
            Path(job.source_path),
            db,
            embedder,
            on_progress=on_progress,
            cancel=job.cancel_token,
        )
        job.warnings.extend(stats.warnings)
        return {"stats": stats.as_dict()}

    def _export_sync(self, job: JobState) -> dict[str, Any]:
        meeting_dir = Path(job.source_path)
        export_dir = Path(job.output_path)

        job.progress = 0.1
        export_md, warnings = exporting.build_export_md(
            meeting_dir=meeting_dir,
            meeting_name=meeting_dir.name,
        )
        job.warnings.extend(warnings)
        md_path = artifacts.write_text_atomic(export_md, export_dir / "export.md")
        artifact_paths = [str(md_path)]
        job.progress = 0.5
        job.cancel_token.raise_if_cancelled()
        try:
            pdf_path = render_pdf(
                export_md,
                # Named for sharing (`<project> - <date> - <title>.pdf`),
                # unlike the fixed `export.md` beside it.
                export_dir / artifacts.export_pdf_filename(meeting_dir),
                base_dir=export_dir,
                # Font degradation (no Cyrillic-capable family) is not an
                # error, but it makes the PDF unreadable for Russian text --
                # the operator has to hear about it, not just the log.
                warnings=job.warnings,
            )
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
