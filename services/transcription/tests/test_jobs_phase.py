"""Job progress honesty: the `phase` label and the nullable `progress` (F3/T1).

Everything here is observed the way a client sees it -- through
`GET /v1/jobs/{id}` (`TestClient`, fixtures as in `tests/test_api_jobs.py`)
or through the CLI's stderr -- never by reading `JobState` internals.

Mid-phase states are pinned with `threading.Event` gates inside a test-local
provider and, for the CLI, with a stderr wrapper that releases a gate the
moment the line under test is actually written: the worker is held exactly
where the assertion needs it, so nothing here depends on timing (NFR-1). No
model, no GPU, no network.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient

from transcription import cli, providers
from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import JobManager
from transcription.providers.base import CancelToken, TranscriptResult
from transcription.schema import JobStatus

AUTH = {"Authorization": "Bearer test-token"}
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Every gate is bounded so a red run (the phase never reported, the gate
# never released) still ends instead of hanging the suite.
GATE_TIMEOUT = 3.0


class _GatedProvider(FakeProvider):
    """A `FakeProvider` that can be held at the two points this feature is about.

    `entered` is set once the provider has started work -- the job is running
    and has reported no progress yet; `reported_first` once the first fraction
    has been handed to `on_progress`. A gate handed to either point blocks the
    worker thread there until the test releases it.
    """

    def __init__(
        self,
        config: Any = None,
        *,
        before_first_progress: threading.Event | None = None,
        after_first_progress: threading.Event | None = None,
        first_fraction: float = 0.5,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._before_first_progress = before_first_progress
        self._after_first_progress = after_first_progress
        self._first_fraction = first_fraction
        self.entered = threading.Event()
        self.reported_first = threading.Event()

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        self.seen_language = language
        self.model_state = "loaded"
        self.entered.set()

        if self._before_first_progress is not None:
            self._before_first_progress.wait(timeout=GATE_TIMEOUT)
        on_progress(self._first_fraction)
        self.reported_first.set()

        if self._after_first_progress is not None:
            self._after_first_progress.wait(timeout=GATE_TIMEOUT)
        on_progress(1.0)

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


class _TriggeringStream:
    """A stream wrapper that fires a callback when a watched line is written.

    The CLI's own output is the synchronisation point: a gate is released the
    moment the line under test has actually been reported, so the assertion
    never races the worker (NFR-1).
    """

    def __init__(self, wrapped: Any, triggers: dict[str, Callable[[], None]]) -> None:
        self._wrapped = wrapped
        self._triggers = dict(triggers)

    def write(self, data: str) -> int:
        written: int = self._wrapped.write(data)
        for needle in [needle for needle in self._triggers if needle in data]:
            self._triggers.pop(needle)()
        return written

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


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
def audio_file(tmp_app_dir: Path) -> Path:
    path = tmp_app_dir / "audio.wav"
    path.write_bytes(b"fake-audio-bytes")
    return path


@pytest.fixture
def cli_env(tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))
    monkeypatch.delenv("TRANSCRIBER_MODEL_PATH", raising=False)


def _submit(client: TestClient, audio_file: Path, output_dir: Path) -> str:
    response = client.post(
        "/v1/jobs",
        json={"audio_path": str(audio_file), "output_dir": str(output_dir)},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    job_id: str = response.json()["job_id"]
    return job_id


def _poll_until_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body: dict[str, Any] = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in TERMINAL_STATUSES:
            return body
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not reach a terminal state in {timeout}s")
        time.sleep(0.01)


def _cli_args(tmp_app_dir: Path, audio_file: Path, output_dir: Path) -> list[str]:
    return [
        "transcribe",
        str(audio_file),
        "--out",
        str(output_dir),
        "--provider",
        "fake",
        "--allow-root",
        str(tmp_app_dir),
    ]


def _capture_submissions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[JobManager, str]]:
    """Let a CLI test reach the job the CLI submitted, through the real manager."""
    captured: list[tuple[JobManager, str]] = []

    class _RecordingJobManager(JobManager):
        async def submit(self, **kwargs: Any) -> str:
            job_id: str = await super().submit(**kwargs)
            captured.append((self, job_id))
            return job_id

    monkeypatch.setattr(cli, "JobManager", _RecordingJobManager)
    return captured


def test_job_status_accepts_a_null_progress_with_a_phase_label() -> None:
    status = JobStatus(job_id="job-1", status="running", progress=None, phase="rendering PDF")

    assert status.progress is None
    assert status.phase == "rendering PDF"


@pytest.mark.parametrize(("reported", "clamped"), [(1.7, 1.0), (-0.2, 0.0)])
def test_job_status_clamps_a_numeric_progress_into_the_unit_range(
    reported: float, clamped: float
) -> None:
    status = JobStatus(job_id="job-1", status="running", progress=reported)

    assert status.progress == clamped


def test_a_queued_job_reports_zero_progress_and_no_phase(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    gate = threading.Event()
    provider = _GatedProvider(before_first_progress=gate)
    providers.register("fake", lambda cfg: provider)
    app = create_app(config)

    with TestClient(app) as client:
        try:
            running_job = _submit(client, audio_file, tmp_app_dir / "running")
            assert provider.entered.wait(timeout=5.0)
            queued_job = _submit(client, audio_file, tmp_app_dir / "queued")

            body = client.get(f"/v1/jobs/{queued_job}", headers=AUTH).json()
        finally:
            gate.set()

        assert body["status"] == "queued"
        assert body["progress"] == 0.0
        assert body["phase"] is None

        _poll_until_terminal(client, running_job)
        _poll_until_terminal(client, queued_job)


def test_a_succeeded_job_reports_full_progress_and_no_phase(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    app = create_app(config)

    with TestClient(app) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")

        final = _poll_until_terminal(client, job_id)

        assert final["status"] == "succeeded"
        assert final["progress"] == 1.0
        assert final["phase"] is None


def test_a_failed_job_reports_no_phase(config: Config, audio_file: Path, tmp_app_dir: Path) -> None:
    providers.register("fake", lambda cfg: FakeProvider(cfg, raise_kind=ErrorKind.TIMEOUT))
    app = create_app(config)

    with TestClient(app) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")

        final = _poll_until_terminal(client, job_id)

        assert final["status"] == "failed"
        assert final["phase"] is None


def test_a_job_rebuilt_from_the_ledger_reports_full_progress_and_no_phase(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    """A job no manager still holds in memory -- the service was restarted --
    is answered from the ledger row, and answers the same contract."""
    providers.register("fake", FakeProvider)
    with TestClient(create_app(config)) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")
        _poll_until_terminal(client, job_id)

    with TestClient(create_app(config)) as restarted:
        body = restarted.get(f"/v1/jobs/{job_id}", headers=AUTH).json()

    assert body["status"] == "succeeded"
    assert body["progress"] == 1.0
    assert body["phase"] is None


def test_transcription_reports_preparing_until_the_provider_first_reports_progress(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    gate = threading.Event()
    provider = _GatedProvider(before_first_progress=gate)
    providers.register("fake", lambda cfg: provider)
    app = create_app(config)

    with TestClient(app) as client:
        try:
            job_id = _submit(client, audio_file, tmp_app_dir / "out")
            assert provider.entered.wait(timeout=5.0)

            body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        finally:
            gate.set()

        assert body["status"] == "running"
        assert body["phase"] == "preparing"
        assert body["progress"] == 0.0

        _poll_until_terminal(client, job_id)


def test_the_first_provider_progress_clears_the_phase_and_reports_that_fraction(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    gate = threading.Event()
    provider = _GatedProvider(after_first_progress=gate, first_fraction=0.5)
    providers.register("fake", lambda cfg: provider)
    app = create_app(config)

    with TestClient(app) as client:
        try:
            job_id = _submit(client, audio_file, tmp_app_dir / "out")
            assert provider.reported_first.wait(timeout=5.0)

            body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        finally:
            gate.set()

        assert body["progress"] == 0.5
        assert body["phase"] is None

        _poll_until_terminal(client, job_id)


@pytest.mark.usefixtures("cli_env")
def test_the_cli_reports_the_preparing_phase_before_any_transcription_progress(
    tmp_app_dir: Path,
    audio_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_transcribing = threading.Event()
    finish_transcribing = threading.Event()
    provider = _GatedProvider(
        before_first_progress=start_transcribing,
        after_first_progress=finish_transcribing,
        first_fraction=0.5,
    )
    providers.register("fake", lambda cfg: provider)
    monkeypatch.setattr(
        sys,
        "stderr",
        _TriggeringStream(
            sys.stderr,
            {
                "phase: preparing": start_transcribing.set,
                "progress: 0.50": finish_transcribing.set,
            },
        ),
    )

    code = cli.main(_cli_args(tmp_app_dir, audio_file, tmp_app_dir / "out"))

    lines = capsys.readouterr().err.splitlines()
    assert code == 0
    assert "phase: preparing" in lines
    assert lines.index("phase: preparing") < lines.index("progress: 0.50")


@pytest.mark.usefixtures("cli_env")
def test_the_cli_reports_a_phase_without_a_number_and_prints_no_progress_line_for_it(
    tmp_app_dir: Path,
    audio_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase with no linear signal (what summarize and export report) leaves
    `progress` null: the CLI names the phase and prints no number for it."""
    finish_transcribing = threading.Event()
    provider = _GatedProvider(before_first_progress=finish_transcribing)
    providers.register("fake", lambda cfg: provider)
    submissions = _capture_submissions(monkeypatch)

    def enter_a_phase_without_a_number() -> None:
        manager, job_id = submissions[-1]
        job = manager.status(job_id)
        job.progress = None
        job.phase = "reading transcript"

    monkeypatch.setattr(
        sys,
        "stderr",
        _TriggeringStream(
            sys.stderr,
            {
                "phase: preparing": enter_a_phase_without_a_number,
                "phase: reading transcript": finish_transcribing.set,
            },
        ),
    )

    code = cli.main(_cli_args(tmp_app_dir, audio_file, tmp_app_dir / "out"))

    lines = capsys.readouterr().err.splitlines()
    assert code == 0
    assert "phase: reading transcript" in lines
    reported = [line.split(":", 1)[1].strip() for line in lines if line.startswith("progress:")]
    assert [value for value in reported if not re.fullmatch(r"[01]\.\d+", value)] == []
