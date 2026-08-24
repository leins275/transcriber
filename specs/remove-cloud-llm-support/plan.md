---
slug: remove-cloud-llm-support
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Remove cloud LLM and cloud STT support (local-only)

## Architecture overview

This is a pure-removal feature confined to the Python sidecar service plus docs. No new
components; the existing seams stay and shrink:

- **STT provider registry** — `services/transcription/src/transcription/providers/__init__.py`
  `_REGISTRY` drops the `"cloud"` entry; `providers/litellm_cloud.py` is deleted. The
  registry mechanism (`register()` test hook, lazy `importlib` resolution,
  `validate_provider_name` → `invalid_request` before any ledger row) is untouched — a
  `provider="cloud"` request now falls into the existing unknown-name rejection.
- **LLM engine registry** — `services/transcription/src/transcription/llm/__init__.py`
  `_REGISTRY` drops `"openai_compat"`; `llm/openai_compat.py` is deleted. `BUILTIN_ENGINE`
  (`"llama_cpp"`) becomes the only shipping engine.
- **Config** — `config.py` loses six fields (`cloud_model`, `provider_api_key`,
  `max_cloud_upload_mb`, `llm_provider`, `llm_base_url`, `llm_api_key`), their env pickups
  (incl. the `OPENAI_API_KEY`/`GROQ_API_KEY` fallback block), their coercion branches, and
  their `public()` rows. `_SECRET_KEYS` shrinks to `{"token", "hf_token"}`. Removed keys in
  an installed `config.json` fall into the existing unknown-key-ignored path (FR-4) — no
  migration needed.
- **Job manager** — `jobs.py` stops reading `config.llm_provider` in three places: the
  engine factory (line ~133) and per-job resolution (line ~296) become
  `provider or BUILTIN_ENGINE`; the LLM status payload (lines ~217–226) always checks the
  GGUF file and drops its `"llm_provider"` response key. The desktop consumes only
  `llm_model_present` from that payload (verified: `apps/desktop/src-tauri/src/service/http.rs`,
  `commands/llm.rs`), so dropping the key is contract-safe.
- **Errors** — `errors.py::classify_http_status` loses its last two callers (both deleted
  files) and is pruned along with its direct tests in `tests/test_errors.py`. The
  `ErrorKind` taxonomy itself (`PROVIDER_AUTH`, `PROVIDER_UNAVAILABLE`, ...) stays — the
  desktop asserts on those strings (spec, out of scope).
- **Dependencies** — `litellm>=1.60` leaves `pyproject.toml`; `uv.lock` is regenerated
  (`make lint` runs `scripts/verify_locks.py --check`).
- **Rust/TS side** — zero source changes; verified green by the final QA task (FR-8).

Interpretation note (flagged for the plan gate): the FR-3 acceptance "grep over
`src/transcription/` finds none of these identifiers" is read as *config-field usages*.
The substring `llm_provider` legitimately survives inside the registry function names
`get_llm_provider` / `validate_llm_provider_name` / `known_llm_provider_names`, which the
spec's out-of-scope section explicitly keeps.

## Risks

- **Mid-plan broken tree**: removing `Config.llm_provider` while `jobs.py` still reads it
  would break the suite between tasks. Mitigated by ordering: T4 rewrites `jobs.py` to
  `BUILTIN_ENGINE` first (harmless while the config field still exists and defaults to
  `"llama_cpp"`), then T3 deletes the field (`deps: T4`).
- **Lockfile churn**: `uv lock` can re-pin unrelated packages. T6 runs a plain `uv lock`
  and the `verify_locks.py --check` gate; the diff should show only litellm and its
  now-orphaned transitives (`openai`, etc.) leaving.
- **Test-collection failure ordering**: `tests/test_provider_cloud.py` imports
  `litellm.exceptions`; it must be deleted (T2) before `uv.lock` drops litellm from the
  environment (T6 `deps: T1, T2`), or pytest collection breaks.
- **Contract regressions**: `cost_usd`, the `provider` ledger column, and the error
  taxonomy are consumed by the desktop app and must survive. Tasks only rename/reword the
  tests around them, never remove the fields (spec Decisions log).

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T7 |
| 2 | T4, T5, T6 |
| 3 | T3 |
| 4 | T8 |

(The orchestrator schedules by `deps` + `Files`; e.g. T6 only needs T1+T2 and can start
the moment those clear, regardless of T4/T5.)

## Tasks

### [x] T1: Remove the openai_compat LLM engine  [deps: —]

- **Files**: `services/transcription/src/transcription/llm/openai_compat.py` (delete),
  `services/transcription/src/transcription/llm/__init__.py`,
  `services/transcription/src/transcription/llm/llama_cpp_local.py`,
  `services/transcription/tests/test_llm_units.py`
- **Test first**: `services/transcription/tests/test_llm_units.py` — cases:
  `known_llm_provider_names() == {"llama_cpp"}` (FR-2); `validate_llm_provider_name("openai_compat")`
  raises `ServiceError(invalid_request)` naming the known engines (FR-2, currently line 53
  asserts the opposite — flip it); the import-isolation case (line ~41, pops
  `llama_cpp`/`litellm` from `sys.modules`) still proves importing `transcription.llm`
  loads no engine library (NFR-2) — drop the `litellm` mention once dead.
- **Implement**: Delete `openai_compat.py`. In `llm/__init__.py` remove the
  `"openai_compat"` `_REGISTRY` entry and the litellm mentions in the module/`validate`
  docstrings; keep `BUILTIN_ENGINE`, `register()`, and lazy resolution unchanged. Update
  the `llama_cpp_local.py` header docstring (lines 1–5) that names `openai_compat.py` and
  `litellm` as isolation-exempt.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: `llm/openai_compat.py` does not exist;
  `uv run --directory services/transcription pytest tests/test_llm_units.py -q` passes;
  full service suite (`uv run --directory services/transcription pytest -q`), `ruff check .`
  and `mypy src` (same directory) stay green.

### [x] T2: Remove the cloud STT provider  [deps: —]

- **Files**: `services/transcription/src/transcription/providers/litellm_cloud.py` (delete),
  `services/transcription/src/transcription/providers/__init__.py`,
  `services/transcription/tests/test_provider_cloud.py` (delete),
  `services/transcription/tests/test_provider_registry.py`
- **Test first**: `services/transcription/tests/test_provider_registry.py` — cases:
  `known_provider_names() == {"local"}` (FR-1, replaces the `{"local","cloud"} <= names`
  assertion at line ~41); `validate_provider_name("cloud")` raises
  `ServiceError(invalid_request)` whose message names the known providers (FR-1, adapts the
  line-61 expectation); import-isolation cases keep proving no provider library import at
  registry-import time (NFR-2), with the now-moot `sys.modules.pop("litellm")` guards
  removed or retired.
- **Implement**: Delete `litellm_cloud.py` and the whole `tests/test_provider_cloud.py`.
  In `providers/__init__.py` remove the `"cloud"` `_REGISTRY` entry and rewrite the module
  docstring / comments that name `litellm_cloud.py` and `litellm`; keep `register()`,
  `known_provider_names`, `validate_provider_name`, `get_provider` signatures unchanged.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: both deleted files are gone (FR-1, FR-6 acceptance);
  `uv run --directory services/transcription pytest -q`, `ruff check .`, `mypy src` pass
  (the `test_cli.py` cloud-name test still passes via the `register()` hook — renamed later
  by T5).

### [x] T4: jobs.py engine resolution + HTTP-level rejections  [deps: T1, T2]

- **Files**: `services/transcription/src/transcription/jobs.py`,
  `services/transcription/tests/test_jobs.py`,
  `services/transcription/tests/test_api_contract.py`
- **Test first**: `services/transcription/tests/test_api_contract.py` — cases:
  `POST /v1/jobs` with `{"provider": "cloud", ...}` returns the `invalid_request` error
  shape naming known providers and creates no ledger row (FR-1 acceptance); an LLM job
  (`summarize`) with `provider="openai_compat"` is rejected `invalid_request` (FR-2
  acceptance); the health/LLM-status expectation at line ~97 no longer contains an
  `llm_provider` key (FR-3). `services/transcription/tests/test_jobs.py` — case: an LLM job
  with `provider=None` resolves to `BUILTIN_ENGINE` (FR-3); reword the "cloud-shaped
  segments" tests (lines ~526–564) to describe providers that omit confidence fields —
  behavior kept, cloud framing dropped (FR-6).
- **Implement**: In `jobs.py`: factory lambda (line ~133) → `get_llm_provider(BUILTIN_ENGINE,
  config)`; LLM status (lines ~213–226) → always check
  `Path(config.llm_model_path) / config.llm_model_file`, drop the `"llm_provider"` key and
  the external-server docstring; per-job resolution (line ~296) →
  `provider_name = provider or BUILTIN_ENGINE`. Do NOT touch `config.py` yet — the field
  still exists and defaults to `"llama_cpp"`, keeping the tree green until T3.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: `jobs.py` no longer reads `config.llm_provider`;
  `uv run --directory services/transcription pytest -q`, `ruff check .`, `mypy src` pass.

### [x] T5: Prune dead seam helpers and cloud remnants in remaining tests  [deps: T1, T2]

- **Files**: `services/transcription/src/transcription/errors.py`,
  `services/transcription/src/transcription/cli.py`,
  `services/transcription/src/transcription/schema.py`,
  `services/transcription/tests/test_errors.py`,
  `services/transcription/tests/test_cli.py`,
  `services/transcription/tests/fakes.py`,
  `services/transcription/tests/test_ledger.py`,
  `services/transcription/tests/test_transcript.py`,
  `services/transcription/tests/test_attribution.py`
- **Test first**: `services/transcription/tests/test_errors.py` — remove the
  `classify_http_status` parametrized cases (line ~49) together with the helper; assert the
  `ErrorKind` taxonomy members and `redact`/`_SECRET_PATTERNS` behavior stay intact (NFR-3,
  spec out-of-scope). `tests/test_cli.py` — rename the line-303 test to register the fake
  under a neutral name (e.g. `"fake_stt"`) instead of `"cloud"`, proving the `register()`
  hook still works (FR-6). `tests/test_ledger.py` (line ~215) and `tests/test_transcript.py`
  (lines ~102–130) — keep the `cost_usd`/null-confidence behavior tests, reworded without
  cloud framing (FR-6; `cost_usd` and the `provider` column stay per spec).
- **Implement**: Prune `errors.py::classify_http_status` (last callers deleted by T1/T2);
  keep every `ErrorKind` member. Update the `cli.py` comment (line ~53) to say credentials
  = `token` only. Fix the `schema.py` docstring (lines ~27–33) and `tests/fakes.py`
  module docstring to stop describing a cloud STT capability. In `test_attribution.py`,
  update the comments (lines ~25–29) that justify litellm's presence; keep
  `_PROVIDER_LIBRARY_STRINGS = ("litellm", "faster_whisper", "openai", "groq", "llama_cpp")`
  as the FR-5 guard.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: no test module references a current cloud capability;
  `test_provider_libraries_are_confined_to_the_provider_seam` passes;
  `uv run --directory services/transcription pytest -q`, `ruff check .`, `mypy src` pass.

### [x] T6: Drop the litellm dependency and regenerate uv.lock  [deps: T1, T2]

- **Files**: `services/transcription/pyproject.toml`, `services/transcription/uv.lock`
- **Test first**: red/green via the lock gate — after editing `pyproject.toml`,
  `uv run scripts/verify_locks.py --check` (repo root) fails until `uv.lock` is
  regenerated; the existing
  `tests/test_attribution.py::test_provider_libraries_are_confined_to_the_provider_seam`
  is the standing guard that no src file references litellm/openai/groq (FR-5).
- **Implement**: Remove `"litellm>=1.60"` from `[project.dependencies]`; rewrite the
  `description` (line 4) to drop "litellm cloud providers" (e.g. "Standalone transcription
  microservice: local faster-whisper + built-in llama.cpp LLM, over localhost HTTP and a
  one-shot CLI."). Regenerate the lock with `uv lock` inside `services/transcription`;
  eyeball the diff — only litellm and its orphaned transitives (openai etc.) may leave.
- **Skills**: testing-toolkit:python-testing-patterns, devops-toolkit:devops-rollout-plan
- **Done when**: `litellm` absent from `pyproject.toml` and `uv.lock`;
  `uv run scripts/verify_locks.py --check` passes;
  `uv run --directory services/transcription pytest -q` passes in the re-synced
  environment (proving nothing still imports litellm).

### [x] T7: Docs and comments follow reality  [deps: —]

- **Files**: `services/transcription/README.md`, `docs/config-contract.md`,
  `services/transcription/src/transcription/__init__.py`, `scripts/build_pyenv.py`
- **Test first**: none — documentation/comment task; verification is the grep acceptance
  in Done-when (FR-7). Historical documents (`specs/*`, `IDEA.md`, `vexa/`) are explicitly
  untouched.
- **Implement**: README — drop the config-table rows for `cloud_model`,
  `provider_api_key`, `max_cloud_upload_mb`, `llm_provider`, `llm_base_url`, `llm_api_key`
  (lines ~67–92), the credentials list at ~96, the openai_compat prose at ~112–115, the
  cloud-cost paragraph at ~239–241, and the "swapping to a cloud STT" intro claim (line 5).
  `docs/config-contract.md` — trim the `llm_*` example list at lines ~71–72 to surviving
  keys. `src/transcription/__init__.py` — remove "or a cloud speech-to-text provider" from
  the docstring. `scripts/build_pyenv.py` — swap the `litellm.exe` example (line ~237) for
  a surviving entry point (e.g. `uvicorn.exe`); comment-only change.
- **Skills**: — (no domain toolkit applies; docs only)
- **Done when**: `grep -i "cloud\|litellm"` over `services/transcription/README.md` and
  `docs/config-contract.md` returns no hit describing a current capability (FR-7
  acceptance); `uv run --directory services/transcription pytest -q` still passes (the
  `__init__.py` docstring edit is import-safe).

### [x] T3: Purge the config surface  [deps: T4]

- **Files**: `services/transcription/src/transcription/config.py`,
  `services/transcription/tests/test_config.py`
- **Test first**: `services/transcription/tests/test_config.py` — cases: `Config` exposes
  none of the six removed fields (FR-3); setting `OPENAI_API_KEY`, `GROQ_API_KEY`,
  `TRANSCRIBER_PROVIDER_API_KEY`, `TRANSCRIBER_LLM_API_KEY`, `TRANSCRIBER_LLM_BASE_URL`,
  `TRANSCRIBER_LLM_PROVIDER` in `env` has no effect on the loaded `Config` (FR-3
  acceptance); `Config.public()` contains no removed key (FR-3); a config file containing
  all seven removed keys (`provider: "cloud"` value included as a key-with-leftover-value)
  loads into a valid `Config` via the unknown-key-ignored path (FR-4 acceptance);
  `load_config(overrides={"token": ...})` and `overrides={"hf_token": ...}` still raise
  `ConfigError` (NFR-3). Rework the existing secret tests: line ~138
  (`provider_api_key` override) retargets to `token`/`hf_token`; line ~121
  (`test_public_contains_no_secrets`) drops `TRANSCRIBER_PROVIDER_API_KEY`.
- **Implement**: In `config.py` delete the six dataclass fields and their comments; shrink
  `_SECRET_KEYS` to `{"token", "hf_token"}`; delete the `provider_api_key` env-fallback
  block (lines ~287–295), the `llm_api_key` comment block (~314–317), the
  `max_cloud_upload_mb` int coercion (~331–332) and `llm_base_url` normalization
  (~363–364); remove the four removed rows from `public()` (keep `llm_model`, `llm_ctx`,
  `llm_gpu_layers`). Everything else in the layering (defaults < file < env < overrides,
  pinned `host`, nested-`model` unpacking) stays byte-identical.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: grep over `src/transcription/` finds no usage of the six removed config
  identifiers (registry function names `*_llm_provider*` excepted, per the interpretation
  note); `uv run --directory services/transcription pytest -q`, `ruff check .`,
  `mypy src` pass.

### [x] T8: Full QA gate and cross-language verification  [deps: T3, T5, T6, T7]

- **Files**: fix-forward only — may touch any file already declared by T1–T7
  (`services/transcription/**`, `docs/config-contract.md`, `scripts/build_pyenv.py`);
  no file under `apps/desktop/` or `crates/` (FR-8 requires them unchanged).
- **Test first**: no new tests — this task executes the acceptance sweep: (a) `make format`,
  `make lint` (includes `verify_locks.py --check` and clippy), `make type` (mypy
  `--strict`), `make test` (cargo + vitest + pytest + scripts tests) — NFR-1, FR-5, FR-8;
  (b) grep sweeps: `litellm|openai|groq` in zero files under
  `services/transcription/src/transcription/` (FR-5); no cloud-capability hits in
  README/config-contract (FR-7); `git status` shows no change under `apps/desktop/` or
  `crates/` (FR-8).
- **Implement**: Run the gates; start the service once
  (`uv run --directory services/transcription transcription-service` or equivalent dev
  entry) and confirm `GET /health` output contains no removed key and startup/ready-line
  behavior is unchanged (FR-3, NFR-2 — the desktop-profile "prove it runs, not just
  tests" rule, scoped to the sidecar since no desktop code changed). Fix any residual
  breakage within the declared file set only.
- **Skills**: testing-toolkit:python-testing-patterns, devops-toolkit:devops-rollout-plan
- **Done when**: all four make targets pass from the repo root; every FR-1..FR-8
  acceptance checkbox in `specs/remove-cloud-llm-support/spec.md` is satisfiable; no
  source change outside the declared file set.

## QA expectations

- All four root targets exist and are the gate: `make format`, `make lint`, `make type`,
  `make test` (Makefile fans each out to cargo + npm + uv; recipes are fail-fast).
- `make lint` additionally runs `scripts/sync_version.py --check` and
  `scripts/verify_locks.py --check` — the latter is why T6 must regenerate `uv.lock` in
  the same task as the `pyproject.toml` edit.
- `make test` also runs `uv run --with pytest -- pytest scripts/tests -q`; nothing there
  references litellm/cloud (verified), so it should stay green untouched.
- Per-task loops use the scoped Python commands
  (`uv run --directory services/transcription pytest -q` / `ruff check .` / `mypy src`);
  the full cross-language fanout runs once in T8. Default pytest excludes `gpu`-marked
  tests (`addopts = -q -m "not gpu"`); nothing here needs them.
- Batch note: this feature runs in its own worktree; every declared file is under
  `services/transcription/**` except `docs/config-contract.md` and comment-only edits to
  `scripts/build_pyenv.py` — no overlap risk with sibling features touching the desktop
  app or crates.
