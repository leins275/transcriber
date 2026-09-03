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
| `language` | `TRANSCRIBER_LANGUAGE` | none (constrained auto-detect over `ru`/`en`/`tr`) |
| `filter_hallucinations` | `TRANSCRIBER_FILTER_HALLUCINATIONS` | `true` |
| `job_timeout_sec` | `TRANSCRIBER_JOB_TIMEOUT_SEC` | none |
| `log_level` | `TRANSCRIBER_LOG_LEVEL` | `INFO` |
| `diarize` | `TRANSCRIBER_DIARIZE` | `false` |
| `diarization_model` | `TRANSCRIBER_DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` |
| `diarization_model_path` | `TRANSCRIBER_DIARIZATION_MODEL_PATH` | none (load from the HF hub/cache) |
| `diarization_min_speakers` / `diarization_max_speakers` | `TRANSCRIBER_DIARIZATION_MIN_SPEAKERS` / `..._MAX_SPEAKERS` | none (pyannote estimates) |
| `speaker_match_threshold` | `TRANSCRIBER_SPEAKER_MATCH_THRESHOLD` | `0.5` -- cosine floor for pre-naming a diarized voice already named in a sibling meeting; above `1.0` disables auto-naming |
| `hf_token` | `TRANSCRIBER_HF_TOKEN` (else `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`) | none -- env only, never a CLI flag (FR-9) |
| `llm_model` | `TRANSCRIBER_LLM_MODEL` | the curated-catalog default (`qwen3.5-9b`, the only entry); a config still naming the retired `qwen3.6-35b-a3b` migrates to the default |
| `llm_model_path` | `TRANSCRIBER_LLM_MODEL_PATH` | `<app_dir>/models/llm` |
| `llm_model_repo` / `llm_model_revision` / `llm_model_file` | `TRANSCRIBER_LLM_MODEL_REPO` / `..._REVISION` / `..._FILE` | from the catalog entry for `llm_model` (`llm_catalog.py`); setting them explicitly is the escape hatch for a hand-picked GGUF and wins over the catalog |
| `llm_ctx` | `TRANSCRIBER_LLM_CTX` | `32768` |
| `llm_gpu_layers` | `TRANSCRIBER_LLM_GPU_LAYERS` | `-1` = auto-fit: as many whole layers as free VRAM holds (NVML + GGUF header), rest on CPU; `0` disables; positive pins |
| `llm_threads` | `TRANSCRIBER_LLM_THREADS` | none (llama.cpp picks) |
| `llm_temperature` | `TRANSCRIBER_LLM_TEMPERATURE` | `0.3` |
| `llm_max_output_tokens` | `TRANSCRIBER_LLM_MAX_OUTPUT_TOKENS` | `4096` |
| `llm_think_headroom_tokens` | `TRANSCRIBER_LLM_THINK_HEADROOM_TOKENS` | `2048` (extra output budget for the reasoning `<think>` block on free-text calls) |
| `llm_keep_loaded` | `TRANSCRIBER_LLM_KEEP_LOADED` | `false` (release the multi-GB working set after each LLM job) |
| `vault_root` | `TRANSCRIBER_VAULT_ROOT` | none -- the meetings vault the `index` job walks; falls back to the app schema's `meetings_root` key in the same config file (what a standalone `transcriber-mcp` launch relies on); whatever layer wins is also appended to `allowed_roots` |
| `index_db_path` | `TRANSCRIBER_INDEX_DB_PATH` | `<vault_root>/.transcriber/index.sqlite3` when a vault root is set (the index travels with its vault), else `<app_dir>/data/index.sqlite3` (rebuildable derived data -- deleting it costs one re-index) |
| `search_top_k` | `TRANSCRIBER_SEARCH_TOP_K` | `10` |
| `embedding_model` | `TRANSCRIBER_EMBEDDING_MODEL` | `bge-m3` (the one curated search-embedding GGUF) |
| `embedding_model_repo` / `embedding_model_revision` / `embedding_model_file` | `TRANSCRIBER_EMBEDDING_MODEL_REPO` / `..._REVISION` / `..._FILE` | the `bge-m3` pins; setting them explicitly is the hand-picked-GGUF escape hatch. The file lives in `llm_model_path`, fetched via `POST /v1/embedding-model/download` |

`Config.public()` (what `/health` and log lines may show) never includes
`token` or `hf_token`.

## Derived (LLM) jobs

Beyond `transcribe`, `POST /v1/jobs` accepts a `job_type` with an
`input_path` (a meeting directory under the allowed roots) instead of
`audio_path`:

| `job_type` | reads | writes |
|---|---|---|
| `summarize` | `<meeting>/transcript.json` (+ `speakers.json`) | `<meeting>/summary.md` (with the action items as a section) |
| `export` | one meeting's existing materials (no LLM call) | `<meeting>/export.md` + `<meeting>/<project> - <date> - <title>.pdf` (share-ready name; see `artifacts.export_pdf_filename`), overwritten in place on re-export |
| `diarize` | `<meeting>/source.<ext>` + `<meeting>/transcript.json` (no LLM call; the pyannote engine) | `<meeting>/transcript.json` rewritten in place with speaker labels and the `diarization` block, ids untouched -- see "Speaker diarization" below |

`facts` and `action_items` jobs existed once; both were retired (the
summary carries the notable facts and the action items), and submitting one
answers `invalid_request`. Existing `<meeting>/facts/`,
`<meeting>/action items/` and `<meeting>/exports/` trees stay on disk
untouched and are no longer read — exports no longer include a Facts or
Action-items section, and `POST /v1/items/screenshots` is gone.

All of them run on the built-in llama.cpp runtime -- the only LLM *engine*
this service ships -- against the one GGUF in the curated model catalog
(`llm_catalog.py`): `qwen3.5-9b` (Q5_K_M, ~6.6 GB). There is deliberately
no model switching. `GET /v1/llm-models` lists the catalog with per-model
presence and download status;
`POST`/`DELETE /v1/llm-models/{id}/download` start/cancel one model's
transfer (one transfer at a time across the catalog) and
`DELETE /v1/llm-models/{id}` removes a downloaded file (refused for the
active model, during its transfer, or while an LLM job runs). The legacy
`POST /v1/llm-model/download` trio and the CLI's `download-llm-model`
keep working against the *active* model's slot -- with a one-model catalog
that is always `qwen3.5-9b` unless the `llm_model_file` escape hatch points
elsewhere.
Long transcripts are map-reduced against `llm_ctx`, with the reduce running
in budget-fitted rounds so any transcript length fits the context window;
a completion that hits the output-token cap is retried on smaller input
splits instead of being silently truncated. PDF rendering degrades rather
than failing the export job: the `.md` is always written first.

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

## Hybrid search and the MCP server

`POST /v1/jobs {"job_type": "index"}` incrementally walks `vault_root` into
the search index (`index_db_path`): transcripts (speaker renames applied),
summaries and notes, chunked with breadcrumbs and embedded by the bge-m3
GGUF (CPU-only, fetched via `POST /v1/embedding-model/download` — the
desktop app's Settings exposes this as "Enable vector search"). Documents
indexed while the model was missing are re-embedded automatically on the
first pass after the GGUF arrives (the mtime/hash skip is bypassed for
docs with unembedded chunks, gated on the weights being on disk). The
desktop app fires this quietly after every finished job and note save; a
queued index job absorbs repeat submissions. The index is derived data --
deleting the file costs one re-index.

`POST /v1/search` `{"query", "project"?, "top_k"?, "date"?}` fuses four
channels with weighted Reciprocal Rank Fusion: sqlite-vec cosine kNN, FTS5
BM25 over chunk text, exact-title containment, and trigram fuzz over
titles/speaker names. `date` (the vault's `YYMMDD` or ISO `YYYY-MM-DD`)
hard-filters every channel to that meeting day via the `meeting_date` tag;
an unparseable value degrades to no filter. It runs on the same serial
worker as everything else, and degrades to text-only when the embedding
model (or the sqlite-vec extension) is unavailable.

The chat (`POST /v1/chat`) applies the same day filter automatically: a
question naming dates -- `260902`, `2026-09-02`, `02.09.2026`, or the
words "сегодня"/"today"/"вчера"/"yesterday" (`search/dates.py`) --
retrieves those meeting days and nothing else, so "summarize today's
meetings" can never cite last month's.

**`transcriber-mcp`** is a standalone stdio MCP server over the same vault
and index -- point Claude Desktop at it and ask questions about your
meetings **without the app running** (see `mcp_server.py`'s docstring for
both launch configs). Tools: `hybrid_search`, `list_projects`,
`list_meetings`, `read_transcript` (time-window slicing, speaker renames
applied), `read_summary`, `read_note`. Read-only: it never writes the
vault or the index.

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

A segment whose words fall into two speakers' turns is **cut at the change
of voice** before labelling (`diarization.split_segments_at_turns`), so a
sentence-sized segment holding the end of one remark and the start of the
next speaker's answer becomes two segments with one speaker each; runs
shorter than two words *and* 0.4 s are treated as turn-boundary jitter and
stay with the surrounding voice. Only a transcript being created is split
(ids change); the `diarize` job over an existing transcript never is.

Two prerequisites, both optional by design:

- **The runtime** -- `pyannote.audio` and the torch stack under it. A dev
  environment gets it from the `diarization` extra (`uv sync --extra
  diarization`, CPU torch from PyPI). The installed app never bakes it:
  `POST /v1/diarization-runtime/download` fetches the pinned wheel set --
  every package the extra adds on top of the baked environment, with
  torch/torchaudio swapped for their `cu126` CUDA builds (~2.7 GB in all)
  -- into `<app_dir>/runtime/diarization/`, which the diarizer puts on
  `sys.path` before importing pyannote. The manifest
  (`diarization_runtime_packages.py`) is generated from `uv.lock` by
  `scripts/gen_diarization_runtime.py`; `make lint` fails when it drifts.
  Nothing imports any of it until the first diarized job runs.
- **The models and a Hugging Face token**: `pyannote/speaker-diarization-3.1`
  and `pyannote/segmentation-3.0` are gated -- accept their terms on the
  hub once (signed in as the token's owner), then supply a read token
  (`hf_token` in the config file, `TRANSCRIBER_HF_TOKEN`, else
  `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`). `POST /v1/diarization-model/download`
  -- or the CLI's `download-diarization-models [--out DIR]` -- snapshots
  the three repos at pinned revisions into `<app_dir>/models/diarization/`
  (the hub-cache layout, `PYANNOTE_CACHE`) and pins each `refs/main` to its
  snapshot; from then on the pipeline loads **offline** (the hub is never
  consulted and the token is not needed at load time), so the pin holds.
  A gated refusal names the repo whose terms to accept. **The installer
  ships these snapshots**: the release build runs that CLI with the
  workflow's `HF_TOKEN` secret and bundles the result into
  `<install dir>\models\diarization\`, so an installed app's operator never
  handles a token (`docs/setup.md`). Without the fetch (a dev
  environment), the diarizer downloads on first use with the token, as
  before. Alternatively point `diarization_model_path` at a local snapshot
  directory (containing the pipeline's `config.yaml`) for fully offline
  loads.

`GET /v1/diarization/status` reports which prerequisites are met
(`runtime_present`, `model_present`, `token_present`, `gpu_present` -- the
CUDA runtime is the only build offered) and whether `diarize` is on.

### `diarize`: speakers for an already-transcribed meeting

`POST /v1/jobs {"job_type": "diarize", "input_path": <meeting dir>,
"output_dir": <meeting dir>}` runs the diarization pass over the meeting's
`source.<ext>` and writes the speaker labels and the `diarization` block
(embeddings included) into its **existing** `transcript.json`, keeping
every segment id -- the operator's `speakers.json` is keyed by them and is
never touched (it already outranks the labels wherever they are read). This
is the backfill behind cross-meeting recognition: a meeting labelled by
hand while it had no diarization becomes voice memory for every later
recording in its project. Unlike the transcribe path, a failing pass fails
this job (identification is its whole point); a meeting without a
recording or a transcript is refused up front.

Diarization **degrades, never fails the job**: if the pass cannot run
(extra not installed, model not fetchable, runtime error), the transcript
is still written -- without speakers -- and the failure is attributed in
the document's `diarization` block (`status: "failed"`, `error_kind`,
`error_message`) and the service log. Cancelling the job mid-pass cancels
the job as usual.

When the pipeline also yields per-speaker voice embeddings, they are
stored in the `diarization` block (`speaker_embeddings`, keyed by display
label) and immediately put to work: **cross-meeting speaker recognition**
compares each new voice against voices the operator has already named in
sibling meetings (their `speakers.json` joined to their stored
embeddings) and pre-fills the new meeting's `speakers.json` on a match at
or above `speaker_match_threshold`. Additive only -- an assignment the
operator made by hand is never overwritten -- and best-effort: any failure
is a job warning, never a failed job.

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

The speaker-identification models bundled with the Windows installer are
redistributed under their own licenses: `pyannote/speaker-diarization-3.1`
and `pyannote/segmentation-3.0` (MIT, © CNRS / Hervé Bredin et al.) and
`pyannote/wespeaker-voxceleb-resnet34-LM` (CC BY 4.0, the WeSpeaker
project's VoxCeleb ResNet34-LM model as packaged by pyannote). Cite
*Bredin, "pyannote.audio 2.1 speaker diarization pipeline: principle,
benchmark, and recipe" (Interspeech 2023)* and *Wang et al., "WeSpeaker: a
research and production oriented speaker embedding toolkit" (ICASSP 2023)*.
