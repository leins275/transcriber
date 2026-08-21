# transcription-service

A standalone Python microservice that turns a meeting recording file into a
`transcript.json`, using whisper `large-v3` locally (faster-whisper /
CTranslate2) with a provider abstraction that makes swapping to a cloud STT
provider a config change. See `../../specs/transcription-service/spec.md` for
the full specification.

This package is self-contained: it imports nothing from the rest of the
repository and builds, lints, type-checks and tests standalone.

## QA commands

**No `make` targets exist here, and `make` is not installed on this
machine.** The root `Makefile` (if any) belongs to F4's batch, not this
package. Run these four commands from inside `services/transcription/`:

| Purpose | Command |
|---|---|
| format | `uv run ruff format --check .` |
| lint | `uv run ruff check .` |
| types | `uv run mypy` |
| tests | `uv run pytest -q` |

The default `pytest` run is model-free, GPU-free and network-free, and
finishes in under 30 seconds (`addopts = -m "not gpu"` deselects the opt-in
GPU integration test). To run the GPU-only test on the reference machine
(RTX 4070, real `large-v3` weights required):

```
uv run pytest -m gpu
```

It self-skips cleanly, with an explanatory message, unless a sample is
configured -- see `tests/data/README.md`.

## CUDA extras

CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) are an
optional dependency group, Windows-only, so the default `uv sync` used for
tests stays light:

```
uv sync --extra cuda
```

CTranslate2 4.8 (the faster-whisper backend) requires **cuDNN 9** --
pinning cuDNN 8 silently breaks CUDA at runtime, which is why the `cuda`
extra pins `nvidia-cudnn-cu12>=9,<10`.

## Configuration

Configuration is layered, lowest to highest precedence: built-in defaults <
`<app_dir>/config.json` < `TRANSCRIBER_*` environment variables < explicit
overrides (CLI flags). The app folder is located by `TRANSCRIBER_APP_DIR`,
falling back to the parent of the running executable's directory. F4 owns
`config.json`'s overall schema; this service reads the keys it knows and
ignores the rest, except `vault_root`, which it folds into `allowed_roots`.

| Config key | `TRANSCRIBER_*` env var | Default |
|---|---|---|
| `model` | `TRANSCRIBER_MODEL` | `large-v3` |
| `model_path` | `TRANSCRIBER_MODEL_PATH` | `<app_dir>/models` |
| `device` | `TRANSCRIBER_DEVICE` | `auto` (`cuda` if a device is probed, else `cpu`) |
| `compute_type` | `TRANSCRIBER_COMPUTE_TYPE` | `float16` on cuda / `int8` on cpu |
| `provider` | `TRANSCRIBER_PROVIDER` | `local` |
| `cloud_model` | `TRANSCRIBER_CLOUD_MODEL` | none |
| `provider_api_key` | `TRANSCRIBER_PROVIDER_API_KEY` (else `OPENAI_API_KEY`/`GROQ_API_KEY`) | none -- env only, never the config file or a CLI flag (FR-9) |
| `db_path` | `TRANSCRIBER_DB_PATH` | `<app_dir>/data/jobs.sqlite3` |
| `allowed_roots` | `TRANSCRIBER_ALLOWED_ROOTS` (`os.pathsep`-separated) | empty (fail closed) |
| `token` | `TRANSCRIBER_TOKEN` | auto-generated, >= 32 chars |
| `language` | `TRANSCRIBER_LANGUAGE` | none (auto-detect) |
| `filter_hallucinations` | `TRANSCRIBER_FILTER_HALLUCINATIONS` | `true` |
| `max_cloud_upload_mb` | `TRANSCRIBER_MAX_CLOUD_UPLOAD_MB` | `25` |
| `job_timeout_sec` | `TRANSCRIBER_JOB_TIMEOUT_SEC` | none |
| `log_level` | `TRANSCRIBER_LOG_LEVEL` | `INFO` |

`Config.public()` (what `/health` and log lines may show) never includes
`token` or `provider_api_key`.

### Model weights and CUDA runtime are prerequisites, not this service's job

The local provider always passes `local_files_only=True` to `faster-whisper`
(FR-3 acceptance: "verified offline"). It **never** downloads weights: if
`model_path` (default `<app_dir>/models`) does not already contain a cached
`large-v3` snapshot, every job against the local provider fails with
`error_kind="model_load"` (CLI exit `4`) naming the configured path.
Installing the weights there is F4's job, not this service's.

Likewise, `device: auto` picks `cuda` whenever
`ctranslate2.get_cuda_device_count() > 0` reports a device is *present* --
it does not verify the CUDA runtime actually loads, and there is no CPU
fallback if it doesn't. A default `uv sync` (no `--extra cuda`) omits the
CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`), so on a
machine with an NVIDIA GPU but without those wheels installed, every local
job fails with `error_kind="model_load"` (e.g. `cublas64_12.dll is not
found`) even though `/health`/the ledger correctly report `device="cuda"`.
Run `uv sync --extra cuda` (see "CUDA extras" above) before pointing the
local provider at a real job on such a machine.

## The ready-line contract (F3 depends on this)

`serve` prints **exactly one** line to stdout for the whole process
lifetime, then never touches stdout again -- every other log line goes to
stderr as JSON:

```json
{"event": "listening", "port": 51234, "token": "<bearer token>", "pid": 42}
```

The listening socket is bound (and its real port read back, for
`--port 0`) *before* this line is printed, so a caller that sees the line
can connect immediately. The server binds `127.0.0.1` only.

## CLI exit codes

`transcribe` maps every failure kind in the error taxonomy to a distinct,
documented exit code (`serve` exits 0 only via a clean shutdown):

| Exit code | `ErrorKind` | Meaning |
|---|---|---|
| 0 | -- | success |
| 1 | `internal` | unclassified/unexpected error |
| 2 | `invalid_request`, `unsupported_input` | validation, path allowlist, unsupported extension |
| 3 | `audio_decode` | missing input file / decode failure |
| 4 | `model_load` | local model failed to construct |
| 5 | `provider_auth` | provider 401/403 |
| 6 | `provider_payment_required`, `provider_rate_limited`, `provider_unavailable` | provider 402/429/5xx |
| 7 | `timeout` | request aborted, no response in time |
| 8 | `cancelled` | job was cancelled |

No flag is named like a secret: `--api-key` is not a defined flag.
Credentials come only from the environment or `config.json`, never argv
(FR-9).

## Why `cost_usd` is `NULL` for the local provider (NFR-8)

The local `faster-whisper` provider has no per-request cost -- there is no
metered API call to price. `cost_usd` is stored as SQL `NULL` (and
serialized as JSON `null`) in that case, never `0.0`, because `0.0` would
falsely claim "this ran for free" when the real answer is "this question
does not apply." The cloud (`litellm`) provider always reports a real
`cost_usd`/`currency` pair when the provider prices the request, and
`NULL`/a logged warning when even litellm's cost hooks come back empty --
never a fabricated `0.0`.

## Idle memory footprint (NFR-6)

Measured on the reference machine with the **real** default configuration
(`provider: local`, no fake registered): a served process sits at about
**53-56 MB** working-set RSS before any job runs, including after a
`GET /health` call -- `/health` never constructs (and so never imports)
the provider library, so it does not move this number (E15). RSS grows
only once a job actually runs and the worker thread resolves/imports the
provider for the first time; even so, it stays comfortably under the
300 MB budget.

## Attribution

Portions of this package are adapted from [Vexa](https://github.com/Vexa-ai/vexa)
(Apache License, Version 2.0). See `NOTICE` for the full list of adapted
files and their origin paths.
