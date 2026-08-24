"""Tests for the serial job manager: queue, progress, cancel, ledger, transcript.

Drives `JobManager` directly with `FakeProvider` (and small local subclasses of
it for timing/concurrency instrumentation) from `tests/fakes.py` -- no HTTP,
no model, no GPU, no network (FR-15).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fakes import FakeProvider

from transcription import jobs as jobs_module
from transcription import providers
from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import TERMINAL_STATUSES, JobManager, JobNotFoundError
from transcription.ledger import Ledger
from transcription.llm import BUILTIN_ENGINE
from transcription.providers.base import CancelToken, TranscriptResult


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


@pytest.fixture
async def manager(config: Config, ledger: Ledger) -> AsyncIterator[JobManager]:
    mgr = JobManager(config, ledger)
    await mgr.start()
    yield mgr
    await mgr.aclose()


@pytest.fixture
def audio_file(tmp_app_dir: Path) -> Path:
    path = tmp_app_dir / "audio.wav"
    path.write_bytes(b"fake-audio-bytes")
    return path


@pytest.fixture
def output_dir(tmp_app_dir: Path) -> Path:
    return tmp_app_dir / "output"


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        job = manager.status(job_id)
        if job.status in TERMINAL_STATUSES:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not reach a terminal state in {timeout}s")
        await asyncio_sleep()


async def asyncio_sleep(seconds: float = 0.01) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class SlowProvider(FakeProvider):
    """A FakeProvider whose chunks take measurable time, for timing tests."""

    def __init__(self, *args: object, chunk_sleep: float = 0.05, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.chunk_sleep = chunk_sleep

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        self.model_state = "loading"
        self.model_state = "loaded"

        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake provider raised {self.raise_kind.value}")

        for fraction in (0.25, 0.5, 0.75, 1.0):
            cancel.raise_if_cancelled()
            time.sleep(self.chunk_sleep)
            on_progress(fraction)

        text = "".join(seg.text for seg in self._segments)
        return TranscriptResult(
            segments=[seg.as_dict() for seg in self._segments],
            text=text,
            language=language or self.language,
            language_probability=0.99,
            duration_sec=1.0,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
        )


class TrackingProvider(FakeProvider):
    """Records call start/end times and concurrent-entry count (test-only)."""

    calls: ClassVar[list[tuple[float, float]]] = []
    concurrent: ClassVar[int] = 0
    max_concurrent: ClassVar[int] = 0
    lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.concurrent = 0
        cls.max_concurrent = 0

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        with TrackingProvider.lock:
            TrackingProvider.concurrent += 1
            TrackingProvider.max_concurrent = max(
                TrackingProvider.max_concurrent, TrackingProvider.concurrent
            )
        start = time.monotonic()
        try:
            time.sleep(0.05)
            return super().transcribe(
                audio_path, language=language, on_progress=on_progress, cancel=cancel
            )
        finally:
            end = time.monotonic()
            with TrackingProvider.lock:
                TrackingProvider.concurrent -= 1
            TrackingProvider.calls.append((start, end))


class CountingProvider(FakeProvider):
    """Counts how many times `transcribe` is actually invoked (test-only)."""

    call_count: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.call_count = 0

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        CountingProvider.call_count += 1
        time.sleep(0.2)
        return super().transcribe(
            audio_path, language=language, on_progress=on_progress, cancel=cancel
        )


async def test_submit_returns_job_id_and_inserts_queued_row_immediately(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )

    assert job_id
    row = ledger.get_job(job_id)
    assert row is not None
    assert row["status"] == "queued"


async def test_two_submissions_run_serially_never_concurrently(
    manager: JobManager, audio_file: Path, output_dir: Path
) -> None:
    TrackingProvider.reset()
    providers.register("fake", TrackingProvider)

    job1 = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "a"), provider="fake"
    )
    job2 = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "b"), provider="fake"
    )

    # Immediately after both submissions, the second must still be queued.
    assert manager.status(job2).status == "queued"

    await _wait_until_terminal(manager, job1)
    await _wait_until_terminal(manager, job2)

    assert manager.status(job1).status == "succeeded"
    assert manager.status(job2).status == "succeeded"
    assert TrackingProvider.max_concurrent == 1
    assert len(TrackingProvider.calls) == 2
    calls_sorted = sorted(TrackingProvider.calls)
    assert calls_sorted[1][0] >= calls_sorted[0][1]


async def test_status_progress_is_monotonic_and_ends_at_one(
    manager: JobManager, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", SlowProvider)

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )

    observed: list[float] = []
    while True:
        job = manager.status(job_id)
        observed.append(job.progress)
        if job.status in TERMINAL_STATUSES:
            break
        await asyncio_sleep(0.005)

    assert observed == sorted(observed)
    assert observed[-1] == 1.0


async def test_status_returns_fast_while_provider_blocks_worker_thread(
    manager: JobManager, audio_file: Path, output_dir: Path
) -> None:
    class BlockingProvider(FakeProvider):
        def transcribe(
            self,
            audio_path: Path,
            *,
            language: str | None,
            on_progress: Callable[[float], None],
            cancel: CancelToken,
        ) -> TranscriptResult:
            time.sleep(1.0)
            return super().transcribe(
                audio_path, language=language, on_progress=on_progress, cancel=cancel
            )

    providers.register("fake", BlockingProvider)

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    # Give the worker a moment to dequeue and enter the blocking call.
    await asyncio_sleep(0.1)
    assert manager.status(job_id).status == "running"

    started = time.perf_counter()
    manager.status(job_id)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05

    await _wait_until_terminal(manager, job_id, timeout=5.0)


async def test_success_writes_transcript_and_succeeded_ledger_row(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    # A measurable chunk sleep guarantees elapsed_sec > 0 regardless of clock
    # resolution -- an instant FakeProvider could round to 0.0 on a fast run.
    providers.register("fake", lambda config: SlowProvider(config, chunk_sleep=0.01))

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await _wait_until_terminal(manager, job_id)

    transcript_path = output_dir / "transcript.json"
    assert transcript_path.exists()
    doc = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert doc["text"] == "hello world"

    row = ledger.get_job(job_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["elapsed_sec"] > 0
    assert row["realtime_factor"] > 0
    assert row["segment_count"] == 2
    assert row["cost_usd"] is None


async def test_provider_failure_produces_failed_row_and_no_transcript(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register(
        "fake", lambda config: FakeProvider(config, raise_kind=ErrorKind.PROVIDER_AUTH)
    )

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await _wait_until_terminal(manager, job_id)

    row = ledger.get_job(job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_kind"] == "provider_auth"
    assert row["elapsed_sec"] is not None

    assert not (output_dir / "transcript.json").exists()
    assert list(output_dir.glob("*.tmp")) == []


async def test_cancel_queued_job_never_calls_the_provider(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    CountingProvider.reset()
    providers.register("fake", CountingProvider)

    job1 = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "a"), provider="fake"
    )
    job2 = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "b"), provider="fake"
    )

    await manager.cancel(job2)

    assert manager.status(job2).status == "cancelled"

    await _wait_until_terminal(manager, job1)

    assert CountingProvider.call_count == 1
    row2 = ledger.get_job(job2)
    assert row2 is not None
    assert row2["status"] == "cancelled"


async def test_cancel_running_job_ends_cancelled_within_five_seconds(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", lambda config: SlowProvider(config, chunk_sleep=0.1))

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await asyncio_sleep(0.05)
    assert manager.status(job_id).status == "running"

    await manager.cancel(job_id)
    await _wait_until_terminal(manager, job_id, timeout=5.0)

    assert manager.status(job_id).status == "cancelled"
    row = ledger.get_job(job_id)
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["elapsed_sec"] is not None
    assert not (output_dir / "transcript.json").exists()


async def test_cancel_unknown_job_id_raises_not_found(manager: JobManager) -> None:
    with pytest.raises(JobNotFoundError):
        await manager.cancel("does-not-exist")


async def test_submit_outside_allowlist_raises_and_creates_no_ledger_row(
    manager: JobManager,
    ledger: Ledger,
    output_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    providers.register("fake", FakeProvider)
    outside_dir = tmp_path_factory.mktemp("outside")
    outside_audio = outside_dir / "audio.wav"
    outside_audio.write_bytes(b"fake-audio-bytes")

    with pytest.raises(ServiceError) as exc_info:
        await manager.submit(
            audio_path=str(outside_audio), output_dir=str(output_dir), provider="fake"
        )

    assert exc_info.value.kind == ErrorKind.INVALID_REQUEST
    assert ledger.list_jobs() == []


async def test_submit_with_a_non_string_configured_model_raises_model_load_not_internal(
    manager: JobManager,
    ledger: Ledger,
    audio_file: Path,
    output_dir: Path,
) -> None:
    """Field report / Bug 3 regression: a malformed `Config.model` (a
    `dict`, exactly what a config.json parsing bug once produced from F3's
    real nested `"model": {"id": ..., "path": ...}` schema -- see
    config.py) must be rejected here, before any ledger write, as a
    classified `ServiceError(MODEL_LOAD)` -- never left to reach
    `self._ledger.insert_job`, whose sqlite bind would raise an unhandled
    `sqlite3.ProgrammingError` that FastAPI's generic exception handler
    turns into an uninformative HTTP 500 `internal`.
    """
    providers.register("fake", FakeProvider)
    manager._config = Config(
        app_dir=manager._config.app_dir,
        config_path=manager._config.config_path,
        provider="fake",
        model={"id": None, "path": None},  # type: ignore[arg-type]
        allowed_roots=manager._config.allowed_roots,
        db_path=manager._config.db_path,
        token=manager._config.token,
    )

    with pytest.raises(ServiceError) as exc_info:
        await manager.submit(
            audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
        )

    assert exc_info.value.kind == ErrorKind.MODEL_LOAD
    assert ledger.list_jobs() == []


async def test_exactly_one_ledger_row_per_job_across_terminal_outcomes(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    succeeded_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "ok"), provider="fake"
    )
    await _wait_until_terminal(manager, succeeded_id)

    providers.register("fake", lambda config: FakeProvider(config, raise_kind=ErrorKind.TIMEOUT))
    failed_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "bad"), provider="fake"
    )
    await _wait_until_terminal(manager, failed_id)

    providers.register("fake", lambda config: SlowProvider(config, chunk_sleep=0.1))
    blocker_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "blocker"), provider="fake"
    )
    cancelled_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "cancelled"), provider="fake"
    )
    await manager.cancel(cancelled_id)
    await _wait_until_terminal(manager, blocker_id)

    rows = ledger.list_jobs()
    job_ids = [row["job_id"] for row in rows]
    assert sorted(job_ids) == sorted([succeeded_id, failed_id, blocker_id, cancelled_id])
    assert len(job_ids) == len(set(job_ids))


async def test_submit_with_unknown_provider_raises_invalid_request_and_creates_no_row(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E1: an unknown provider name must be rejected synchronously, before
    any job/ledger row exists -- not discovered later by a dying worker."""
    with pytest.raises(ServiceError) as exc_info:
        await manager.submit(
            audio_path=str(audio_file), output_dir=str(output_dir), provider="bogus-provider"
        )

    assert exc_info.value.kind == ErrorKind.INVALID_REQUEST
    assert ledger.list_jobs() == []


def _stray_engine_selector(config: Config, value: str) -> None:
    """Put a leftover `llm_provider` value on a (frozen) `Config`.

    Stands in for an installed `config.json` that still carries the removed
    key: the job manager must ignore it entirely and run the built-in engine
    (FR-3). `object.__setattr__` because `Config` is frozen.
    """
    object.__setattr__(config, "llm_provider", value)


async def test_llm_job_without_a_provider_resolves_to_the_builtin_engine(
    config: Config, ledger: Ledger, tmp_app_dir: Path, output_dir: Path
) -> None:
    """FR-3: a derived job with no explicit provider runs on `BUILTIN_ENGINE`,
    with no config-file engine selector anywhere in the resolution."""
    _stray_engine_selector(config, "openai_compat")
    meeting = tmp_app_dir / "vault" / "PRJ" / "260101 - Planning"
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(
        json.dumps({"schema_version": 1, "text": "hi", "segments": []}), encoding="utf-8"
    )
    # No `start()`: `submit()` only validates and enqueues, so nothing here
    # ever resolves (or imports) a real LLM engine.
    manager = JobManager(config, ledger)
    try:
        job_id = await manager.submit(
            job_type="summarize", input_path=str(meeting), output_dir=str(output_dir)
        )
    finally:
        await manager.aclose()

    assert manager.status(job_id).provider == BUILTIN_ENGINE
    row = ledger.get_job(job_id)
    assert row is not None
    assert row["provider"] == BUILTIN_ENGINE


async def test_the_default_llm_factory_asks_the_registry_for_the_builtin_engine(
    config: Config, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-3: the engine factory names `BUILTIN_ENGINE` itself instead of
    reading a selector off the config."""
    _stray_engine_selector(config, "openai_compat")
    requested: list[str] = []
    sentinel = object()

    def fake_get_llm_provider(name: str, cfg: Config) -> object:
        requested.append(name)
        return sentinel

    monkeypatch.setattr(jobs_module, "get_llm_provider", fake_get_llm_provider)
    manager = JobManager(config, ledger)
    try:
        engine = manager._get_llm()
    finally:
        await manager.aclose()

    assert requested == [BUILTIN_ENGINE]
    assert engine is sentinel


def test_llm_info_reports_the_builtin_gguf_presence_and_no_engine_selector(
    config: Config, ledger: Ledger
) -> None:
    """FR-3: `/health`'s LLM block drops the `llm_provider` key and always
    answers `llm_model_present` from the configured GGUF file on disk."""
    _stray_engine_selector(config, "openai_compat")
    # `llm_info()` is the cheap `/health` snapshot: it never constructs an
    # engine, so this manager needs neither a worker nor a teardown.
    info = JobManager(config, ledger).llm_info()

    assert info == {"llm_model": config.llm_model, "llm_model_present": False}


async def test_worker_loop_survives_run_job_raising_and_keeps_draining_queue(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E1: any exception out of `_run_job` must finish that job as
    `failed`/`internal` and the worker loop must keep processing the next
    queued job -- reproduces the eval report's "worker died, good job stuck
    queued forever" scenario."""
    providers.register("fake", FakeProvider)

    original_run_job = manager._run_job
    calls = {"count": 0}

    async def flaky_run_job(job: object, loop: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash outside the normal error handling")
        await original_run_job(job, loop)  # type: ignore[arg-type]

    manager._run_job = flaky_run_job  # type: ignore[method-assign]

    crashing_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "crash"), provider="fake"
    )
    good_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "good"), provider="fake"
    )

    await _wait_until_terminal(manager, crashing_id)
    await _wait_until_terminal(manager, good_id)

    assert manager.status(crashing_id).status == "failed"
    assert manager.status(crashing_id).error_kind == ErrorKind.INTERNAL
    assert manager.status(good_id).status == "succeeded"

    crash_row = ledger.get_job(crashing_id)
    assert crash_row is not None
    assert crash_row["status"] == "failed"
    assert crash_row["error_kind"] == "internal"


class NoConfidenceProvider(FakeProvider):
    """Emits segments with the confidence fields missing/`None` (E2) -- the
    shape of any provider that does not report them."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        self.model_state = "loaded"
        on_progress(1.0)
        return TranscriptResult(
            segments=[
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hi",
                    "avg_logprob": None,
                    "no_speech_prob": None,
                    "compression_ratio": None,
                }
            ],
            text="hi",
            language=language or self.language,
            language_probability=None,
            duration_sec=1.0,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
        )


async def test_none_confidence_segments_reach_succeeded_not_stuck_running(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E2: a provider that legitimately omits the confidence fields must not
    hang the job in `running` -- `Segment` must accept `None` for those
    three fields."""
    providers.register("fake", NoConfidenceProvider)

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await _wait_until_terminal(manager, job_id)

    assert manager.status(job_id).status == "succeeded"
    row = ledger.get_job(job_id)
    assert row is not None
    assert row["status"] == "succeeded"

    doc = json.loads((output_dir / "transcript.json").read_text(encoding="utf-8"))
    assert doc["segments"][0]["avg_logprob"] is None
    assert doc["segments"][0]["no_speech_prob"] is None
    assert doc["segments"][0]["compression_ratio"] is None


async def test_submitted_job_ledger_row_records_resolved_device_not_config_literal(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E7: the ledger's `device` column must end up as the provider's
    resolved device, not the unresolved `config.device` ("auto") -- even
    though (E15) that resolution now happens off the event loop once the
    job actually starts, not at submission time, so it isn't visible until
    the job reaches `running`/terminal."""
    providers.register("fake", lambda cfg: FakeProvider(cfg, device="cuda"))

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await _wait_until_terminal(manager, job_id)
    row = ledger.get_job(job_id)

    assert row is not None
    assert row["device"] == "cuda"


async def test_provider_construction_failure_at_job_start_fails_as_model_load(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E15: a genuine failure to resolve/import a provider once a job
    actually starts must fail *that job* as `model_load` -- not crash the
    worker, and not the generic `internal` bucket -- and the worker must
    keep draining the queue afterwards."""

    def _boom(config: object) -> FakeProvider:
        raise ImportError("simulated: provider module cannot be imported")

    providers.register("broken", _boom)

    broken_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "broken"), provider="broken"
    )
    await _wait_until_terminal(manager, broken_id)

    assert manager.status(broken_id).status == "failed"
    assert manager.status(broken_id).error_kind == ErrorKind.MODEL_LOAD

    row = ledger.get_job(broken_id)
    assert row is not None
    assert row["error_kind"] == "model_load"

    providers.register("fake", FakeProvider)
    good_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir / "good"), provider="fake"
    )
    await _wait_until_terminal(manager, good_id)
    assert manager.status(good_id).status == "succeeded"


async def test_cancelled_job_carries_error_kind_cancelled_in_memory_and_ledger(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E9: a cancelled job must carry `error_kind='cancelled'`, not `null`."""
    providers.register("fake", lambda config: SlowProvider(config, chunk_sleep=0.1))

    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), provider="fake"
    )
    await asyncio_sleep(0.05)
    await manager.cancel(job_id)
    await _wait_until_terminal(manager, job_id, timeout=5.0)

    assert manager.status(job_id).error_kind == ErrorKind.CANCELLED
    row = ledger.get_job(job_id)
    assert row is not None
    assert row["error_kind"] == "cancelled"


async def test_status_hydrates_from_ledger_on_in_memory_cache_miss(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    """E11: a job this process never held in memory (e.g. seeded straight
    into the ledger, as after a restart) must still answer via `status()`
    instead of raising `JobNotFoundError`."""
    ledger.insert_job(
        "restart-job",
        provider="fake",
        model="fake-model",
        device="cpu",
        source_path=str(audio_file),
        output_path=str(output_dir),
    )
    ledger.mark_running("restart-job")
    ledger.finish_succeeded("restart-job", elapsed_sec=1.5, audio_duration_sec=3.0, segment_count=2)

    job = manager.status("restart-job")

    assert job.status == "succeeded"
    assert job.provider == "fake"
    assert job.elapsed_sec == pytest.approx(1.5)


async def test_provider_override_lands_in_ledger_row_and_transcript(
    manager: JobManager, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake-override", FakeProvider)

    job_id = await manager.submit(
        audio_path=str(audio_file),
        output_dir=str(output_dir),
        provider="fake-override",
    )
    await _wait_until_terminal(manager, job_id)

    row = ledger.get_job(job_id)
    assert row is not None
    assert row["provider"] == "fake-override"

    doc = json.loads((output_dir / "transcript.json").read_text(encoding="utf-8"))
    assert doc["provider"]["name"] == "fake-override"
