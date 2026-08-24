"""CLI: `serve` and one-shot `transcribe` (FR-1, FR-10).

Invokes `main(argv)` in-process -- never a real subprocess -- and registers
`FakeProvider` (and small local subclasses of it) in the provider registry,
so the default suite stays model-free, GPU-free and network-free (FR-15).
`transcribe` drives the same `JobManager` as the HTTP API, so a successful
run leaves the same sqlite row and `transcript.json` the HTTP path would
(FR-10). Every case checks the stdout/stderr split: exactly one JSON summary
object on stdout on success, everything else (progress, errors) on stderr,
and a distinct nonzero exit code per documented failure class.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider

from transcription import providers
from transcription.cli import EXIT_CODES, main
from transcription.errors import ErrorKind
from transcription.ledger import Ledger
from transcription.providers.base import CancelToken, TranscriptResult


class RawRaisingProvider(FakeProvider):
    """Raises an unclassified exception -- never a `ServiceError` -- to prove
    an unexpected failure still maps to a distinct, documented exit code."""

    def transcribe(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("totally unexpected failure")


class SlowProvider(FakeProvider):
    """A `FakeProvider` whose chunks take measurable time (E13's progress-line
    dedup test needs more than one distinct progress value to observe)."""

    def __init__(self, *args: Any, chunk_sleep: float = 0.05, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
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


@pytest.fixture(autouse=True)
def _app_dir_env(tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_app_dir))
    monkeypatch.delenv("TRANSCRIBER_MODEL_PATH", raising=False)


@pytest.fixture
def audio_file(tmp_app_dir: Path) -> Path:
    path = tmp_app_dir / "sample.wav"
    path.write_bytes(b"fake-audio-bytes")
    return path


def _common_args(tmp_app_dir: Path, audio: Path, out: Path, *, provider: str = "fake") -> list[str]:
    return [
        "transcribe",
        str(audio),
        "--out",
        str(out),
        "--provider",
        provider,
        "--allow-root",
        str(tmp_app_dir),
    ]


def test_help_exits_zero_and_lists_both_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "serve" in captured.out
    assert "transcribe" in captured.out


def test_unknown_secret_flag_errors_as_unrecognized_argument(
    tmp_app_dir: Path, audio_file: Path
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "transcribe",
                str(audio_file),
                "--out",
                str(tmp_app_dir / "out"),
                "--api-key",
                "sk-should-not-exist",
            ]
        )

    assert exc_info.value.code == 2


def test_transcribe_success_writes_transcript_and_prints_one_json_line(
    tmp_app_dir: Path, audio_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    providers.register("fake", FakeProvider)
    output_dir = tmp_app_dir / "out"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir))

    assert code == 0
    assert (output_dir / "transcript.json").exists()

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["status"] == "succeeded"
    assert summary["provider"] == "fake"
    assert captured.err != ""  # progress/diagnostics went to stderr


def test_progress_lines_are_deduplicated_by_rounded_value(
    tmp_app_dir: Path, audio_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """E13: the progress line must only be printed when the rounded value
    changes -- not once per poll iteration -- so a long job doesn't produce
    tens of thousands of near-identical stderr lines."""
    providers.register("fake", lambda cfg: SlowProvider(cfg, chunk_sleep=0.05))
    output_dir = tmp_app_dir / "out-progress"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir))

    assert code == 0
    captured = capsys.readouterr()
    progress_lines = [line for line in captured.err.splitlines() if line.startswith("progress:")]

    assert progress_lines  # at least one was printed
    values = [line.split(":", 1)[1].strip() for line in progress_lines]
    assert values == [v for i, v in enumerate(values) if i == 0 or v != values[i - 1]]
    # Bounded: at most one line per distinct rounded progress value (0.00..1.00).
    assert len(progress_lines) <= 6


def test_transcribe_success_leaves_one_ledger_row(tmp_app_dir: Path, audio_file: Path) -> None:
    providers.register("fake", FakeProvider)
    output_dir = tmp_app_dir / "out"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir))
    assert code == 0

    ledger = Ledger(str(tmp_app_dir / "data" / "jobs.sqlite3"))
    try:
        rows = ledger.list_jobs()
    finally:
        ledger.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"


def test_missing_input_file_exits_with_audio_decode_code(
    tmp_app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_app_dir / "does-not-exist.wav"
    output_dir = tmp_app_dir / "out"

    code = main(_common_args(tmp_app_dir, missing, output_dir))

    assert code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert missing.name in captured.err


def test_transcribe_succeeds_on_a_fresh_app_dir_with_no_data_folder(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-10 acceptance / E3: a brand-new app dir (no `data/` pre-created)
    must still work -- reproduces the eval report's fresh-checkout failure.

    Uses `tmp_path_factory` (not the `tmp_app_dir` fixture, which the
    autouse `_app_dir_env` fixture above already pre-populates with
    `models/`/`data/`/`logs/`) to get a directory nothing has touched yet.
    """
    fresh_app_dir = tmp_path_factory.mktemp("fresh-app-dir")
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(fresh_app_dir))
    monkeypatch.delenv("TRANSCRIBER_MODEL_PATH", raising=False)
    providers.register("fake", FakeProvider)

    audio = fresh_app_dir / "sample.wav"
    audio.write_bytes(b"fake-audio-bytes")
    output_dir = fresh_app_dir / "out"

    assert not (fresh_app_dir / "data").exists()

    code = main(_common_args(fresh_app_dir, audio, output_dir))

    assert code == 0
    assert (output_dir / "transcript.json").exists()


def test_out_of_allowlist_output_dir_exits_invalid_request_code(tmp_app_dir: Path) -> None:
    providers.register("fake", FakeProvider)
    allowed_root = tmp_app_dir / "inside"
    allowed_root.mkdir()
    audio_inside_root = allowed_root / "sample.wav"
    audio_inside_root.write_bytes(b"fake-audio-bytes")
    escaping_out = tmp_app_dir / "outside" / "definitely-outside"

    code = main(
        [
            "transcribe",
            str(audio_inside_root),
            "--out",
            str(escaping_out),
            "--provider",
            "fake",
            "--allow-root",
            str(allowed_root),
        ]
    )

    assert code == 2


@pytest.mark.parametrize(
    ("raise_kind", "expected_code"),
    [
        (ErrorKind.MODEL_LOAD, 4),
        (ErrorKind.PROVIDER_AUTH, 5),
        (ErrorKind.PROVIDER_RATE_LIMITED, 6),
        (ErrorKind.TIMEOUT, 7),
        (ErrorKind.CANCELLED, 8),
    ],
)
def test_failure_kinds_map_to_documented_exit_codes(
    raise_kind: ErrorKind, expected_code: int, tmp_app_dir: Path, audio_file: Path
) -> None:
    provider_name = f"fake-{raise_kind.value}"
    providers.register(
        provider_name, lambda cfg, kind=raise_kind: FakeProvider(cfg, raise_kind=kind)
    )
    output_dir = tmp_app_dir / f"out-{raise_kind.value}"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir, provider=provider_name))

    assert code == expected_code


def test_unexpected_exception_exits_with_internal_code(tmp_app_dir: Path, audio_file: Path) -> None:
    providers.register("raw-error", RawRaisingProvider)
    output_dir = tmp_app_dir / "out-raw"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir, provider="raw-error"))

    assert code == EXIT_CODES[ErrorKind.INTERNAL]
    assert code == 1


def test_documented_failure_exit_codes_are_pairwise_distinct() -> None:
    codes = [
        EXIT_CODES[ErrorKind.MODEL_LOAD],
        EXIT_CODES[ErrorKind.PROVIDER_AUTH],
        EXIT_CODES[ErrorKind.PROVIDER_RATE_LIMITED],
        EXIT_CODES[ErrorKind.TIMEOUT],
        EXIT_CODES[ErrorKind.CANCELLED],
        EXIT_CODES[ErrorKind.INTERNAL],
    ]
    assert len(set(codes)) == len(codes)


def test_provider_override_shows_up_in_stdout_summary(
    tmp_app_dir: Path, audio_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--provider` selects a registered provider by name and the summary
    reports it -- proven through the `register()` test hook, the only way a
    name other than the shipping `local` enters the registry."""
    providers.register("fake_stt", FakeProvider)
    output_dir = tmp_app_dir / "out-fake-stt"

    code = main(_common_args(tmp_app_dir, audio_file, output_dir, provider="fake_stt"))

    assert code == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip())
    assert summary["provider"] == "fake_stt"


def test_model_path_flag_beats_env_beats_config_file(
    tmp_app_dir: Path,
    audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    providers.register("fake", FakeProvider)
    (tmp_app_dir / "config.json").write_text(json.dumps({"model_path": "from-config"}))
    monkeypatch.setenv("TRANSCRIBER_MODEL_PATH", "from-env")
    output_dir = tmp_app_dir / "out-model-path"

    args = _common_args(tmp_app_dir, audio_file, output_dir) + ["--model-path", "from-flag"]
    code = main(args)

    assert code == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip())
    assert summary["model_path"] == "from-flag"


def test_serve_wires_merged_config_into_run_server(
    tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_server(config: object, **kwargs: object) -> None:
        captured["config"] = config

    import transcription.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_server", fake_run_server)

    code = main(["serve", "--port", "0", "--provider", "fake", "--allow-root", str(tmp_app_dir)])

    assert code == 0
    assert captured["config"].port == 0  # type: ignore[attr-defined]
    assert captured["config"].provider == "fake"  # type: ignore[attr-defined]
