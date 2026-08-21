"""argparse CLI: `serve` and one-shot `transcribe` (FR-1, FR-10).

`transcribe` drives the same `JobManager` the HTTP API uses, in-process via
`asyncio.run`, so the sqlite row and the written `transcript.json` are
identical to what a `POST /v1/jobs` request would produce (FR-10). Exit
codes come from one `EXIT_CODES` table keyed by `ErrorKind`, matching the
plan's error-taxonomy exit-code column verbatim; documented in `README.md`
(T16).

Machine-readable output -- the one-line JSON summary on success -- goes to
stdout; every diagnostic (progress, error messages) goes to stderr, so a
caller piping stdout through `json.loads` never has to filter noise (`cli`
profile rule). No flag here is named like a secret (FR-9): credentials come
from the environment or the config file only (`config.py`), never argv --
`--api-key` is not a defined flag, so passing it is an unrecognized argument.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from transcription.config import Config, ConfigError, load_config
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.jobs import TERMINAL_STATUSES, JobManager, JobState
from transcription.ledger import Ledger, LedgerError
from transcription.server import run_server

# Verbatim from the plan's error-taxonomy table (`CLI exit` column).
EXIT_CODES: dict[ErrorKind, int] = {
    ErrorKind.INVALID_REQUEST: 2,
    ErrorKind.UNSUPPORTED_INPUT: 2,
    ErrorKind.AUDIO_DECODE: 3,
    ErrorKind.MODEL_LOAD: 4,
    ErrorKind.PROVIDER_AUTH: 5,
    ErrorKind.PROVIDER_PAYMENT_REQUIRED: 6,
    ErrorKind.PROVIDER_RATE_LIMITED: 6,
    ErrorKind.PROVIDER_UNAVAILABLE: 6,
    ErrorKind.TIMEOUT: 7,
    ErrorKind.CANCELLED: 8,
    ErrorKind.INTERNAL: 1,
}

# Config fields settable from a shared CLI flag; deliberately excludes
# `token`/`provider_api_key` -- credentials never come from argv (FR-9).
_OVERRIDE_FIELDS = ("provider", "model", "model_path", "device", "language", "db_path", "log_level")


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-path", default=None, dest="model_path")
    parser.add_argument("--device", default=None)
    parser.add_argument("--language", default=None)
    parser.add_argument("--db", default=None, dest="db_path")
    parser.add_argument(
        "--allow-root",
        action="append",
        default=None,
        dest="allowed_roots",
        help="an allowed root directory (repeatable)",
    )
    parser.add_argument("--log-level", default=None, dest="log_level")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcription-service",
        description="Standalone transcription microservice: HTTP server or one-shot CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the HTTP server")
    _add_common_flags(serve_parser)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.set_defaults(func=_cmd_serve)

    transcribe_parser = subparsers.add_parser("transcribe", help="transcribe one file and exit")
    _add_common_flags(transcribe_parser)
    transcribe_parser.add_argument("audio", help="path to the audio/video file to transcribe")
    transcribe_parser.add_argument("--out", required=True, dest="output_dir")
    transcribe_parser.set_defaults(func=_cmd_transcribe)

    return parser


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for field_name in _OVERRIDE_FIELDS:
        value = getattr(args, field_name, None)
        if value is not None:
            overrides[field_name] = value

    allowed_roots = getattr(args, "allowed_roots", None)
    if allowed_roots:
        overrides["allowed_roots"] = allowed_roots

    port = getattr(args, "port", None)
    if port is not None:
        overrides["port"] = port

    return overrides


def _cmd_serve(args: argparse.Namespace, config: Config) -> int:
    run_server(config)
    return 0


def _cmd_transcribe(args: argparse.Namespace, config: Config) -> int:
    audio_path = Path(args.audio)
    if not audio_path.is_file():
        # A missing input is treated as an inability to decode it (FR-7's
        # `audio_decode` kind), distinct from an out-of-allowlist path
        # (`invalid_request`) -- the plan's taxonomy gives each its own
        # CLI exit code.
        print(f"input file not found: {audio_path.name}", file=sys.stderr)
        return EXIT_CODES[ErrorKind.AUDIO_DECODE]

    return asyncio.run(_run_transcribe(config, str(audio_path), args.output_dir))


async def _run_transcribe(config: Config, audio_path: str, output_dir: str) -> int:
    try:
        ledger = Ledger(config.db_path)
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CODES[ErrorKind.INTERNAL]

    manager = JobManager(config, ledger)
    try:
        ledger.reconcile_interrupted()
        await manager.start()

        try:
            job_id = await manager.submit(
                audio_path=audio_path,
                output_dir=output_dir,
                language=config.language,
                provider=config.provider,
                model=config.model,
            )
        except ServiceError as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_CODES.get(exc.kind, EXIT_CODES[ErrorKind.INTERNAL])

        job = manager.status(job_id)
        last_reported: float | None = None
        while job.status not in TERMINAL_STATUSES:
            rounded = round(job.progress, 2)
            if rounded != last_reported:
                print(f"progress: {rounded:.2f}", file=sys.stderr)
                last_reported = rounded
            await asyncio.sleep(0.2)
            job = manager.status(job_id)

        return _report_result(config, job)
    finally:
        await manager.aclose()
        ledger.close()


def _report_result(config: Config, job: JobState) -> int:
    if job.status == "succeeded":
        summary = {
            "job_id": job.job_id,
            "status": job.status,
            "provider": job.provider,
            "model": job.model,
            "model_path": config.model_path,
            "output_path": job.output_path,
            "elapsed_sec": job.elapsed_sec,
            "audio_duration_sec": job.audio_duration_sec,
            "cost_usd": job.cost_usd,
        }
        sys.stdout.write(json.dumps(summary) + "\n")
        return 0

    if job.status == "cancelled":
        print("job cancelled", file=sys.stderr)
        return EXIT_CODES[ErrorKind.CANCELLED]

    kind = job.error_kind or ErrorKind.INTERNAL
    print(redact(job.error_message or "transcription failed"), file=sys.stderr)
    return EXIT_CODES.get(kind, EXIT_CODES[ErrorKind.INTERNAL])


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `transcription-service` console script (FR-1)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(config_path=args.config, overrides=_build_overrides(args))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CODES[ErrorKind.INVALID_REQUEST]

    func: Any = args.func
    result: int = func(args, config)
    return result


if __name__ == "__main__":
    sys.exit(main())
