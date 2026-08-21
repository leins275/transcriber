---
slug: transcription-service
status: approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Python transcription microservice

## Architecture overview

One self-contained uv package at `services/transcription/`. It imports nothing from F1/F3/F4 and
must build, lint, type-check and test standalone (the feature worktree contains only `specs/`).

```
services/transcription/
  pyproject.toml  uv.lock  NOTICE  README.md  .gitignore  .python-version
  src/transcription/
    __init__.py          __version__, nothing importable-heavy at import time (NFR-1)
    errors.py       (A)  ErrorKind + ServiceError + HTTP-status classifier
    filters.py      (A)  silence / hallucination heuristics
    config.py            layered config: defaults < config.json < TRANSCRIBER_* env < CLI flags
    ledger.py            sqlite (WAL, PRAGMA user_version) job ledger + startup reconcile
    paths.py             allowlist / traversal / UNC / symlink validation
    schema.py            pydantic models: requests, JobStatus, Health, TranscriptDoc, Segment
    transcript.py        build TranscriptDoc + atomic write into output_dir
    logging_setup.py     JSON-lines logs to stderr; ready-line emitter to stdout
    providers/
      __init__.py        registry: name -> "module:Class" strings, imported lazily
      base.py            TranscriptionProvider Protocol, TranscriptResult, CancelToken
      local_whisper.py (A) faster-whisper adapter, lazy model load, segment mapping
      litellm_cloud.py   litellm adapter for every cloud provider + cost extraction
    jobs.py              JobManager: FIFO queue, ONE serial worker thread, progress, cancel
    app.py               FastAPI app + routes + bearer auth dependency
    server.py            uvicorn on 127.0.0.1, --port 0, ready line, signal handling
    cli.py               argparse: `serve` | `transcribe`; distinct exit codes
  tests/                 pytest; default run is model-free / GPU-free / network-free
```

`(A)` = contains code adapted from vexa; carries the Apache-2.0 header + origin path (FR-13).

**Data flow (one job)**

```
POST /v1/jobs {audio_path, output_dir, language?, provider?, model?, meeting?}
  -> app.py: bearer auth -> schema validation -> paths.resolve_under_roots(audio_path, output_dir)
  -> ledger.insert(job, status="queued")           # row exists from the first instant (NFR-7)
  -> jobs.JobManager.submit() -> asyncio.Queue     -> 202 {job_id}    (< 200 ms)

worker task (single, serial):
  ledger.mark_running -> providers.get_provider(name, cfg)
  -> await loop.run_in_executor(single_worker_pool, provider.transcribe, ...)   # NFR-4
       local: lazy WhisperModel load (once per process) -> iterate segments
              -> on_progress(seg.end / info.duration) -> filters.apply()
       cloud: litellm.transcription(...) -> cost from _hidden_params["response_cost"]
  -> transcript.write_atomic(doc, output_dir)      # mkstemp in target dir + os.replace (NFR-5)
  -> ledger.finish(status, elapsed, rtf, cost, segment_count, error_kind, error_message)

GET /v1/jobs/{id}        <- in-memory job state (fast) merged with ledger row
GET /v1/jobs/{id}/result <- json.load(ledger.output_path)
DELETE /v1/jobs/{id}     <- CancelToken.set(); worker aborts between segments; no file written
```

**Provider seam (FR-4).** `providers/base.py` declares the only interface anyone else uses:

```python
class TranscriptionProvider(Protocol):
    name: str
    def describe(self) -> ProviderInfo: ...           # name, model, device, compute_type, state
    def transcribe(self, audio_path: Path, *, language: str | None,
                   on_progress: Callable[[float], None],
                   cancel: CancelToken) -> TranscriptResult: ...
```

No module outside `providers/` and `config.py` may contain the strings `litellm`,
`faster_whisper`, `openai` or `groq` — enforced by a grep test in T16 (FR-4 acceptance).

**Error taxonomy (FR-8)**, adapted from `vexa/core/meetings/modules/whisper/src/transcription-client.ts:57-88`:

| kind | source | retryable | HTTP status of `POST /v1/jobs` | CLI exit |
|---|---|---|---|---|
| `invalid_request` | validation, path allowlist, oversize cloud upload | no | 400 | 2 |
| `unsupported_input` | extension/container we refuse up front | no | 400 | 2 |
| `audio_decode` | PyAV/CTranslate2 decode failure | no | job body | 3 |
| `model_load` | WhisperModel construction failed | no | job body | 4 |
| `provider_auth` | provider 401/403 | no | job body | 5 |
| `provider_payment_required` | provider 402 | no | job body | 6 |
| `provider_rate_limited` | provider 429 | yes (one bounded retry) | job body | 6 |
| `provider_unavailable` | provider 5xx / network | yes (one bounded retry) | job body | 6 |
| `timeout` | request aborted, no response in time | yes | job body | 7 |
| `cancelled` | DELETE | no | job body | 8 |
| `internal` | anything unclassified | no | 500 | 1 |

Missing/wrong bearer token is `401` and never reaches the taxonomy. The provider's raw HTTP
status and a sanitized detail string are preserved on the record; API keys are scrubbed.

**SQLite schema v1** (`ledger.py`, `PRAGMA user_version = 1`, WAL):
`jobs(job_id TEXT PK, created_at, started_at, finished_at, status, provider, model, device,
source_path, output_path, audio_duration_sec, elapsed_sec, realtime_factor, cost_usd, currency,
language, segment_count, filtered_segment_count, error_kind, error_message, meeting_json,
service_version)`.

**`transcript.json` v1** exactly as FR-6 spells it; the `segments` element shape is vexa's
`verbose_json` mapper (`.../transcription/src/transcription/main.py:462-484`).

**Cross-feature contracts this plan must honour** (F3/F4 code them against):
- stdout ready line, exactly one line, then never stdout again from `serve`:
  `{"event":"listening","port":<int>,"token":"<str>","pid":<int>}` (FR-14).
- App folder is located by env var `TRANSCRIBER_APP_DIR`, falling back to the parent of the
  running executable's directory; config file defaults to `<app_dir>/config.json` (F4 FR-11).
  F4 owns that file's schema, so this service **reads the keys it knows and ignores the rest**;
  a `vault_root` key, if present, seeds `allowed_roots`.

## Risks

- **Vexa clone is absent from the feature worktree.** Every constant, threshold and code shape
  we adapt is written verbatim into the task blocks below; no task may need to read the clone.
- **CUDA wheels are heavy and Windows-only.** They go in an optional extra
  (`[project.optional-dependencies] cuda`, `sys_platform == "win32"` markers) so `uv sync` for
  tests stays light and F4 can bake `uv sync --extra cuda`. CTranslate2 4.8 needs **cuDNN 9**
  (`nvidia-cudnn-cu12>=9,<10`) — pinning cuDNN 8 silently breaks CUDA at runtime.
- **Test suite drifting into needing a model/GPU/network (FR-15).** Mitigated by the vexa
  conftest pattern: model load is lazy (never at import), `TestClient(app)` is used *without*
  the `with` block so lifespan does not run, and the provider registry is monkeypatched to a
  fake. Enforced by `addopts = -m "not gpu"` plus a T16 audit.
- **Polling starved by inference (NFR-4).** Inference runs in a single-worker
  `ThreadPoolExecutor`; the event loop only awaits. Asserted by a test that polls while a fake
  provider sleeps.
- **Double-writing the ledger / orphan rows (NFR-7).** One row is inserted at submission and
  only ever `UPDATE`d. Non-terminal rows are reconciled to `failed`/`interrupted` on startup.
- **Cancel leaving a partial `transcript.json` (FR-11).** The file is only ever produced by
  `transcript.write_atomic`, which is called once, after a successful, uncancelled result.
- **Serialized wave chain (waves 5-8 are one task each).** Inherent: app -> server -> cli -> e2e.
  Everything parallelizable was pushed into waves 2-4.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1 |
| 2 | T2, T3, T4, T5 |
| 3 | T6, T7, T8, T9 |
| 4 | T10, T11, T12 |
| 5 | T13 |
| 6 | T14 |
| 7 | T15 |
| 8 | T16 |

Two standing rules that keep parallel agents off each other's files:
1. `tests/conftest.py` is created once (T1) and **never edited again** — every later task puts
   its fixtures in its own test module (or imports `tests/fakes.py`, owned by T8).
2. `providers/__init__.py` is written once (T8) with the concrete providers already registered
   as `"module:Class"` **strings** resolved lazily, so T10 and T11 never touch it.

## Tasks

### [ ] T1: Package scaffold, tooling and NOTICE  [deps: —]

- **Files**: `services/transcription/pyproject.toml`, `services/transcription/uv.lock`, `services/transcription/.python-version`, `services/transcription/.gitignore`, `services/transcription/NOTICE`, `services/transcription/README.md`, `services/transcription/src/transcription/__init__.py`, `services/transcription/tests/conftest.py`, `services/transcription/tests/test_packaging.py`
- **Test first**: `services/transcription/tests/test_packaging.py` — cases: `import transcription` succeeds and exposes `__version__` matching `pyproject.toml`'s version (FR-1); `tomllib`-parsing `pyproject.toml` shows `[project.scripts] transcription-service = "transcription.cli:main"` (FR-1, do **not** import it — `cli.py` arrives in T15); `requires-python == ">=3.12"`; the `cuda` optional-dependency group lists `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`; the `gpu` marker is registered and excluded by default `addopts` (FR-15); `NOTICE` exists, is non-empty and contains both `Vexa` and `Apache License, Version 2.0` (FR-13).
- **Implement**: `[project] name="transcription", version="0.1.0", requires-python=">=3.12"`; dependencies `fastapi>=0.110,<1`, `uvicorn[standard]>=0.30`, `pydantic>=2.7`, `faster-whisper>=1.2,<2`, `litellm>=1.60`, `h11>=0.16`; `[dependency-groups] dev = ["pytest>=8", "pytest-asyncio>=0.23,<2", "httpx>=0.27,<1", "ruff>=0.6", "mypy>=1.11"]`; `[project.optional-dependencies] cuda = ["nvidia-cublas-cu12>=12.3; sys_platform=='win32'", "nvidia-cudnn-cu12>=9,<10; sys_platform=='win32'"]` (CTranslate2 4.8 requires cuDNN **9**). Src layout (`[tool.hatch.build.targets.wheel] packages = ["src/transcription"]` or equivalent). `[tool.pytest.ini_options] testpaths=["tests"] addopts='-q -m "not gpu"' markers=["gpu: needs the real large-v3 model and a CUDA device (opt-in)"] asyncio_mode="auto"`. `[tool.ruff] line-length=100` with `lint.select` at least `E,F,I,B,UP,S` (S = bandit: it is what forbids `shell=True`/`subprocess` misuse). `[tool.mypy] strict=true, files=["src"]`. Run `uv sync` (and `uv sync --extra cuda` once, to prove the CUDA wheels resolve on Windows) and commit `uv.lock`. `conftest.py` holds only universal fixtures: `tmp_app_dir` (a `tmp_path` app folder with `models/`, `data/`, `logs/`) and `anyio_backend`. `NOTICE` names Vexa (Vexa-ai/vexa, Apache-2.0) and lists the four origin paths adapted in T2/T3/T10/T13. `README.md` documents the four QA commands and states plainly that `make` is not installed on this machine and the root `Makefile` belongs to F4.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: `uv run pytest -q` passes with zero network access after `uv sync`; `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy` are green (mypy over an empty package is trivially green — that is fine, it proves the config loads); `uv sync --extra cuda` resolves.

### [ ] T2: Error taxonomy and provider-fault classifier  [deps: T1]

- **Files**: `services/transcription/src/transcription/errors.py`, `services/transcription/tests/test_errors.py`
- **Test first**: `services/transcription/tests/test_errors.py` — cases: every value of `ErrorKind` in the plan's taxonomy table exists and round-trips through `ErrorKind(value).value` (FR-8); `classify_http_status(402) -> provider_payment_required, retryable=False`, `401 -> provider_auth`, `403 -> provider_auth`, `429 -> provider_rate_limited, retryable=True`, `500/503 -> provider_unavailable, retryable=True`, `400/404/422 -> invalid_request, retryable=False`, `200 -> internal`; `ServiceError` carries `kind`, `status`, `detail`, `retryable` and its `str()` never contains a value passed as a secret — `redact("Bearer sk-abc123deadbeef and key=sk-live-xyz")` returns a string containing neither `sk-abc123deadbeef` nor `sk-live-xyz` (FR-8: "no response body or log line contains an API key"); `ServiceError.to_dict()` yields `{"error_kind", "error_message", "provider_status"}` (FR-8: never collapsed into a generic message).
- **Implement**: Apache-2.0 header + `# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 — origin: core/meetings/modules/whisper/src/transcription-client.ts:57-88, reinforced by docs/docs/how-to/custom-stt.mdx:114-124` (FR-13). `class ErrorKind(str, Enum)` with the 11 values from the taxonomy table; `class ServiceError(Exception)` with `kind: ErrorKind, message: str, status: int | None = None, retryable: bool = False, detail: str | None = None`; `classify_http_status(status, detail) -> ServiceError` reproducing vexa's ladder (402 -> payment_required; 401/403 -> unauthorized; 429 -> rate_limited retryable; >=500 -> unavailable retryable; >=400 -> bad_request; else unknown) renamed onto our kinds; `redact(text)` strips `sk-…`/`Bearer …`/`api[-_]?key=…` patterns.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `errors.py` imports nothing from this package and nothing heavy from stdlib; QA gate green.

### [ ] T3: Silence and hallucination filters  [deps: T1]

- **Files**: `services/transcription/src/transcription/filters.py`, `services/transcription/tests/test_filters.py`
- **Test first**: `services/transcription/tests/test_filters.py` — cases: `is_low_confidence({"no_speech_prob":0.7,"avg_logprob":-1.2})` is True and `{"no_speech_prob":0.5,"avg_logprob":-1.2}` is False (both thresholds required); `compression_ratio = 2.5` alone is True, `2.4` is False (strict `>`); `avg_logprob = -1.35` alone is True, `-1.25` alone (with benign other fields) is False; `looks_like_silence([])` is True; `looks_like_silence` is True only when **every** segment is (`no_speech_prob > 0.6` and `avg_logprob < -1.0`) and False if any segment is not; `apply_filters(segments, enabled=True)` on an all-silence list returns `([], n_filtered=len(segments))` and the caller's `text` therefore ends up `""` (FR-12); `apply_filters(segments, enabled=False)` returns the input list unchanged with `n_filtered=0` — the toggle is live (FR-12 acceptance); filtering is stable (surviving segments keep original order, `id`s are re-numbered contiguously from 0).
- **Implement**: Apache-2.0 header + `# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 — origin: core/meetings/services/transcription/src/transcription/main.py:99-118 and core/meetings/modules/whisper/src/confidence.ts:8-13` (FR-13). Module constants exactly as vexa tuned them: `NO_SPEECH_THRESHOLD = 0.6`, `LOG_PROB_THRESHOLD = -1.0`, `LOG_PROB_HARD_THRESHOLD = -1.3`, `COMPRESSION_RATIO_THRESHOLD = 2.4`. Pure functions over `Mapping[str, Any]`; no imports from the rest of the package.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; QA gate green.

### [ ] T4: Layered configuration  [deps: T1]

- **Files**: `services/transcription/src/transcription/config.py`, `services/transcription/tests/test_config.py`
- **Test first**: `services/transcription/tests/test_config.py` — cases: precedence defaults < config file < env < explicit override — write `{"model_path": "A"}` to `<app_dir>/config.json`, set `TRANSCRIBER_MODEL_PATH=B`, pass `overrides={"model_path": "C"}`, assert the winner is `C`, and with the override removed it is `B`, and with the env removed it is `A` (FR-16 acceptance); a missing config file is not an error (all defaults); an unknown key in the config file is ignored (F4 owns that schema) but a `vault_root` key lands in `allowed_roots`; `TRANSCRIBER_APP_DIR` selects the app dir and `db_path`/`model_path` default under it (`<app_dir>/data/jobs.sqlite3`, `<app_dir>/models`); `token` is auto-generated when unset, is >= 32 chars and differs between two loads; `TRANSCRIBER_ALLOWED_ROOTS` splits on `os.pathsep`; malformed JSON raises `ConfigError` naming the file path; `Config.public()` (what `/health` and logs may show) contains `provider`/`model`/`device` and contains **no** token and **no** API key (FR-9); an API key present in `overrides` from an argv-shaped source raises `ConfigError` — credentials come from env/config file only (FR-9).
- **Implement**: frozen `@dataclass Config` with the FR-16 keys: `app_dir, config_path, model, model_path, device ("auto"), compute_type (None -> float16 on cuda / int8 on cpu), provider ("local"), cloud_model, provider_api_key (env only: TRANSCRIBER_PROVIDER_API_KEY, else OPENAI_API_KEY/GROQ_API_KEY), db_path, allowed_roots, host (fixed "127.0.0.1"), port (0), token, language (None), filter_hallucinations (True), max_cloud_upload_mb (25), job_timeout_sec, log_level`. `load_config(*, config_path=None, env=os.environ, overrides=None) -> Config`; env var names are `TRANSCRIBER_<UPPER_KEY>`; booleans parse `1/true/yes` case-insensitively. App dir resolution: `TRANSCRIBER_APP_DIR` else `Path(sys.executable).parent.parent` else CWD.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; no module-level filesystem or network access at import; QA gate green.

### [ ] T5: SQLite job ledger  [deps: T1]

- **Files**: `services/transcription/src/transcription/ledger.py`, `services/transcription/tests/test_ledger.py`
- **Test first**: `services/transcription/tests/test_ledger.py` — cases: opening a fresh path creates the file, sets `PRAGMA journal_mode=wal` and `PRAGMA user_version=1`, and creates `jobs` with every FR-5 column; reopening an existing DB preserves rows and does not re-run DDL destructively (FR-5 acceptance: "delete the DB and restart recreates the schema; an existing DB is opened without data loss"); `insert_job` then `mark_running` then `finish_succeeded` leaves exactly **one** row with `status='succeeded'`, `elapsed_sec > 0`, `realtime_factor > 0`, `cost_usd IS NULL` (FR-5, NFR-8); `finish_failed(kind=ErrorKind.PROVIDER_AUTH)` leaves one row with `status='failed'`, `error_kind='provider_auth'`, non-null `elapsed_sec` (FR-5); a cloud success stores `cost_usd = 0.006` and `currency='USD'` and reads back as a float, and a local success stores SQL `NULL` — asserted with `IS NULL`, not `== 0.0` (NFR-8); `reconcile_interrupted()` flips rows left in `queued`/`running` to `status='failed'`, `error_kind='internal'`, `error_message` mentioning interruption, and returns the count (NFR-7); `list_jobs(limit=…, status=…)` returns newest-first and honours both filters (FR-2); concurrent open from two connections in WAL mode does not raise `database is locked` for a read during a write.
- **Implement**: stdlib `sqlite3` only, `check_same_thread=False` with a `threading.Lock` around writes (the worker thread and the event loop both write). Timestamps stored as ISO-8601 UTC strings. `error_kind` stored as the plain string value (no import of `errors.py` needed — keep the ledger dependency-free). Schema DDL is `CREATE TABLE IF NOT EXISTS` + `user_version` check that refuses to open a DB with a *higher* version.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; a temp-file DB is used in every test (never a fixed path); QA gate green.

### [ ] T6: Path allowlist and traversal defence  [deps: T2]

- **Files**: `services/transcription/src/transcription/paths.py`, `services/transcription/tests/test_paths.py`
- **Test first**: `services/transcription/tests/test_paths.py` — cases: a file directly under an allowed root resolves and returns the resolved absolute `Path`; `..\..\Windows\System32\config\SAM` (and its POSIX form) raises `ServiceError(invalid_request)` (FR-9 acceptance); a UNC path `\\\\server\\share\\x.mp4` and a device path `\\\\?\\C:\\x.mp4` are rejected; a path whose *resolved* target escapes the root via a symlink/junction is rejected while a symlink that stays inside is accepted (create both with `tmp_path`; skip the symlink creation with `pytest.skip` if the OS refuses without privilege, but still run the junction-free escape case); an empty `allowed_roots` list rejects everything (fail closed); `must_exist=True` on a missing file raises `invalid_request` with the filename in the message; `ensure_output_dir` creates a missing directory **inside** a root but refuses to create one outside; case-insensitive comparison on Windows (`C:\Vault\x` vs `c:\vault\x` both accepted); the rejection message never echoes the full attacker-supplied path beyond its basename.
- **Implement**: `resolve_under_roots(candidate: str | Path, roots: Sequence[Path], *, must_exist: bool) -> Path` — reject empty input; reject `\\\\` prefixes and `:` stream separators before touching the filesystem; `Path(...).resolve(strict=must_exist)`; compare with `os.path.commonpath` on `os.path.normcase`'d strings (`Path.is_relative_to` alone misses case differences on Windows). Raise `ServiceError(ErrorKind.INVALID_REQUEST, …)` from T2. No `shell=True`, no subprocess anywhere in this module.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; QA gate green.

### [ ] T7: Transcript schema and atomic writer  [deps: T2]

- **Files**: `services/transcription/src/transcription/schema.py`, `services/transcription/src/transcription/transcript.py`, `services/transcription/tests/test_transcript.py`
- **Test first**: `services/transcription/tests/test_transcript.py` — cases: a built `TranscriptDoc` serializes to exactly the FR-6 v1 shape with `schema_version == 1` and top-level keys `created_at, source, provider, language, language_probability, text, segments, stats`; `segments[0]` has `id, start, end, text, avg_logprob, no_speech_prob, compression_ratio` and `words` is absent (not `null`) when word timestamps were not requested (FR-6 acceptance); `stats` has `elapsed_sec, realtime_factor, cost_usd, currency` and `cost_usd` serializes as JSON `null` for local (NFR-8); `write_atomic(doc, output_dir)` produces `output_dir/transcript.json` that `json.loads` round-trips, and leaves **no** `*.tmp` / partial file in the directory (FR-6 acceptance, NFR-5); writing twice overwrites cleanly; if `json.dump` raises mid-write (monkeypatch it) the temp file is removed and `transcript.json` is either absent or still the previous good content — a reader never sees a partial file (NFR-5); the API models `JobCreate` (rejects an empty `audio_path`), `JobStatus` (status literal set `queued|running|succeeded|failed|cancelled`, `progress` clamped to 0..1) and `Health` (`model_state` literal `unloaded|loading|loaded`) validate per FR-2.
- **Implement**: pydantic v2 models in `schema.py`; `transcript.py` has `build_document(...)` (pure) and `write_atomic(doc, output_dir) -> Path` using `tempfile.mkstemp(dir=output_dir, prefix=".transcript-", suffix=".tmp")` + `os.write` + `os.fsync` + `os.replace` inside `try/finally` that unlinks the temp on any exception (profile rule: mkstemp semantics, never a predictable temp name). `error_kind` on `JobStatus` is typed as `ErrorKind | None` from T2.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `write_atomic` never writes outside `output_dir`; QA gate green.

### [ ] T8: Provider protocol, registry and test fakes  [deps: T2]

- **Files**: `services/transcription/src/transcription/providers/__init__.py`, `services/transcription/src/transcription/providers/base.py`, `services/transcription/tests/fakes.py`, `services/transcription/tests/test_provider_registry.py`
- **Test first**: `services/transcription/tests/test_provider_registry.py` — cases: `get_provider("local", cfg)` and `get_provider("cloud", cfg)` raise `ModuleNotFoundError`-free *lazily* — i.e. `import transcription.providers` alone imports neither `faster_whisper` nor `litellm` (assert `"faster_whisper" not in sys.modules` after the import) (NFR-1, NFR-6); `get_provider("nope", cfg)` raises `ServiceError(invalid_request)` naming the known provider names; `register("fake", FakeProvider)` then `get_provider("fake", cfg)` returns the fake (this is the hook every later test uses); `TranscriptResult` is constructible with `segments, text, language, language_probability, duration_sec, model, device, compute_type, cost_usd=None, currency=None, filtered_segment_count=0` and rejects a negative `duration_sec`; `CancelToken.set()` makes `cancelled` True and `raise_if_cancelled()` raise `ServiceError(cancelled)`; `FakeProvider` reports progress `0.25/0.5/0.75/1.0`, honours the cancel token between chunks, and can be configured to raise any `ErrorKind`.
- **Implement**: `base.py` — `Protocol` per the architecture overview, `@dataclass TranscriptResult`, `@dataclass ProviderInfo`, `CancelToken` (a thin `threading.Event` wrapper). `__init__.py` — `_REGISTRY: dict[str, str] = {"local": "transcription.providers.local_whisper:LocalWhisperProvider", "cloud": "transcription.providers.litellm_cloud:LiteLLMProvider"}` resolved with `importlib.import_module` **inside** `get_provider`, plus `register(name, cls)` for tests. Do not import the concrete modules at module scope — T10/T11 land later and this file must not be edited again. `tests/fakes.py` holds `FakeProvider` and `FakeSegment` used by T12/T13/T15.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `providers/__init__.py` contains no provider-library import; QA gate green.

### [ ] T9: Structured logging and the ready-line emitter  [deps: T1]

- **Files**: `services/transcription/src/transcription/logging_setup.py`, `services/transcription/tests/test_logging.py`
- **Test first**: `services/transcription/tests/test_logging.py` — cases: `configure_logging(level)` attaches exactly one handler writing to **stderr** and emits one JSON object per line with `ts, level, event, msg` plus any extra fields, parseable by `json.loads` (FR-14); nothing is ever written to stdout by the logger (capture both streams and assert stdout is empty); an exception logged with `exc_info=True` puts the traceback in a `"traceback"` string field and still emits a single line (no raw multi-line output that breaks JSON-lines); a log record whose extras contain `token`/`api_key`/`authorization` has those values replaced with `"***"` (FR-8: no log line contains an API key); `emit_ready_line(port=51234, token="t", pid=42, stream=buf)` writes exactly one line, `json.loads` gives `{"event":"listening","port":51234,"token":"t","pid":42}`, the stream is flushed, and calling it twice raises (exactly one ready line is the contract F3 codes against); repeated `configure_logging` calls do not duplicate handlers.
- **Implement**: stdlib `logging` with a custom `Formatter` producing JSON lines; module-level `_ready_emitted` guard. `emit_ready_line` takes the stream as a parameter (default `sys.stdout`) so it is testable without capsys games.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; QA gate green.

### [ ] T10: Local faster-whisper provider  [deps: T3, T4, T8]

- **Files**: `services/transcription/src/transcription/providers/local_whisper.py`, `services/transcription/tests/test_provider_local.py`
- **Test first**: `services/transcription/tests/test_provider_local.py` (monkeypatch a `FakeWhisperModel` over the module's `WhisperModel` symbol — never load a real model, FR-15) — cases: the model is **not** constructed at import nor at provider construction, only on the first `transcribe`, and a second `transcribe` on the same instance constructs it **zero** additional times (FR-3 acceptance: "the second job's log contains no model-load event"); `describe().model_state` walks `unloaded -> loading -> loaded`; device resolution — with the CUDA probe monkeypatched to report 1 device, `device="auto"` resolves to `cuda` + `compute_type="float16"`; with 0 devices it resolves to `cpu` + `int8`; an explicit `device="cpu"` wins over the probe (FR-3); the constructor kwargs passed to `WhisperModel` are exactly `model_size_or_path`, `device`, `compute_type`, `download_root` (the **configured** model dir, never a hardcoded `/app/models`) plus `cpu_threads` only when device is cpu; a `WhisperModel` constructor raising maps to `ServiceError(model_load)` with the original message preserved; a `transcribe` call raising a decode/format error (simulate with `RuntimeError("Invalid data found when processing input")`) maps to `ServiceError(audio_decode)` whose message names the file and the underlying decoder error (FR-7 acceptance); segment mapping produces `{id, start, end, text, avg_logprob, compression_ratio, no_speech_prob}` with `id` renumbered from 0 and `words` only when word timestamps were requested (FR-6); `on_progress` is called with non-decreasing values ending at 1.0 computed as `segment.end / info.duration` (FR-2); an all-silence fake result yields `text == ""` with the filter on and the raw invented text with `filter_hallucinations=False` (FR-12 both acceptance bullets); `cancel.set()` after the second segment stops iteration and raises `ServiceError(cancelled)` without exhausting the generator (FR-11); `cost_usd` is `None` and `currency` is `None` (NFR-8).
- **Implement**: Apache-2.0 header + `# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 — origin: core/meetings/services/transcription/src/transcription/main.py:227-245 (model load) and :462-484 (segment mapping)` (FR-13). Model load kwargs verbatim from vexa except `download_root` is `config.model_path`. Segment dict per vexa's mapper, trimmed to the FR-6 fields (`seek`/`tokens`/`temperature`/`audio_start`/`audio_end` are dropped — our schema does not carry them). Call `model.transcribe(str(path), language=…, beam_size=5, vad_filter=True, word_timestamps=…)` and iterate the returned generator so progress and cancel are cooperative. `import faster_whisper` at module scope is fine (the registry imports this module lazily), but nothing may execute at import.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green with no model on disk, no GPU and no network; QA gate green.

### [ ] T11: litellm cloud provider  [deps: T4, T8]

- **Files**: `services/transcription/src/transcription/providers/litellm_cloud.py`, `services/transcription/tests/test_provider_cloud.py`
- **Test first**: `services/transcription/tests/test_provider_cloud.py` (monkeypatch the module's `litellm` symbol with a fake — no network, FR-15) — cases: a successful fake `verbose_json` response maps to a `TranscriptResult` whose `segments` have the **same** field names as the local provider's, so `transcript.json` is schema-identical across providers (FR-4 acceptance); `cost_usd` is taken from the response's `_hidden_params["response_cost"]` when present, else from `litellm.completion_cost(response)`, and is `> 0` with `currency == "USD"` (NFR-8, FR-5); when the provider prices nothing and both hooks fail, `cost_usd` is `None` (never `0.0`, which would lie about pricing) and a warning is logged; provider faults map through `classify_http_status` — a fake raising an exception carrying `status_code=401` becomes `provider_auth`, `402 -> provider_payment_required`, `429 -> provider_rate_limited` with `retryable=True`, `503 -> provider_unavailable`, a timeout exception -> `timeout` — each preserving the raw status on the record (FR-8 acceptance: every taxonomy value produced by at least one test); exactly **one** bounded retry happens for a retryable fault and **zero** for a non-retryable one; an input file larger than `max_cloud_upload_mb` fails fast with `invalid_request` whose message names the limit and the actual size, before any call is made (Out-of-scope: no chunking); the API key is read from `config.provider_api_key` and passed to litellm as a keyword — it appears in **no** log line and in **no** `ServiceError` message (FR-9 acceptance); `on_progress(0.0)` then `on_progress(1.0)` are the only progress calls; `cancel.set()` before the call raises `ServiceError(cancelled)` without calling litellm.
- **Implement**: `import litellm` at module scope with `litellm.suppress_debug_info = True`; call `litellm.transcription(model=config.cloud_model, file=<opened binary file>, language=…, response_format="verbose_json", api_key=…)`. Normalise the response's segments into our shape, defaulting missing `avg_logprob`/`no_speech_prob`/`compression_ratio` to `None`. This module and `local_whisper.py` are the only places a provider library name may appear (FR-4).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green with network disabled; QA gate green.

### [ ] T12: Serial job manager (queue, progress, cancel, ledger, transcript)  [deps: T5, T6, T7, T8]

- **Files**: `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_jobs.py`
- **Test first**: `services/transcription/tests/test_jobs.py` (async tests driving `JobManager` directly with `FakeProvider` from `tests/fakes.py`, no HTTP) — cases: `submit()` returns a `job_id` immediately and inserts one `queued` ledger row before returning (FR-2, NFR-7); two submissions run **serially** — the second stays `queued` until the first is terminal, and the fake's concurrent-entry counter never exceeds 1 (FR-2 acceptance); `status()` progress is monotonically non-decreasing across `queued -> running -> succeeded` and ends at 1.0 (FR-2); `status()` returns in well under 50 ms while the fake provider blocks its worker thread for 1 s — proving inference is off the event loop (NFR-4); on success `transcript.json` exists in `output_dir`, the ledger row is `succeeded` with `elapsed_sec > 0`, `realtime_factor = elapsed/duration > 0`, `segment_count` matching, `cost_usd IS NULL` (FR-5, FR-6); a provider raising `ServiceError(provider_auth)` produces one `failed` row with `error_kind='provider_auth'`, non-null `elapsed_sec`, **no** `transcript.json` and no leftover `*.tmp` (FR-5, FR-8); `cancel(job_id)` on a *queued* job ends it `cancelled` without ever calling the provider; `cancel(job_id)` on a *running* job ends it `cancelled` within 5 s, writes the ledger row with its elapsed time, and leaves no `transcript.json` (FR-11 acceptance); `cancel()` on an unknown id raises a not-found error; a job whose `audio_path` fails the allowlist is rejected by the *caller* path — `submit()` with an out-of-root path raises `ServiceError(invalid_request)` and creates **no** ledger row (FR-9 acceptance); exactly one ledger row exists per job in every terminal case (NFR-7); `provider` from the request overrides the configured default and the override is what lands in the ledger row and in `transcript.json` (FR-4 acceptance).
- **Implement**: `JobManager(config, ledger)` owning an `asyncio.Queue`, a `dict[str, JobState]` and a **single-worker** `ThreadPoolExecutor(max_workers=1)`; one long-lived worker coroutine pops, marks running, `await loop.run_in_executor(...)` on `provider.transcribe`, then writes the transcript and finishes the row. Path validation and `ensure_output_dir` happen in `submit()`, before the ledger insert. `start()`/`aclose()` for lifecycle. Timing from `time.monotonic`; wall-clock timestamps from `datetime.now(UTC)`.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green in under a few seconds (use short fake sleeps); no test needs a model, GPU or network; QA gate green.

### [ ] T13: HTTP API  [deps: T9, T12]

- **Files**: `services/transcription/src/transcription/app.py`, `services/transcription/tests/test_api_contract.py`, `services/transcription/tests/test_api_jobs.py`
- **Test first**: `services/transcription/tests/test_api_contract.py` (auth, health, validation — adapts vexa's GPU-free pattern: build the app with a config pointing at `tmp_app_dir`, use `TestClient(app)` **without** the `with` block so lifespan never runs, and register `FakeProvider` in the registry) and `services/transcription/tests/test_api_jobs.py` (job lifecycle, with lifespan so the worker runs) — cases: `GET /health` returns 200 with `{status, version, provider, model, device, model_state}` and `model_state == "unloaded"` before any job (FR-2 acceptance) and the body contains no token and no API key (FR-9); with a token configured, every `/v1/*` route without `Authorization: Bearer <token>` returns **401**, with a wrong token 401, with the right token 200/202 — and `/health` stays reachable without a token so a supervisor can probe liveness (FR-9 acceptance); `POST /v1/jobs` with a valid path returns **202** with `{job_id}` in under 200 ms while the fake provider still runs (FR-2 acceptance); `POST /v1/jobs` with `audio_path = "..\\..\\Windows\\System32\\config\\SAM"` returns **400** with `error_kind='invalid_request'` and creates **no** ledger job row (FR-9 acceptance); a malformed body returns 400/422 with `error_kind='invalid_request'`, never a 500 traceback; `GET /v1/jobs/{id}` polls `queued -> running -> succeeded` with non-decreasing `progress` and reports `elapsed_sec, audio_duration_sec, provider, cost_usd` (FR-2); `GET /v1/jobs/{unknown}` is 404; `GET /v1/jobs/{id}/result` returns the same JSON document as the written `transcript.json` byte-for-byte after `json.loads` (FR-2, FR-6) and 404 while the job is not `succeeded`; `GET /v1/jobs?limit=2&status=succeeded` returns newest-first, honours both filters and reads from the ledger (FR-2); `DELETE /v1/jobs/{id}` returns 200 and the job becomes `cancelled` (FR-11); a failed job returns HTTP 200 with `status='failed'` and a specific `error_kind` — never a generic message (FR-8); startup calls `ledger.reconcile_interrupted()` and a pre-seeded `running` row is `failed` after startup (NFR-7); the app object is created by a factory (`create_app(config)`) so no import-time side effects exist (NFR-1).
- **Implement**: Apache-2.0 header + `# Test pattern adapted from Vexa (Vexa-ai/vexa), Apache-2.0 — origin: core/meetings/services/transcription/tests/conftest.py` on `test_api_contract.py` (FR-13). `create_app(config) -> FastAPI` with an `asynccontextmanager` lifespan that opens the ledger, reconciles, and starts/stops the `JobManager`. Bearer auth as a `Depends` on the `/v1` router only. A single exception handler maps `ServiceError` to `{error_kind, error_message, provider_status}` with the status from the taxonomy table, and a catch-all maps anything else to 500 `internal` with the traceback logged (never returned).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green with no model, no GPU, no network; `uv run pytest -q` still finishes in under 30 s (FR-15); QA gate green.

### [ ] T14: Server entrypoint, loopback binding and the ready line  [deps: T13]

- **Files**: `services/transcription/src/transcription/server.py`, `services/transcription/tests/test_server.py`
- **Test first**: `services/transcription/tests/test_server.py` — cases: `run_server(config)` binds a socket on `127.0.0.1` only — assert the constructed uvicorn config's `host == "127.0.0.1"` and that the bound socket's `getsockname()[0]` is `127.0.0.1`, so a LAN address is never listened on (FR-9 acceptance); with `port=0` the socket is created **before** the ready line is emitted and the ready line's `port` equals the actually-bound port (FR-14 acceptance); exactly one JSON line reaches stdout for a whole server lifetime, and it parses to `{"event":"listening","port","token","pid"}` with `pid == os.getpid()` (FR-14); all uvicorn/app logging goes to stderr — stdout after the ready line is empty (FR-14, profile rule); a fixed configured port is used verbatim; `/health` answers 200 within **3 s** of process start in an in-process spawn test (NFR-1); a SIGINT/`shutdown()` stops the server and closes the ledger without leaving a non-terminal row.
- **Implement**: create the listening socket ourselves with `socket.socket()` + `bind((host, port))` + `listen()`, read the real port from `getsockname()`, emit the ready line via T9's `emit_ready_line`, then hand the socket to `uvicorn.Server.run(sockets=[sock])`. Configure uvicorn with `log_config=None` and `access_log=False` so it cannot write to stdout. Prefer testing with the socket + `uvicorn.Config` objects over spawning a process where possible; one test may spawn `python -c` in-process via `uvicorn` in a thread for the NFR-1 timing check.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; a manual `uv run python -c "from transcription.server import ..."` smoke run prints exactly one ready line; QA gate green.

### [ ] T15: CLI — `serve` and one-shot `transcribe`  [deps: T14]

- **Files**: `services/transcription/src/transcription/cli.py`, `services/transcription/tests/test_cli.py`
- **Test first**: `services/transcription/tests/test_cli.py` (invoke `main(argv)` in-process; register `FakeProvider`) — cases: `main(["--help"])` exits 0 and prints usage listing both subcommands (FR-1 acceptance); `main(["transcribe", str(sample), "--out", str(tmp)])` exits **0**, writes `transcript.json`, prints **exactly one** JSON object on stdout (`json.loads(captured_out)` succeeds and captured_out has one line) and writes its progress/diagnostics to **stderr** (FR-10 acceptance, profile rule); a missing input file exits with the documented code **3** and a stderr message naming the file, with stdout empty (FR-10 acceptance); each failure class maps to its documented distinct exit code per the plan's taxonomy table — drive `FakeProvider` to raise `model_load` (4), `provider_auth` (5), `provider_rate_limited` (6), `timeout` (7), `cancelled` (8), and an unexpected `RuntimeError` (1) — and assert the codes are pairwise distinct (profile rule: distinct nonzero code per failure mode); an out-of-allowlist `--out` exits **2**; `--provider cloud` overrides the configured default and shows up in the stdout summary (FR-4); a `--model-path` flag beats `TRANSCRIBER_MODEL_PATH` which beats the config file, and the winner is reported in the summary (FR-16 acceptance); no argument named like a secret exists — `--api-key` is **not** a flag, and passing it errors as unknown (FR-9, profile rule "secrets never in argv"); `serve` wires config + `run_server` (assert with a monkeypatched `run_server` that it receives the merged config, including `--port 0`).
- **Implement**: `argparse` with subparsers `serve` and `transcribe`; shared flags `--config`, `--provider`, `--model`, `--model-path`, `--device`, `--language`, `--db`, `--allow-root` (repeatable), `--log-level`; `serve` adds `--port`. `main(argv=None) -> int` and a console-script wrapper `main()` used by `[project.scripts]`. `transcribe` runs the same `JobManager` in-process via `asyncio.run` so the sqlite row and the transcript are identical to the HTTP path (FR-10). Exit codes come from one `EXIT_CODES: dict[ErrorKind, int]` table, documented in `README.md`.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; the real commands are run once for real — `uv run transcription-service --help` and `uv run transcription-service transcribe <a generated wav> --out <tmp> --provider …` — and their exit codes observed (profile Verification); QA gate green.

### [ ] T16: Attribution audit, provider-isolation guard, opt-in GPU test, docs  [deps: T15]

- **Files**: `services/transcription/tests/test_attribution.py`, `services/transcription/tests/test_gpu_integration.py`, `services/transcription/tests/data/README.md`, `services/transcription/README.md`, `services/transcription/NOTICE`
- **Test first**: `services/transcription/tests/test_attribution.py` — cases: `NOTICE` exists, names `Vexa`, `Vexa-ai/vexa` and `Apache License, Version 2.0`, and lists every adapted origin path (FR-13 acceptance); each of `src/transcription/errors.py`, `src/transcription/filters.py`, `src/transcription/providers/local_whisper.py`, `tests/test_api_contract.py` contains the Apache-2.0 header **and** a comment naming its vexa origin path (FR-13 acceptance); provider isolation — scanning every `.py` under `src/transcription/` except `providers/` and `config.py`, none contains `litellm`, `faster_whisper`, `openai` or `groq` (FR-4 acceptance); no source file contains `shell=True`, `os.system` or `subprocess.` (profile rule). `services/transcription/tests/test_gpu_integration.py` — all `@pytest.mark.gpu`, skipped unless a real model directory is configured: transcribes a short real sample end-to-end, asserts `device == "cuda"`, `transcript.json` validates against the v1 schema, `realtime_factor` is recorded and `> 0`, the ledger row is `succeeded` with `cost_usd IS NULL`, and a **second** job in the same process logs no model-load event (FR-3, NFR-2, NFR-3, FR-15 acceptance).
- **Implement**: the GPU test resolves its sample from `TRANSCRIBER_TEST_SAMPLE`, else `tests/data/sample.wav` if the operator has dropped one there, else `pytest.skip` with an explanatory message — do not commit large binaries; `tests/data/README.md` documents how to place a sample. Finish `README.md`: the four QA commands, the exit-code table, the config keys and their `TRANSCRIBER_*` names, the ready-line contract F3 depends on, the `cost_usd IS NULL for local` rationale (NFR-8), and how to run `uv sync --extra cuda` for the GPU path.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: `uv run pytest -q` passes in **< 30 s** with no model, no GPU and networking disabled, and reports the `gpu` tests as deselected (FR-15 acceptance); `uv run pytest -m gpu` is runnable and self-skips with a clear message when no sample/model is present; idle RSS of a served process before the first job is under 300 MB, measured once and noted in the README (NFR-6); `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run pytest -q` all green.

## QA expectations

**No `make` targets exist and `make` is not installed on this machine.** The root `Makefile` is
owned by F4 (batch boundary) and this feature deliberately does **not** create one — a deviation
from the spec's stack note (`spec.md:80`, which offered a convenience Makefile), taken so the two
features cannot collide on the same file. F4 may add `format`/`lint`/`type`/`test` targets that
shell out to the commands below.

The QA gate for every task, run **inside `services/transcription/`**:

| Purpose | Command |
|---|---|
| format | `uv run ruff format --check .` |
| lint | `uv run ruff check .` |
| types | `uv run mypy` |
| tests | `uv run pytest -q` |

- The default `pytest` run is model-free, GPU-free and network-free and must stay under 30 s
  (FR-15). `addopts = -m "not gpu"` keeps the integration test out by default.
- Opt-in, operator machine only: `uv run pytest -m gpu` (needs the real `large-v3` weights and
  the RTX 4070; self-skips otherwise).
- CUDA extras are installed separately: `uv sync --extra cuda` (Windows only).
- Known-flaky watch items: the symlink-escape case in T6 needs Developer Mode or admin on
  Windows — it must `pytest.skip` rather than fail; the NFR-1/NFR-4 timing assertions should use
  generous margins (3 s and 50 ms are the spec's numbers; assert against them, but keep the fake
  sleeps that surround them short and deterministic).
