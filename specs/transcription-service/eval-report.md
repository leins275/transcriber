---
slug: transcription-service
base_ref: 6d0fce75f5cc49a0b46c6eb6c052d4029ab06f7d
round: 3
---

# Evaluation report: Python transcription microservice

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 3 | 0 |
| major | 1 | 6 | 0 |
| minor | 0 | 7 | 1 |

**Round 3 — final (verification only; fix budget exhausted).** All three round-2 findings
(E15, E16, E17) were re-reproduced against the current code and are **genuinely fixed**, each
with a test that uses the *real* provider registry rather than a fake. Measured on this machine
against a real `transcription-service serve` subprocess (not `TestClient`):

```
=== provider=local (real registry) ===
  first  GET /health   -> 200 in   54.4 ms   (NFR-1 budget 3 s; was 190 ms / 2714 ms in round 2)
  first  POST /v1/jobs -> 202 in    5.9 ms   (FR-2 budget 200 ms; was 218 ms in round 2)
  bogus provider POST  -> 400 in    2.1 ms   {"error_kind":"invalid_request", ...}
  non-ASCII auth       -> 401 (was 500 in round 2)
  job final            -> failed / model_load, worker survived, next job ran
  ledger rows          -> 2 (no row for the rejected request)   device='cuda'  (not 'auto')
=== provider=cloud (real registry) ===
  first  GET /health   -> 200 in   52.2 ms   (was 2714 ms in round 2)
  first  POST /v1/jobs -> 202 in    6.9 ms   (was 2938 ms in round 2)
  job final            -> failed / provider_auth        ledger device='cloud'
idle RSS before /health : 52.8 MB ; after /health : 53.0 MB   (NFR-6 budget 300 MB)
```

`/health` no longer constructs a provider at all, so it neither imports `faster_whisper`/`litellm`
nor 500s when a provider library is unimportable; the provider is resolved off the event loop in
`_run_job` via `asyncio.to_thread`, and a construction failure there fails *that job* as
`model_load` while the worker keeps draining. QA is green: **245 passed / 2 skipped / 1 deselected
in 10.9 s**, `ruff format --check` (39 files), `ruff check`, `mypy` (17 files) all clean.

The second fix pass introduced **one new major**: because `/health` no longer resolves the
provider and nothing else warms it at startup, `/health` reports the *unresolved config literals*
for the entire window before the first job — `device: "auto"` instead of the resolved `cuda`,
and, under `provider: cloud`, `model: "large-v3"` (a model the cloud provider will never use).
That window is precisely when F3's sidecar probes the endpoint, and it partially regresses E6.
It is a reporting-accuracy defect, not a functional break — the ledger's `device` column is
correct, and the values self-correct after the first job — so it is recorded as **E18 [major],
unresolved** rather than reopening E6.

## Findings

### E1 [blocker] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/jobs.py:236-268` (`_worker_loop`,
  `_finish_as_internal_failure`), `:270-369` (`_run_job`), `:143` (`submit`)
- **Spec ref**: FR-2, FR-8, NFR-7
- **Verified fixed (round 2, re-verified round 3)**: `_worker_loop` still wraps `_run_job` in
  `except Exception` and terminates the job as `failed`/`internal` while continuing to drain.
  `submit()` now rejects an unknown provider via `validate_provider_name()` — a registry
  *membership* check that never imports anything — before the ledger insert, which preserves the
  E1 guarantee while removing E15's cost. Re-reproduced against a live server:
  ```
  bogus provider POST -> 400 {"error_kind":"invalid_request",
    "error_message":"unknown provider 'bogus'; known providers: cloud, local"}
  ledger rows: 2 (both terminal; no row for the rejected request)
  ```
  Covered by `tests/test_jobs.py::test_submit_with_unknown_provider_raises_invalid_request_and_creates_no_row`,
  `::test_worker_loop_survives_run_job_raising_and_keeps_draining_queue` and the new
  `::test_provider_construction_failure_at_job_start_fails_as_model_load`.

### E2 [blocker] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/schema.py:19-42` (`Segment`)
- **Spec ref**: FR-4 acceptance, FR-6
- **Verified fixed**: `avg_logprob`/`no_speech_prob`/`compression_ratio` are `float | None = None`;
  unchanged this round. Covered by
  `tests/test_transcript.py::test_cloud_shaped_segment_with_none_confidence_fields_builds_and_writes`
  and `tests/test_jobs.py::test_cloud_shaped_none_confidence_segments_reach_succeeded_not_stuck_running`.

### E3 [blocker] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/ledger.py:70-82` (`Ledger.__init__`)
- **Spec ref**: FR-10 acceptance, FR-5, FR-8
- **Verified fixed**: `self.db_path.parent.mkdir(parents=True, exist_ok=True)` plus the
  `LedgerError` wrapper; unchanged this round. Re-confirmed indirectly in round 3 — the
  verification runs used a fresh `TRANSCRIBER_APP_DIR` with no `data/` and the ledger was created
  without a traceback. Covered by
  `tests/test_ledger.py::test_open_path_with_missing_parent_directory_creates_it` and
  `tests/test_cli.py::test_transcribe_succeeds_on_a_fresh_app_dir_with_no_data_folder`.

### E4 [major] [security] [status: fixed]

- **Where**: `services/transcription/src/transcription/config.py:51-54`, `:137-141`
- **Spec ref**: FR-9, Out of scope
- **Verified fixed**: `host` is still excluded from `known_fields`, so no layer can set it.
  Unchanged this round; covered by
  `tests/test_config.py::test_host_ignores_{env_override,config_file_value,explicit_override}`.

### E5 [major] [spec-drift] [status: fixed]

- **Where**: `services/transcription/src/transcription/config.py:42`,
  `providers/local_whisper.py:139-152`
- **Spec ref**: FR-3 (must), FR-3 acceptance ("no network access, verified offline")
- **Verified fixed**: default is `model: str = "large-v3"`; `_ensure_model` passes
  `local_files_only=True`. Re-reproduced in round 3 against a fresh app dir with an empty
  `models/`: the job failed with
  `model_load: failed to load model 'large-v3' from '<app>\models': Cannot find an appropriate
  cached snapshot folder ... outgoing traffic has been disabled` — no download attempted. The
  README half of this finding is now also correct (see E17).

### E6 [major] [correctness] [status: fixed — partially regressed, see E18]

- **Where**: `services/transcription/src/transcription/app.py:124-142` (`health`),
  `jobs.py:193-215` (`provider_info`)
- **Spec ref**: FR-2, FR-3 acceptance, FR-2 acceptance
- **Verified fixed**: `/health` no longer hardcodes `model_state` and does report the provider's
  live `describe()` — once the provider has been resolved. Observed on this machine after one job:
  `{"status":"ok","version":"0.1.0","provider":"local","model":"large-v3","device":"cuda","model_state":"unloaded"}`
  and, for cloud, `{"provider":"cloud","model":"whisper-1","device":"cloud","model_state":"loaded"}`
  — the resolved device and a live, advancing `model_state`. The FR-2 acceptance criterion
  (`model_state: "unloaded"` before any job) holds, covered by
  `tests/test_api_contract.py::test_health_returns_ok_with_unloaded_model_state_before_any_job`.
  **However**, the round-3 fix for E15 removed provider construction from this path without adding
  any other warm-up, so *before* the first job `/health` falls back to the unresolved config
  literals — the exact `"auto"` value round 1 flagged. Recorded as the new E18 rather than
  reopening this one, because the post-resolution behaviour that E6 asked for is genuinely present.

### E7 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/jobs.py:298` (`_run_job` →
  `mark_running(device=...)`), `ledger.py:140-156`
- **Spec ref**: FR-5, Decisions log
- **Verified fixed, by a different mechanism than round 2**: the resolved device is no longer
  written at `insert_job` time (E15 moved resolution off the request path); `insert_job` writes
  `config.device` as an explicit placeholder and `mark_running` corrects it to
  `provider_instance.describe().device` when the job starts. Real ledger rows from round-3 runs:
  `device='cuda'` for the local provider with `device: auto`, `device='cloud'` for the cloud
  provider — never `'auto'`. Covered by
  `tests/test_jobs.py::test_submitted_job_ledger_row_records_resolved_device_not_config_literal`.
  *Residual, accepted*: a job whose provider fails to **construct** never reaches `mark_running`,
  so its terminal row keeps the `'auto'` placeholder. There is no resolved device to record in
  that case, so the placeholder is the only honest answer; noted for the record only.

### E8 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/providers/local_whisper.py:56-82`,
  `:186-218`
- **Spec ref**: FR-8, FR-7
- **Verified fixed**: `_MODEL_LOAD_ERROR_MARKERS` and `_classify_transcribe_failure` are
  unchanged from round 2 and still separate a CUDA runtime-load failure (`model_load`) from a
  genuine decode failure (`audio_decode`). Covered by
  `tests/test_provider_local.py::test_cuda_runtime_load_failure_during_transcribe_maps_to_model_load`
  and `::test_genuine_decode_failure_still_maps_to_audio_decode_not_model_load`. The accepted
  residual (`device: auto` picks `cuda` on device *presence* without probing the runtime, no CPU
  fallback) is unchanged and is now documented in `README.md:90-99` (E17).

### E9 [minor] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/ledger.py:222-232` (`finish_cancelled`),
  `jobs.py:226-234`, `:350-353`
- **Spec ref**: FR-8
- **Verified fixed**: unchanged from round 2; `finish_cancelled` writes
  `error_kind='cancelled'` and both cancel paths set `JobState.error_kind`. Covered by
  `tests/test_ledger.py::test_finish_cancelled_records_error_kind_cancelled` and
  `tests/test_jobs.py::test_cancelled_job_carries_error_kind_cancelled_in_memory_and_ledger`.

### E10 [minor] [performance] [status: fixed]

- **Where**: `services/transcription/src/transcription/app.py:158-162`, `ledger.py:93-96`
- **Spec ref**: FR-2
- **Verified fixed**: unchanged — `limit: int = Query(default=50, ge=1, le=500)` and the
  `idx_jobs_created_at` / `idx_jobs_status` indexes. Covered by
  `tests/test_api_jobs.py::test_get_jobs_defaults_to_a_bounded_limit` and
  `tests/test_ledger.py::test_schema_has_indexes_on_created_at_and_status`.

### E11 [minor] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/jobs.py:174-191` (`status`),
  `:372-394` (`_job_state_from_ledger_row`)
- **Spec ref**: plan.md:58, FR-2
- **Verified fixed**: unchanged. Covered by
  `tests/test_jobs.py::test_status_hydrates_from_ledger_on_in_memory_cache_miss` and
  `tests/test_api_jobs.py::test_get_job_status_hydrates_from_ledger_after_restart`.

### E12 [minor] [security] [status: fixed]

- **Where**: `services/transcription/src/transcription/app.py:78-94` (`require_token`)
- **Spec ref**: FR-9
- **Verified fixed**: still `secrets.compare_digest`, now on the byte encodings (the E16 fix),
  which keeps the constant-time property. Asserted by
  `tests/test_api_contract.py::test_bearer_token_comparison_uses_constant_time_compare`, which
  spies on the call rather than the outcome.

### E13 [minor] [performance] [status: fixed]

- **Where**: `services/transcription/src/transcription/cli.py:152-160`
- **Spec ref**: FR-10
- **Verified fixed**: unchanged — 200 ms poll, printing only when `round(progress, 2)` changes.
  Covered by `tests/test_cli.py::test_progress_lines_are_deduplicated_by_rounded_value`.

### E14 [minor] [spec-drift] [status: accepted-with-rationale]

- **Where**: `config.py:60` (`job_timeout_sec`); `errors.py:33` (`UNSUPPORTED_INPUT`);
  `local_whisper.py:122,152` (`getattr(config, "word_timestamps", ...)`,
  `getattr(config, "cpu_threads", 4)`)
- **Spec ref**: FR-8 acceptance, FR-6 (`words?`), FR-16
- **Status this round**: unchanged and re-verified by grep. `job_timeout_sec` is parsed in
  `config.py` and referenced nowhere else in `src/`; `ErrorKind.UNSUPPORTED_INPUT` appears only in
  the two mapping tables (`app.py:44`, `cli.py:36`) and is never raised; `word_timestamps` and
  `cpu_threads` are still read via `getattr` from a frozen `Config` that has neither field, so
  FR-6's `words` key stays unreachable in production. Recommend a follow-up task scoped to FR-8's
  timeout enforcement and FR-16's two missing config knobs.

### E15 [major] [performance] [status: fixed]

- **Where**: `services/transcription/src/transcription/jobs.py:130-143` (`submit`, name-only
  validation), `:193-215` (`provider_info`), `:286-295` (`_run_job` resolving via
  `asyncio.to_thread`); `providers/__init__.py:31-54` (`known_provider_names`,
  `validate_provider_name`); `app.py:124-142` (`health`)
- **Spec ref**: FR-2 acceptance (202 within 200 ms), NFR-1 (`/health` 200 within 3 s), NFR-4
- **Verified fixed**: provider construction — and therefore the lazy
  `importlib.import_module` of `faster_whisper`/`litellm` — is gone from both event-loop paths.
  `submit()` validates only the *registry key*; `/health` reads the already-cached instance or
  falls back to config values; `_run_job` does the real resolution on a thread. Re-reproduced the
  exact round-2 scenario against a real server subprocess with the real registry (no fakes):
  ```
  provider=local: first GET /health ->  54.4 ms   (round 2:  190 ms)
  provider=local: first POST /v1/jobs -> 202 in 5.9 ms   (round 2: 218 ms, over the 200 ms budget)
  provider=cloud: first GET /health ->  52.2 ms   (round 2: 2714 ms)
  provider=cloud: first POST /v1/jobs -> 202 in 6.9 ms   (round 2: 2938 ms)
  ```
  The `/health`-coupled-to-importability half is fixed too: `provider_info()` cannot raise, so an
  unimportable provider library no longer turns the liveness probe into a 500 — and an actual
  import failure is now attributed to the job that triggered it
  (`model_load`, worker survives, next job succeeds). Crucially, the new tests use the **real**
  registry, closing round 2's "green in CI, broken in production" hole:
  `tests/test_api_contract.py::test_health_never_imports_a_provider_library` (asserts
  `"faster_whisper" not in sys.modules` after a real-`local` `/health`),
  `::test_submit_stays_under_budget_with_the_real_provider_registry`, and
  `tests/test_jobs.py::test_provider_construction_failure_at_job_start_fails_as_model_load`.
  *Side effect*: the chosen fallback (rather than a background warm-up) is the root of E18 below.

### E16 [minor] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/app.py:88-94`
- **Spec ref**: FR-9 acceptance ("With a token configured, a request without it gets `401`"), FR-8
- **Verified fixed**: the comparison is now on bytes —
  `secrets.compare_digest(authorization.encode("utf-8", errors="replace"), expected)` where
  `expected = f"Bearer {config.token}".encode()` — which `compare_digest` accepts for any byte
  content, so the `TypeError` path is gone while the constant-time property is kept.
  Re-reproduced against a live server with a raw `b"Bearer \xe9\xe9"` header:
  ```
  non-ASCII auth -> 401 {"detail":"unauthorized"}     (round 2: 500 internal)
  ```
  Covered by `tests/test_api_contract.py::test_non_ascii_authorization_header_returns_401_not_500`,
  which sends the raw bytes rather than a pre-decoded `str`.

### E17 [minor] [spec-drift] [status: fixed]

- **Where**: `services/transcription/README.md:62`, `:81-99`, `:147-156`
- **Spec ref**: FR-3, FR-16, NFR-6
- **Verified fixed**: the config table row now reads `| model | TRANSCRIBER_MODEL | large-v3 |`,
  matching `config.py:42`. A new section, "Model weights and CUDA runtime are prerequisites, not
  this service's job", documents `local_files_only=True` (never downloads; empty `model_path` →
  `model_load` / CLI exit `4`) and the `device: auto` presence-probe caveat with the
  `uv sync --extra cuda` remedy — both behaviours a first-run user will actually hit. The NFR-6
  number was re-measured with the real default provider and is now accurate; independently
  confirmed here on a real served process:
  ```
  idle RSS before /health : 52.8 MB      README claims 53-56 MB
  idle RSS after  /health : 53.0 MB      (NFR-6 budget 300 MB)
  ```

### E18 [major] [correctness] [status: open — unresolved, fix budget exhausted]

- **Where**: `services/transcription/src/transcription/jobs.py:193-215` (`provider_info`'s
  fallback branch), `app.py:124-142` (`health`), `app.py:126` (`config.public()`, whose `model`
  key is always `config.model`)
- **Spec ref**: FR-3 acceptance ("`device: auto` selects `cuda` on the reference machine and
  `cpu` when CUDA is forced unavailable; **`/health` reports which**"), FR-2 (`GET /health` →
  `{... provider, model, device ...}`)
- **Expected**: `/health` reports the device the service will actually use and the model the
  configured provider will actually run — from the first probe, which is when F3's sidecar
  (Q3 → A) reads it.
- **Actual**: The E15 fix removed provider construction from `/health` but added no other
  warm-up, so until the first job completes provider resolution, `provider_info()` takes the
  fallback branch and reports unresolved `Config` literals. Observed on this machine against a
  real server, before any job:
  ```
  provider=local, fresh process:
    GET /health -> {"provider":"local","model":"large-v3","device":"auto","model_state":"unloaded"}
    ... after one job:
    GET /health -> {"provider":"local","model":"large-v3","device":"cuda","model_state":"unloaded"}
  provider=cloud, fresh process:
    GET /health -> {"provider":"cloud","model":"large-v3","device":"auto","model_state":"unloaded"}
    ... after one job:
    GET /health -> {"provider":"cloud","model":"whisper-1","device":"cloud","model_state":"loaded"}
  ```
  Two distinct defects in that window: (1) `device` is the literal `"auto"`, which is exactly the
  value E6 was raised to eliminate — FR-3's acceptance criterion is unmet until a job has run;
  (2) under `provider: cloud`, `model` is `"large-v3"`, the *local* whisper model id, which the
  cloud provider will never use — that is not merely unresolved, it is wrong, and it would mislead
  anyone comparing providers or debugging "which model produced this transcript". No test covers
  `/health` after provider resolution in either direction — `tests/test_api_contract.py:59-81`
  pins the pre-resolution literals as the expected body, and nothing asserts the resolved case —
  so this seam is uncovered in CI exactly as round 2's budget tests were.
- **Suggested fix**: Keep `/health` construction-free and warm the default provider in the
  background instead: in `create_app`'s `lifespan`, after `await job_manager.start()` and after
  the ready line has been emitted, schedule a fire-and-forget
  `asyncio.to_thread(job_manager._get_provider, config.provider)` (wrapped so a failure is logged,
  never raised). Cold start and the ready-line handshake are untouched — the import runs off the
  event loop, after the socket is already serving — and `/health` becomes accurate within a few
  hundred ms (local) to ~3 s (cloud) of startup. As a stopgap for the remaining window, have the
  fallback branch report `cloud_model` as `model` when `config.provider == "cloud"`, and either
  `"auto"` explicitly labelled as unresolved or the same `_resolve_device_and_compute_type`
  answer. Add two tests: `/health` reports `device != "auto"` once a job has resolved a provider,
  and `/health` under `provider: cloud` never reports the local whisper model id.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 | `pyproject.toml`, `src/transcription/__init__.py` | `tests/test_packaging.py` (6); `uv run transcription-service --help` exits 0 | ✓ |
| FR-2 | `app.py:124-202`, `jobs.py:64-369` | `tests/test_api_jobs.py` (9), `tests/test_api_contract.py` (12), `tests/test_jobs.py` (19); 202-in-200 ms now measured against the real registry | ✓ (E18: `/health` `model`/`device` accuracy pre-resolution) |
| FR-3 | `providers/local_whisper.py:110-245` | `tests/test_provider_local.py` (20) incl. `local_files_only`, device resolution | gap — E18 (`/health` reports `"auto"`, not the resolved device, until a job runs) |
| FR-4 | `providers/__init__.py`, `base.py`, `litellm_cloud.py` | `tests/test_provider_registry.py` (9), `tests/test_provider_cloud.py` (13), `tests/test_attribution.py` grep guard | ✓ |
| FR-5 | `ledger.py:67-270`, `jobs.py:157-169`, `:298` | `tests/test_ledger.py` (12), `tests/test_jobs.py::test_submitted_job_ledger_row_records_resolved_device_not_config_literal`; real runs show `device='cuda'`/`'cloud'` | ✓ |
| FR-6 | `schema.py:19-77`, `transcript.py` | `tests/test_transcript.py` (16) incl. the cloud-shaped `None` case | gap — E14 (`words` unreachable in production) |
| FR-7 | `providers/local_whisper.py:178-218` | `tests/test_provider_local.py` decode/CUDA classification; real containers only in the opt-in `gpu` test | ✓ |
| FR-8 | `errors.py`, `app.py:96-122`, `jobs.py:236-369`, `local_whisper.py:73-82` | `tests/test_errors.py` (7 + params), `tests/test_jobs.py` worker-survival, cancel kinds, provider-construction-failure → `model_load` | gap — E14 (`unsupported_input`, `timeout` unreached) |
| FR-9 | `paths.py`, `app.py:78-94`, `config.py:137-141` | `tests/test_paths.py` (14), `tests/test_config.py::test_host_ignores_*` (3), `tests/test_server.py::test_binds_socket_on_loopback_only`, `test_non_ascii_authorization_header_returns_401_not_500` | ✓ |
| FR-10 | `cli.py:115-190` | `tests/test_cli.py` (14) incl. fresh-app-dir and progress dedup; verified by a real run | ✓ |
| FR-11 | `jobs.py:217-234`, `app.py:199-202` | `tests/test_jobs.py::test_cancel_*`, `tests/test_api_jobs.py::test_cancel_running_job_*` | ✓ |
| FR-12 | `filters.py`, `local_whisper.py:226` | `tests/test_filters.py` (12), `tests/test_provider_local.py` filter on/off | ✓ |
| FR-13 | `NOTICE`, headers in `errors.py`, `filters.py`, `local_whisper.py`, `test_api_contract.py` | `tests/test_attribution.py` (5) | ✓ |
| FR-14 | `logging_setup.py`, `server.py:29-131` | `tests/test_logging.py` (10), `tests/test_server.py` (7); ready line consumed by the round-3 verification harness | ✓ |
| FR-15 | whole `tests/` tree | `uv run pytest`: 245 passed, 2 skipped, 1 deselected, 10.9 s, no model/GPU/network | ✓ |
| FR-16 | `config.py:108-211` | `tests/test_config.py` (17), `tests/test_cli.py::test_model_path_flag_beats_env_beats_config_file` | ✓ |
| NFR-1 | lazy imports, `create_app` factory, construction-free `/health` | `tests/test_server.py::test_health_answers_within_three_seconds_of_process_start`, `tests/test_api_contract.py::test_health_never_imports_a_provider_library` (**real registry**) | ✓ (measured 52-54 ms with both `local` and `cloud` configured) |
| NFR-2 / NFR-3 | `local_whisper._ensure_model` caching | `tests/test_gpu_integration.py` (opt-in, self-skipping — not executed here) | deferred |
| NFR-4 | `ThreadPoolExecutor(max_workers=1)` + `run_in_executor`; provider resolution via `asyncio.to_thread` | `tests/test_jobs.py::test_status_returns_fast_while_provider_blocks_worker_thread` | ✓ (the event loop is no longer blocked by a provider import) |
| NFR-5 | `transcript.write_atomic` | `tests/test_transcript.py` (mid-write failure, no leftover `*.tmp`) | ✓ |
| NFR-6 | construction-free `/health` | README measurement, not automated; independently measured 52.8 MB idle / 53.0 MB after `/health` | ✓ |
| NFR-7 | `ledger.reconcile_interrupted`, one-row discipline, `_finish_as_internal_failure` | `tests/test_ledger.py`, `tests/test_jobs.py::test_exactly_one_ledger_row_per_job_across_terminal_outcomes`, `::test_worker_loop_survives_run_job_raising_and_keeps_draining_queue` | ✓ |
| NFR-8 | `ledger` NULL cost, `litellm_cloud._extract_cost` | `tests/test_ledger.py` (`IS NULL`), `tests/test_provider_cloud.py` (cost hooks) | ✓ |

## Positive notes

- **The E15 fix chose the cheap invariant over the expensive one.** Splitting
  `validate_provider_name` (a `dict` membership check) out from `get_provider` (an
  `importlib` call) is what lets `submit()` keep E1's synchronous 400 *and* the 200 ms budget at
  the same time. Do not "simplify" these back into one function.
- **The round-3 tests use the real registry, not `FakeProvider`.**
  `test_health_never_imports_a_provider_library` asserts on `sys.modules` after popping the
  module, and `test_submit_stays_under_budget_with_the_real_provider_registry` builds a
  `provider="local"` config. That is the direct answer to round 2's complaint that every budget
  test was green because it never touched a real provider — keep this property in any future test
  added to these seams.
- **A provider that cannot even be constructed is now that job's `model_load` failure**, not a
  worker crash and not the `internal` catch-all, and the queue keeps draining behind it
  (`jobs.py:287-295` re-raises `ServiceError` untouched and reclassifies everything else). The
  `except ServiceError: raise` line before the broad handler is deliberate — removing it would
  swallow real provider taxonomies into `model_load`.
- **`_provider_lock`** was added at the same time resolution moved onto arbitrary
  `asyncio.to_thread` worker threads. It is not decoration: without it two threads could construct
  the same provider twice and load the model twice (NFR-2). `provider_info()` reads the dict
  without the lock, which is correct — a single `dict.get` needs no lock and must not block the
  event loop.
- **The E16 fix compares bytes rather than guarding with `isascii()`**, which keeps
  `compare_digest`'s constant-time property for every input instead of introducing an early return
  that leaks a timing signal. Keep the byte form.
- **`host` is still structurally unsettable**, `local_files_only=True` is still enforced, and
  `_finish_as_internal_failure` still catches `Exception` (not `BaseException`) so
  `asyncio.CancelledError` reaches `aclose()`. Everything praised in rounds 1-2 —
  `write_atomic`, `paths.py`'s fail-closed `commonpath`/`normcase` comparison, the
  one-insert-then-update ledger discipline, the single ready line, `redact()` at every provider
  boundary, the audited attribution — survived this fix pass unchanged.
