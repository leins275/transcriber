---
slug: transcription-service
created: 2026-08-21
status: approved
---

# Spec: Python transcription microservice

## Summary

A standalone Python microservice that turns a meeting recording file into a `transcript.json`, using **whisper large-v3 locally** (faster-whisper / CTranslate2) with a provider abstraction that makes swapping to a cloud STT provider a config change. Every job is logged to **sqlite** with elapsed time and, for cloud providers, monetary cost. The service is consumed over **localhost HTTP** by the Tauri 2 desktop app (F3) and can also be driven from a CLI so the transcription hypothesis can be validated before any UI exists. No summarization in this MVP.

## Problem & context

The operator works across several projects, records meetings, and today dumps the files into a `Meetings` folder on Windows with no transcript. The MVP exists to validate the transcription hypothesis first (`specs/_intake/idea/intake.md`, F2 task text: *"хочу проверить для начала гипотезу и сделать MVP именно с частью транскрибации"*).

Repository state at spec time: the repo is **greenfield** — `D:\Local\Git\transcriber\` contains only `IDEA.md`, `specs/`, and a gitignored read-only clone of **vexa** (`.gitignore` line 1: `vexa/`). There is no `pyproject.toml`, no `package.json`, no `Makefile`, and `make` is not installed on this machine. So every "detected" stack row below is a probe result about the machine plus a choice this spec makes, not an inventory of existing code.

Machine probes (2026-08-21):
- GPU: **NVIDIA GeForce RTX 4070, 12282 MiB, driver 591.86** (`nvidia-smi`) — CUDA path is viable; large-v3 in `float16` fits comfortably.
- `uv 0.8.17` at `C:\Users\<user>\.local\bin\uv.exe`; CPython **3.12.11** and 3.13.7 already managed by uv.
- **`ffmpeg` is NOT on PATH** — this drives FR-7.
- `make` is not installed — QA entrypoints must be `uv run …` first, Makefile second.

### Vexa reuse verdict (research task)

**Verdict: lift a small, precisely identified core (~150 lines of ideas and constants, not a dependency); write the service from scratch around it.** Vexa is Apache-2.0 (`D:\Local\Git\transcriber\vexa\LICENSE`), and its STT worker is genuinely standalone — but it is shaped for a *bot streaming 15-second windows from a live meeting*, not for a one-hour file on disk.

What vexa actually has, and how close it is:
- `D:\Local\Git\transcriber\vexa\core\meetings\services\transcription\` — a **580-line single-file FastAPI service** wrapping faster-whisper behind OpenAI's `POST /v1/audio/transcriptions`, with `Dockerfile` (CUDA base) / `Dockerfile.cpu`, its own `pyproject.toml` (uv, `package = false`), and a 3-file pytest suite. Its own README states it "imports only third-party + its own module (no cross-brick edges)" — so it is copyable without dragging the monorepo in.

**Lift (adapt, with attribution):**
1. `…\src\transcription\main.py` lines 227–245 — the `WhisperModel(model_size_or_path=…, device=…, compute_type=…, download_root=…)` load pattern and the INT8/float16 compute-type reasoning. We must replace its hardcoded `"download_root": "/app/models"` with a configurable Windows path (F4 installs the model there).
2. `main.py` lines 462–484 — the faster-whisper segment → OpenAI `verbose_json` dict mapping (`start/end/text/avg_logprob/compression_ratio/no_speech_prob`, optional `words`). Adopt verbatim as our `transcript.json` segment shape: it is the de-facto interop schema and costs nothing to match.
3. `main.py` lines 99–118 (`_looks_like_silence`, `_looks_like_hallucination`) plus `…\core\meetings\modules\whisper\src\confidence.ts` — hallucination/low-confidence thresholds (`no_speech_prob > 0.6 && avg_logprob < -1.0`, `compression_ratio > 2.4`, `avg_logprob < -1.3`). These constants are hard-won and directly relevant: whisper on silence invents subtitle-credit junk. Port to Python (FR-12).
4. `…\core\meetings\modules\whisper\src\transcription-client.ts` lines 53–78 — the `TranscriptionFaultKind` taxonomy (`payment_required | unauthorized | rate_limited | unavailable | timeout | bad_request | unknown`) with `retryable`, reinforced by `…\docs\docs\how-to\custom-stt.mdx` lines 114–124 ("preserve the raw HTTP status… do not collapse every failure into 'transcription failed'"). Port as our provider error taxonomy (FR-8).
5. `…\tests\conftest.py` — the pattern that makes the HTTP contract testable with **no GPU, no model download, no network** (lazy model load + `TestClient` without lifespan + a monkeypatched sentinel). Port directly (FR-15).

**Do not reuse:**
- The entire load-management layer (`main.py` lines 165–206, 300–356: dual semaphores, realtime/deferred tiers, `FAIL_FAST_WHEN_BUSY` 503s, `MAX_QUEUE_SIZE`). It exists because many bots stream concurrent windows at one GPU. A single-user desktop app transcribing one file at a time needs a serial worker and nothing else.
- Deployment: `Dockerfile`, `Dockerfile.cpu`, `…\deploy\transcription\{docker-compose.yml, nginx.conf}` — Linux/GPU-server/nginx-LB shaped; we ship a Windows sidecar process.
- Its audio path (`main.py` lines 366–408: multipart upload → `soundfile` → **`ffmpeg` subprocess fallback**). It is a streaming-window path. We pass a file path to faster-whisper, which decodes via **PyAV** (`av 17.1.0`, a hard dependency of `faster-whisper 1.2.1` — see vexa's own `uv.lock` lines 218–228). PyAV wheels bundle the ffmpeg libraries, so we need no `ffmpeg.exe` — which matters because it is not on this machine.
- Everything above the STT worker (bot fleet, meeting-api, collector, gmeet/mixed pipelines, agents, terminal client) — live-meeting product, irrelevant here.
- The TypeScript client — our consumer is Rust (F3).

**What vexa gives us nothing for:** the operator's two explicit requirements. Searching the whole clone, **sqlite** appears only in unrelated modules (`core/meetings/modules/recording/src/recording-codec.ts`, `core/meetings/services/desktop/src/desktop.ts`) — there is no job/cost ledger anywhere. **litellm** appears only as prose about an upstream OpenAI-compatible endpoint someone else might run (`docs/docs/configuration.mdx:22`, `core/agent/llm/anthropic_api.py:4`) — never as a dependency of the STT path. Job logging, elapsed/cost accounting and the litellm provider layer are 100% ours.

**Attribution obligation:** Apache-2.0 §4 — files containing adapted vexa code carry the Apache-2.0 header and a `NOTICE` naming Vexa (Vexa-ai/vexa) and the origin path. FR-13.

## Users

- **Operator (single local user)** — indirectly, through the Tauri app (F3): drops a recording in, watches progress, gets a transcript in the meeting folder.
- **Operator as developer/debugger** — directly, through the CLI one-shot command and the sqlite ledger, to validate the hypothesis, compare providers, and see what a transcription cost in minutes and dollars.
- **F3 (Tauri app)** — the machine consumer of the HTTP API; the only production caller.

## Profiles

- `cli` — matched. This feature is a headless service plus a console entry point: an argv/HTTP/exit-code contract with **no UI layer of its own**. The profile's negative signal holds for the code this feature owns — no `react`/`vue`/`tauri`/`electron`/Qt dependency belongs in this Python package (proof: the repo has no `package.json` or `src-tauri/` at all today, and by the batch boundaries the Tauri app is F3's package, not this one).
- `desktop` — **not matched for this feature.** No `src-tauri/tauri.conf.json` or `Cargo.toml` exists in the repo. F3 will match it. It is named here only so the architect does not attach desktop/IPC rules to this feature's tasks — but note the *deployment* reality below.
- `web` — **not matched.** No `manage.py`, no Django, no browser-facing UI. The feature serves HTTP, but the `web` profile is about a Django backend and/or a browser UI; neither exists. Do not attach `frontend-toolkit` or `django-toolkit` skills here.

Because the repository is empty, detection was run against the machine and against the batch's declared boundaries rather than an existing tree; this is stated plainly rather than papered over.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Language / runtime | Python 3.12 (uv-managed) | `uv python list` shows `cpython-3.12.11` installed locally; faster-whisper's onnxruntime needs cp311+ (vexa `pyproject.toml`, `requires-python = ">=3.11"`) |
| Service | FastAPI + uvicorn, loopback HTTP | chosen; reference implementation `vexa\core\meetings\services\transcription\src\transcription\main.py` |
| STT (local) | faster-whisper 1.2.x / CTranslate2 4.x, whisper **large-v3** | operator requirement; vexa `uv.lock` lines 165–172, 218–228 |
| Audio decode | PyAV (`av` 17.x, transitive via faster-whisper) | vexa `uv.lock` lines 37–43; `ffmpeg` absent from PATH on this machine |
| Accelerator | CUDA on RTX 4070 12 GB, CPU fallback | `nvidia-smi`: `NVIDIA GeForce RTX 4070, 12282 MiB, 591.86` |
| Provider layer (cloud) | litellm (audio transcription API + cost hooks) | operator requirement; no prior art in vexa |
| Storage | SQLite (stdlib `sqlite3`, WAL) | operator requirement |
| Package/dep manager | `uv` | `uv 0.8.17` at `C:\Users\<user>\.local\bin\uv.exe`; global user preference mandates `uv` over `python`/`pip` |
| Testing | pytest (+ `fastapi.testclient`) | chosen; port of `vexa\core\meetings\services\transcription\tests\conftest.py` |
| Consumer | Tauri 2 / Rust (F3) over localhost HTTP | batch boundary; not in this feature's package |
| OS target | Windows 11 only | operator decision; `# Ключевая функциональность` "начать можно с винды" |

Makefile QA targets present: **none — there is no Makefile in the repo, and `make` is not installed on this machine** (`make: command not found`). This feature introduces the QA entrypoints as `uv run` commands (`uv run ruff format`, `uv run ruff check`, `uv run mypy`, `uv run pytest`) and, as a convenience wrapper only, a `Makefile` with `format`/`lint`/`type`/`test` targets that shell out to the same `uv run` commands. Agents must not assume `make` works here.

## Functional requirements

- **FR-1** (must): The service is a self-contained Python package with its own `pyproject.toml` managed by `uv`, living in its own directory (proposed `services/transcription/`). It imports nothing from F1/F3/F4 and is runnable standalone: `uv run transcription-service serve`.
- **FR-2** (must): It exposes an HTTP API bound to **127.0.0.1 only**, with an asynchronous job model:
  - `POST /v1/jobs` — body `{ "audio_path", "output_dir", "language"?, "provider"?, "model"?, "meeting"? }` → `202 { "job_id" }`.
  - `GET /v1/jobs/{id}` — `{ status: queued|running|succeeded|failed|cancelled, progress: 0..1, elapsed_sec, audio_duration_sec, provider, cost_usd, error_kind?, error_message? }`.
  - `GET /v1/jobs/{id}/result` — the transcript document (same content as the written `transcript.json`).
  - `GET /v1/jobs?limit=&status=` — recent jobs from the sqlite ledger.
  - `GET /health` — `{ status, version, provider, model, device, model_state: unloaded|loading|loaded }`.
  Jobs execute **serially**, one at a time; additional submissions queue.
- **FR-3** (must): Local transcription uses faster-whisper with **whisper large-v3**, loading the model from a **configurable directory path** (F4's installer downloads it there). The model is loaded lazily on the first job and cached in-process for the service's lifetime — never reloaded per job. Device resolution is `auto` by default (CUDA when available, else CPU) and is overridable, as are `compute_type` and `model` id/path.
- **FR-4** (must): Transcription is behind a single internal provider interface — roughly `transcribe(audio_path, language) -> TranscriptResult` — with two implementations: a **local faster-whisper provider** and a **litellm-backed cloud provider**. Changing provider is a config value plus (for cloud) an API key; no caller-side code changes. A per-request `provider` field overrides the configured default. No provider-specific branching may exist outside a provider adapter module.
- **FR-5** (must): Every job — including failures and cancellations — is recorded as one row in a **SQLite** database at a configurable path, with at minimum: `job_id`, `created_at`, `started_at`, `finished_at`, `status`, `provider`, `model`, `device`, `source_path`, `output_path`, `audio_duration_sec`, `elapsed_sec`, `realtime_factor`, `cost_usd` (NULL for local), `currency`, `language`, `segment_count`, `error_kind`, `error_message`, `service_version`. The DB opens in WAL mode, creates its schema on first run, and carries a schema version (`PRAGMA user_version`).
- **FR-6** (must): On success the service writes **`transcript.json`** into the caller-supplied `output_dir` (the per-meeting vault folder owned by F1: `root/<PROJECT>/<date> - <Title>/`), **atomically** (temp file in the same directory + `os.replace`). Schema v1:
  `{ schema_version, created_at, source: {path, filename, duration_sec}, provider: {name, model, device, compute_type}, language, language_probability, text, segments: [{id, start, end, text, avg_logprob, no_speech_prob, compression_ratio, words?}], stats: {elapsed_sec, realtime_factor, cost_usd, currency} }`.
  The `segments` element shape is the OpenAI `verbose_json` shape as produced by vexa's mapper.
- **FR-7** (must): The service accepts the container/codec formats produced by meeting recorders — at minimum `.mp4`, `.m4a`, `.mp3`, `.wav`, `.webm`, `.mkv` — **without requiring an external `ffmpeg` binary on PATH**, by letting faster-whisper decode the path through its bundled PyAV dependency. An undecodable input fails with `error_kind = audio_decode` and a message naming the file and the underlying decoder error.
- **FR-8** (must): Failures carry an attributable taxonomy, surfaced identically in the job record, the sqlite row and the HTTP body: `audio_decode | unsupported_input | model_load | provider_auth | provider_rate_limited | provider_payment_required | provider_unavailable | timeout | invalid_request | cancelled | internal`. Provider HTTP status and sanitized provider message are preserved; failures are never collapsed into a generic "transcription failed".
- **FR-9** (must): Security boundary. `audio_path` and `output_dir` are resolved and validated to lie under a configured allowlist of roots (the vault root and any explicitly configured extra root); traversal, UNC and symlink escapes are rejected with `invalid_request`. The listener binds `127.0.0.1` only. A bearer token is required when configured, and the service generates one at startup by default. Cloud provider API keys are read only from environment/config file — never from a request body and never from argv.
- **FR-10** (should): A one-shot CLI subcommand — `uv run transcription-service transcribe <audio> --out <dir> [--provider …]` — performs the same work in-process (same provider, same sqlite logging, same `transcript.json`), writes progress to **stderr** and a machine-readable summary object to **stdout**, and exits `0` on success / distinct nonzero codes per failure class. This is how the hypothesis gets validated before F3 exists.
- **FR-11** (should): `DELETE /v1/jobs/{id}` cancels a queued or running job; the job ends as `cancelled` and is still written to sqlite with its elapsed time. No partial `transcript.json` is left behind.
- **FR-12** (should): Silence and hallucination filtering, ported from vexa's heuristics (`no_speech_prob`/`avg_logprob`/`compression_ratio` thresholds), applied to local-provider output and switchable off in config. Filtered segments are dropped from `text` but the counts are reported in the job record.
- **FR-13** (must): Any file containing code adapted from vexa carries the Apache-2.0 header plus a source comment naming the origin path, and the repository gains a `NOTICE` file attributing Vexa (Vexa-ai/vexa, Apache-2.0).
- **FR-14** (should): The service emits structured JSON-lines logs on stderr, and on startup prints exactly one machine-readable ready line to **stdout** — `{"event":"listening","port":<n>,"token":"<t>","pid":<p>}` — so a supervising parent (F3/Tauri sidecar) can discover the port. `--port 0` selects a free port; a fixed port is configurable.
- **FR-15** (should): A pytest suite covering the HTTP contract, path validation, the sqlite ledger, the error taxonomy and `transcript.json` schema, all runnable **without a GPU, without downloading a model and without network** (lazy model load + monkeypatched provider), plus one opt-in integration test (marker `gpu`) that transcribes a short bundled sample with the real local model.
- **FR-16** (must): Configuration comes from a config file in the app folder (path overridable) with environment-variable overrides (`TRANSCRIBER_*`) and CLI flags on top. Configurable at minimum: model path/id, device, compute type, default provider, provider credentials, sqlite path, allowed roots, port, token, language default, hallucination filter toggle.

## Non-functional requirements

- **NFR-1**: Cold start to accepting HTTP (`GET /health` returns 200) < **3 s**, because the model loads lazily and not at import.
- **NFR-2**: First local model load from the local model directory completes in < **60 s** on the reference machine (RTX 4070, `float16`); subsequent jobs add **0 s** of load time.
- **NFR-3**: Local transcription of a 60-minute recording on the reference GPU completes with **realtime_factor ≤ 0.3** (i.e. ≤ 18 minutes), measured as `elapsed_sec / audio_duration_sec` and recorded per job.
- **NFR-4**: `GET /v1/jobs/{id}` p95 < **50 ms** while a job is running — polling must never be blocked by inference (inference runs off the event loop).
- **NFR-5**: `transcript.json` is a single parseable JSON object; a reader never observes a partial file (atomic replace).
- **NFR-6**: Idle RSS before model load < **300 MB**.
- **NFR-7**: 100% of terminal jobs (succeeded, failed, cancelled) have exactly one sqlite row; a service kill mid-job leaves at most one row in a non-terminal state, which is reconciled to `failed`/`interrupted` on next startup.
- **NFR-8**: `cost_usd` is non-NULL and > 0 for a cloud-provider job that a provider prices, and NULL (not 0.0, which would be a lie about pricing) for the local provider; the distinction is documented.

## Acceptance criteria

- **FR-1**:
  - [ ] `uv run --directory services/transcription transcription-service --help` works from a clean checkout with no global Python.
  - [ ] `uv sync` resolves without any dependency on F1/F3/F4 code.
- **FR-2**:
  - [ ] `POST /v1/jobs` with a valid path returns `202` and a `job_id` within 200 ms (before transcription finishes).
  - [ ] Polling `GET /v1/jobs/{id}` shows `queued` → `running` with monotonically non-decreasing `progress` → `succeeded`.
  - [ ] Two submissions in a row: the second stays `queued` until the first is terminal.
  - [ ] `GET /health` returns 200 with `model_state: "unloaded"` before any job is submitted.
- **FR-3**:
  - [ ] Pointing `model_path` at a directory containing faster-whisper large-v3 loads the model with no network access (verified offline).
  - [ ] `device: auto` selects `cuda` on the reference machine and `cpu` when CUDA is forced unavailable; `/health` reports which.
  - [ ] Two consecutive jobs: the second job's log contains no model-load event.
- **FR-4**:
  - [ ] Switching the configured default provider from local to a cloud provider and restarting changes nothing for the caller: the same `POST /v1/jobs` request yields the same response shape and the same `transcript.json` schema.
  - [ ] A per-request `provider` override wins over config.
  - [ ] Grepping the codebase shows no provider name (`litellm`, `faster_whisper`, `openai`, `groq`) outside `providers/` and config.
- **FR-5**:
  - [ ] After a successful local job, one sqlite row exists with `elapsed_sec > 0`, `realtime_factor > 0`, `cost_usd IS NULL`, `status='succeeded'`.
  - [ ] After a forced provider-auth failure, one row exists with `status='failed'`, `error_kind='provider_auth'` and a non-null `elapsed_sec`.
  - [ ] After a cloud job, `cost_usd` is populated and matches the provider's published price for the audio duration within rounding.
  - [ ] Deleting the DB file and restarting recreates the schema; an existing DB is opened without data loss.
- **FR-6**:
  - [ ] After a job with `output_dir = <vault>/ELS/260812 - Security issue/`, that folder contains `transcript.json` parsing to the documented schema with `schema_version == 1`.
  - [ ] `segments[0]` has `start`, `end`, `text`, `avg_logprob`, `no_speech_prob`, `compression_ratio`.
  - [ ] No `*.tmp`/partial file remains in the folder after success or failure.
- **FR-7**:
  - [ ] An `.mp4` with AAC audio transcribes successfully on a machine where `ffmpeg` is **not** on PATH (this machine).
  - [ ] `.wav`, `.mp3`, `.m4a` each transcribe successfully.
  - [ ] A renamed text file with a `.mp4` extension fails with `error_kind='audio_decode'`, not a 500 traceback.
- **FR-8**:
  - [ ] Each taxonomy value is produced by at least one test, with the provider's raw status preserved in the record.
  - [ ] No response body or log line contains an API key.
- **FR-9**:
  - [ ] `audio_path` of `..\..\Windows\System32\config\SAM` is rejected `400 invalid_request` and produces no sqlite job row beyond a rejected-request log.
  - [ ] Connecting to the service on the machine's LAN IP is refused; on `127.0.0.1` it is accepted.
  - [ ] With a token configured, a request without it gets `401`.
- **FR-10**:
  - [ ] `transcribe <sample.wav> --out <tmp>` exits `0`, writes `transcript.json`, prints one JSON object on stdout and nothing else on stdout.
  - [ ] A missing input file exits with a distinct documented nonzero code and a stderr message.
- **FR-11**:
  - [ ] Cancelling a running job returns the job to `cancelled` within 5 s; sqlite records it; `transcript.json` is absent.
- **FR-12**:
  - [ ] A 30 s pure-silence input yields empty `text` and zero (or only filtered) segments rather than invented text.
  - [ ] With the filter disabled, the same input yields the raw model output — proving the toggle is live.
- **FR-13**:
  - [ ] `NOTICE` exists and names Vexa; every adapted file names its origin path.
- **FR-14**:
  - [ ] Launching with `--port 0` prints exactly one JSON ready line on stdout whose `port` is the port a subsequent request succeeds on.
- **FR-15**:
  - [ ] `uv run pytest -q` passes on a machine with no model downloaded, no GPU used and network disabled, in < 30 s.
  - [ ] `uv run pytest -m gpu` transcribes the bundled short sample end-to-end on the reference machine.
- **FR-16**:
  - [ ] `TRANSCRIBER_MODEL_PATH` overrides the config file value, and a CLI flag overrides both; `/health` reflects the winner.

## Out of scope

- **Summary generation** of any kind (`summary.md` stays F1's placeholder filename) — operator decision.
- Speaker diarization / speaker labels, topic extraction, action items — post-MVP per intake.
- Live / streaming transcription, meeting bots, screen or audio capture — this service transcribes files that already exist.
- Vault path construction, filename parsing, `unsorted` handling — **F1** owns it; this service only receives an `output_dir`.
- Any UI, drag-and-drop, progress rendering — **F3**.
- Installer, model download, app-folder layout — **F4** (this service only reads a configured model path).
- Splitting/chunking audio to fit a cloud provider's upload limit (e.g. OpenAI's 25 MB); an oversize cloud job fails with a clear `invalid_request` naming the limit.
- Non-Windows platforms; multi-user, remote or authenticated-over-network access; TLS.
- Job resume after a crash, retry policies beyond a single bounded retry on transient provider errors, and job history pruning/retention.
- LLM completions of any kind — litellm is used here only for its transcription/audio surface and as groundwork.

## Applicable toolkits

- `testing-toolkit:python-testing-patterns` — Tests layer; the `cli` profile's `pytest` row. Signal: this feature introduces the repo's first pytest suite (FR-15), and the reference implementation's suite (`vexa\core\meetings\services\transcription\tests\`) is pytest.

Rows deliberately **not** carried over:
- `devops-toolkit:docker-patterns` (`cli` Containers row) — no Docker anywhere; the service is a Windows sidecar process, and vexa's compose/nginx units are explicitly rejected above.
- `devops-toolkit:devops-rollout-plan` (`cli` Release row) — packaging and distribution are **F4**'s feature, not this one.
- All `frontend-toolkit` and `django-toolkit` rows — the `web` profile did not match; attaching a UI skill to a task in this feature means the stack was misread.

**Mandatory skills**: none. The `cli` profile declares none, and the `web` profile (whose `frontend-toolkit:internal-ui` is mandatory) did not match. The `workflow-toolkit` discipline skills every implementer invokes are sufficient.

Additionally binding on every task here, from the `cli` profile's inline rules: no `shell=True`/`os.system` (the PyAV decode path means we never shell out at all), path-traversal validation on every user-supplied path, secrets never in argv, temp files via `mkstemp` semantics, machine output on stdout and diagnostics on stderr, distinct nonzero exit codes.

## Open questions

*All resolved by the operator at the spec gate (2026-08-21): Q1 → A, Q2 → A, Q3 → A, Q4 → A. Retained below for the record.*

1. **How does litellm sit in the provider layer, given that local whisper is not a litellm provider?**
   - **A. Internal `TranscriptionProvider` protocol; litellm used only for cloud providers** *(recommended)* — local faster-whisper is one adapter, litellm is the other and covers every cloud provider at once, including its cost hooks. Simplest, no extra hop, honest about what litellm is good at. Downside: two code paths to keep behaviourally aligned.
   - **B. Everything through litellm; expose local whisper as an OpenAI-compatible `/v1/audio/transcriptions` route and point litellm at it** — one uniform call path, and the local backend becomes swappable by anyone (vexa's exact contract). Downside: HTTP round-trip to ourselves, more moving parts, and litellm's cost for the local model is meaningless.
   - **C. Register local whisper as a litellm custom provider** — single registry, single call site. Downside: litellm's custom-provider surface is completion-shaped; the transcription hook is thin and undertested, so this is the highest-risk option for an MVP.

2. **Who writes `transcript.json` into the vault folder?**
   - **A. The service writes it, given `output_dir` per request** *(recommended)* — one writer, no megabyte payloads crossing the process boundary, atomic-write logic lives in one place. Downside: the service touches the vault, so it needs the path allowlist (FR-9).
   - **B. The service only returns the transcript; F3/Tauri writes the file** — the vault stays exclusively F1/F3 territory. Downside: duplicate serialization rules, and the Rust side must re-implement atomic writes.
   - **C. Both — write the file *and* return the document** — convenient for the UI, at the cost of two sources of truth.

3. **How is the service process started and supervised on Windows?**
   - **A. Tauri sidecar: the app spawns it on launch, reads the stdout ready line, kills it on exit** *(recommended)* — nothing to install or leave running, port/token handshake is trivial, matches "app folder holds everything the user needn't know about". Downside: no transcription without the app open.
   - **B. A Windows service installed by F4, autostarting** — survives app restarts, could transcribe unattended later. Downside: real install/uninstall/upgrade burden in F4 for an MVP.
   - **C. Spawn a process per job** — simplest lifecycle. Downside: reloads large-v3 (tens of seconds) on every single file; contradicts NFR-2.

4. **What is the job API shape the UI codes against?**
   - **A. Async job + polling** *(recommended)* — `POST /v1/jobs` → poll status; supports a progress bar and cancel, no HTTP timeout risk on a 60-minute file. Downside: F3 writes a poll loop.
   - **B. Synchronous request with SSE progress streaming** — push-based progress, one connection. Downside: long-lived connection semantics and reconnect handling in Rust; cancel is fiddlier.
   - **C. Plain synchronous request returning the transcript** — simplest possible client. Downside: no progress at all, minutes-long request, and any timeout loses the work.

## Decisions log

- 2026-08-21 — Scope limited to the `## MVP` section; `# Планы` items deferred → per operator notes at intake.
- 2026-08-21 — `summary.md` is a reserved filename/placeholder only; **no summary generation in this MVP** — transcription only → operator.
- 2026-08-21 — litellm stays in scope as the unified provider layer (its transcription/audio surface) and as groundwork for later LLM steps → operator.
- 2026-08-21 — MVP targets **Windows only** → operator.
- 2026-08-21 — Python tooling is **`uv`**, never bare `python`/`pip` → operator (global preference).
- 2026-08-21 — The whisper large-v3 model is downloaded into the app folder by F4's installer; this service must load it from a **configurable path** → batch boundary.
- 2026-08-21 — Vault layout and naming (`root/<PROJECT>/<date> - <Title>/{source.<ext>, transcript.json, summary.md}`) is owned by **F1**; this service receives a target folder → batch boundary.
- 2026-08-21 — Q1 litellm boundary → **A: internal `TranscriptionProvider` protocol; local faster-whisper adapter + litellm adapter for all cloud providers (incl. cost hooks)**. (Operator, spec gate.)
- 2026-08-21 — Q2 transcript writer → **A: the service writes `transcript.json` into the caller-supplied `output_dir`**, with the FR-9 path allowlist. (Operator, spec gate.)
- 2026-08-21 — GPU-first (operator, F4 spec gate): the primary inference path is **CUDA on the RTX 4070 with full `large-v3`** (`float16`); CPU is an optional untuned fallback via `device=auto`. The shipped uv environment includes the CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) so CTranslate2 finds them without a system CUDA install.
- 2026-08-21 — Q4 job API shape → **A: async job + polling** (`POST /v1/jobs` → poll `GET /v1/jobs/{id}` every 1–2 s). (Operator, spec gate.)
- 2026-08-21 — Q3 process lifecycle → **A: Tauri sidecar** — F3 spawns the service on app launch, reads the FR-14 ready line for port/token, kills it on exit. Propagates to F3 (process model) and F4 (bundling). (Operator, spec gate.)
- 2026-08-21 — Vexa reuse: **adapt a small identified core** (model-load pattern, segment schema, hallucination heuristics, error taxonomy, GPU-free test pattern) under Apache-2.0 attribution; **write the service from scratch** otherwise; reject its streaming/backpressure and Docker/GPU-server deployment layers → analyst verdict, evidence in Problem & context.
