# transcription-service

A standalone Python microservice that turns a meeting recording file into a
`transcript.json`, using whisper `large-v3` locally (faster-whisper /
CTranslate2) behind a lazily-resolved provider registry that registers
exactly one provider, `local`. Every model runtime this service ships runs
on this machine; no audio or transcript text is ever sent anywhere. See
`../../specs/transcription-service/spec.md` for the full specification.

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
| `provider` | `TRANSCRIBER_PROVIDER` | `local` (the only registered provider) |
| `db_path` | `TRANSCRIBER_DB_PATH` | `<app_dir>/data/jobs.sqlite3` |
| `allowed_roots` | `TRANSCRIBER_ALLOWED_ROOTS` (`os.pathsep`-separated) | empty (fail closed) |
| `token` | `TRANSCRIBER_TOKEN` | auto-generated, >= 32 chars |
| `language` | `TRANSCRIBER_LANGUAGE` | none (auto-detect) |
| `filter_hallucinations` | `TRANSCRIBER_FILTER_HALLUCINATIONS` | `true` |
| `job_timeout_sec` | `TRANSCRIBER_JOB_TIMEOUT_SEC` | none |
| `log_level` | `TRANSCRIBER_LOG_LEVEL` | `INFO` |
| `diarize` | `TRANSCRIBER_DIARIZE` | `false` |
| `diarization_model` | `TRANSCRIBER_DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` |
| `diarization_model_path` | `TRANSCRIBER_DIARIZATION_MODEL_PATH` | none (load from the HF hub/cache) |
| `diarization_min_speakers` / `diarization_max_speakers` | `TRANSCRIBER_DIARIZATION_MIN_SPEAKERS` / `..._MAX_SPEAKERS` | none (pyannote estimates) |
| `hf_token` | `TRANSCRIBER_HF_TOKEN` (else `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`) | none -- env only, never a CLI flag (FR-9) |
| `llm_model` | `TRANSCRIBER_LLM_MODEL` | the curated-catalog default (`qwen3.5-9b`); an install whose disk still holds the legacy `qwen3.6-35b-a3b` GGUF and whose config has no `llm_model` key stays on it |
| `llm_model_path` | `TRANSCRIBER_LLM_MODEL_PATH` | `<app_dir>/models/llm` |
| `llm_model_repo` / `llm_model_revision` / `llm_model_file` | `TRANSCRIBER_LLM_MODEL_REPO` / `..._REVISION` / `..._FILE` | from the catalog entry for `llm_model` (`llm_catalog.py`); setting them explicitly is the escape hatch for a hand-picked GGUF and wins over the catalog |
| `llm_ctx` | `TRANSCRIBER_LLM_CTX` | `32768` |
| `llm_gpu_layers` | `TRANSCRIBER_LLM_GPU_LAYERS` | `-1` = auto-fit: as many whole layers as free VRAM holds (NVML + GGUF header), rest on CPU; `0` disables; positive pins |
| `llm_threads` | `TRANSCRIBER_LLM_THREADS` | none (llama.cpp picks) |
| `llm_temperature` | `TRANSCRIBER_LLM_TEMPERATURE` | `0.3` |
| `llm_max_output_tokens` | `TRANSCRIBER_LLM_MAX_OUTPUT_TOKENS` | `4096` |
| `llm_think_headroom_tokens` | `TRANSCRIBER_LLM_THINK_HEADROOM_TOKENS` | `2048` (extra output budget for the reasoning `<think>` block on free-text calls) |
| `llm_keep_loaded` | `TRANSCRIBER_LLM_KEEP_LOADED` | `false` (release the multi-GB working set after each LLM job) |

`Config.public()` (what `/health` and log lines may show) never includes
`token` or `hf_token`.

## Derived (LLM) jobs

Beyond `transcribe`, `POST /v1/jobs` accepts a `job_type` with an
`input_path` (a meeting directory under the allowed roots) instead of
`audio_path`:

| `job_type` | reads | writes |
|---|---|---|
| `summarize` | `<meeting>/transcript.json` (+ `speakers.json`) | `<meeting>/summary.md` |
| `action_items` | same | `<project>/action items/<slug>/<slug>.md` + `screenshot-*.png` |
| `facts` | same | `<project>/facts/<slug>/...` (same shape) |
| `export` | one meeting's existing materials (no LLM call) | `<meeting>/exports/<YYMMDD>/export.md` + `<project> - <date> - <title>.pdf` (share-ready name; see `artifacts.export_pdf_filename`) |

All of them run on the built-in llama.cpp runtime -- the only LLM *engine*
this service ships -- against a GGUF from the curated model catalog
(`llm_catalog.py`): `qwen3.5-9b` (Q5_K_M, ~6.6 GB, the default) or
`qwen3.6-35b-a3b` (Q4_K_M, ~20 GB). `GET /v1/llm-models` lists the catalog
with per-model presence and download status;
`POST`/`DELETE /v1/llm-models/{id}/download` start/cancel one model's
transfer (one transfer at a time across the catalog) and
`DELETE /v1/llm-models/{id}` removes a downloaded file (refused for the
active model, during its transfer, or while an LLM job runs). The legacy
`POST /v1/llm-model/download` trio and the CLI's `download-llm-model`
keep working against the *active* model's slot. Which model is active is
the `llm_model` config key -- the desktop app writes it on selection and
restarts the service.
Long transcripts are map-reduced against `llm_ctx`, with the reduce running
in budget-fitted rounds so any transcript length fits the context window;
a completion that hits the output-token cap is retried on smaller input
splits instead of being silently truncated. Extraction output is
grammar-constrained JSON with one bounded repair retry; a chunk that still
fails is skipped with a job warning, and the job fails
(`error_kind: "llm_output"`) only when no chunk produced usable output. Screenshots come from the
recording's video track via PyAV at the
timestamps the model cites -- an audio-only recording simply gets none,
and a failed screenshot pass degrades (items are written without images,
the job records a warning) rather than failing the job. PDF rendering
degrades the same way: the `.md` is always written first.

Every job type shares the one serial worker: an LLM job queued behind a
transcription waits, and vice versa -- which is also what guarantees
whisper and the LLM never infer concurrently. With the default
`llm_keep_loaded: false` the GGUF's working set is released after each
LLM job.

### GPU offload

`llama-cpp-python` comes in two mutually exclusive uv extras of the same
pinned version: `llm-cpu` (what the installer bakes) and `llm-cuda` (the
cu124 wheel plus the `nvidia-cuda-runtime-cu12`/`nvidia-cublas-cu12`
runtime wheels, whose DLL directories `runtime_dlls` registers at
startup). The desktop app's dev sidecar passes `--extra llm-cuda`
automatically when `nvidia-smi` is on PATH, `--extra llm-cpu` otherwise.
With the default `llm_gpu_layers: -1`, each model load measures free VRAM
via NVML, reads the layer count from the GGUF header and offloads as many
whole layers as fit (the decision is logged); the rest run on CPU.

One uv gotcha when switching variants by hand: both wheels share a name
and version, so `uv sync --extra llm-cuda` over an already-installed CPU
wheel audits it as satisfied. Force the swap once with
`uv sync --extra llm-cuda --reinstall-package llama-cpp-python`.

## Speaker diarization (pyannote)

With `diarize` on (config default, `--diarize`, or a per-job
`"diarize": true` in `POST /v1/jobs`), a pyannote speaker-diarization pass
runs after transcription: each segment gains a `"speaker"` field
(`"Speaker 1"`, `"Speaker 2"`, ... numbered by first speech), and the
document gains a `diarization` block recording the model, device and
distinct speaker count. Attribution is word-timestamp-weighted majority
voting against the diarized turns, so a segment brushing a neighbouring
turn's edge still lands on the voice that actually spoke it; a segment no
turn can claim keeps no `speaker` at all rather than a fabricated guess.

Two prerequisites, both optional by design:

- **The `diarization` extra** (`uv sync --extra diarization`) installs
  `pyannote.audio` and the torch stack under it. Nothing imports these
  until the first diarized job runs.
- **A Hugging Face token** (`TRANSCRIBER_HF_TOKEN`, else
  `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`): the default
  `pyannote/speaker-diarization-3.1` model is gated -- accept its terms on
  the hub once, then supply a token. Alternatively point
  `diarization_model_path` at a local snapshot directory (containing the
  pipeline's `config.yaml`) for fully offline loads.

Diarization **degrades, never fails the job**: if the pass cannot run
(extra not installed, model not fetchable, runtime error), the transcript
is still written -- without speakers -- and the failure is attributed in
the document's `diarization` block (`status: "failed"`, `error_kind`,
`error_message`) and the service log. Cancelling the job mid-pass cancels
the job as usual.

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
does not apply." Since every shipping runtime is local, `cost_usd` is
always `NULL` in practice; the column and the JSON field stay because they
are part of the ledger/API contract the desktop app consumes, and a
provider that did price its requests would report a real
`cost_usd`/`currency` pair rather than a fabricated `0.0`.

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
