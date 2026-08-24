---
slug: remove-cloud-llm-support
created: 2026-08-24
status: approved
---

# Spec: Remove cloud LLM and cloud STT support (local-only)

## Summary

Remove every cloud-model pathway from the Transcriber service: the litellm-backed cloud
speech-to-text provider, the OpenAI-compatible external LLM engine, all configuration keys
that exist only to serve them, their credentials handling, tests, the `litellm` dependency,
and their documentation. After this change the service ships exactly two model runtimes,
both local: faster-whisper for transcription and the built-in llama.cpp engine for LLM jobs.
This enforces the project's standing local-only direction — no cloud LLM or STT, ever.

## Problem & context

The service was originally built with a provider abstraction so a cloud STT backend could be
swapped in (`services/transcription/src/transcription/providers/litellm_cloud.py`, registered
as `"cloud"` in `providers/__init__.py`), and the LLM layer grew a secondary
`openai_compat` engine (`llm/openai_compat.py`, registered in `llm/__init__.py`) that calls an
external OpenAI-protocol server through litellm. The operator has decided the product is
local-only, permanently. The cloud code paths are now dead weight that:

- carry API-key plumbing (`provider_api_key`, `llm_api_key`, the `OPENAI_API_KEY` /
  `GROQ_API_KEY` env fallbacks in `config.py::load_config`) and its secret-redaction burden
  (`_SECRET_KEYS` in `config.py`),
- keep `litellm>=1.60` (and its transitive `openai` client) as a hard install dependency in
  `services/transcription/pyproject.toml`, inflating the installer's baked Python environment,
- and mislead readers: the pyproject description and README still advertise "litellm cloud
  providers".

The desktop app has **no** cloud settings surface — a repo-wide search of `apps/desktop` and
`crates/` finds no reference to `cloud`, `litellm`, `llm_base_url`, `llm_api_key`, or
`provider_api_key` — so this is a Python-service-plus-docs removal; the Rust/TS side only
needs to stay green.

Binding intake decision (`specs/_intake/various-improvements/intake.md`, Decisions log):
remove BOTH the cloud LLM client and the cloud STT provider, plus every related config key
(`provider=cloud`, `cloud_model`, `provider_api_key`, `max_cloud_upload_mb`, `llm_provider`,
`llm_base_url`, `llm_api_key`), their secret-redaction entries, tests, deps, and doc mentions.
No cloud fallback of any kind may remain.

## Users

- **The operator** (single desktop-app user): unchanged day-to-day behavior — all jobs already
  run locally by default; gains a guarantee that no audio or transcript text can ever leave
  the machine, and a smaller installed footprint.
- **Repo developers**: a smaller config/provider surface to maintain and a dependency tree
  without litellm.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists; `[dependencies] tauri` in
  `apps/desktop/src-tauri/Cargo.toml` (Tauri 2 app with bundle/packaging config).
- `web` — webview UI: `react` 18 and `vite` 5 in `apps/desktop/package.json` (per the desktop
  profile, UI toolkits come from `web`; the privileged process rules come from `desktop`).

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Privileged process | Rust / Tauri 2 | `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/Cargo.toml` |
| UI (webview) | React 18 + Vite 5, Vitest | `apps/desktop/package.json` |
| Sidecar service | Python 3.12, FastAPI + uvicorn, faster-whisper, llama-cpp-python, litellm (to be removed) | `services/transcription/pyproject.toml` |
| Shared libs | Rust workspace crates | `crates/vault/` |
| Job store | SQLite ledger | `services/transcription/src/transcription/ledger.py` |
| Testing | cargo test, vitest, pytest (+ ruff, mypy strict, clippy) | root `Makefile`, `services/transcription/pyproject.toml` dev group |

Makefile QA targets present: format, lint, type, test (all four; each fans out to
cargo + npm + uv, and `lint` additionally runs `scripts/verify_locks.py --check`, so the
`uv.lock` must be regenerated when `pyproject.toml` changes).

## Functional requirements

- **FR-1** (must): The cloud STT provider is gone. `services/transcription/src/transcription/providers/litellm_cloud.py`
  is deleted and the `"cloud"` entry is removed from `providers/__init__.py::_REGISTRY`
  (the registry, `register()` test hook, and `"local"` entry remain). A job submitted with
  `provider="cloud"` (HTTP `POST /v1/jobs` or CLI `--provider cloud`) is rejected by the
  existing `validate_provider_name` path as `invalid_request`, before any ledger row exists,
  with a message naming the known providers.
- **FR-2** (must): The external OpenAI-compatible LLM engine is gone.
  `services/transcription/src/transcription/llm/openai_compat.py` is deleted and the
  `"openai_compat"` entry removed from `llm/__init__.py::_REGISTRY`; `llama_cpp`
  (`BUILTIN_ENGINE`) is the only shipping engine. An LLM job naming `provider="openai_compat"`
  is rejected as `invalid_request` by `validate_llm_provider_name`.
- **FR-3** (must): The config surface is purged. `Config` (`config.py`) loses the fields
  `cloud_model`, `provider_api_key`, `max_cloud_upload_mb`, `llm_provider`, `llm_base_url`,
  `llm_api_key`, together with: their `TRANSCRIBER_*` env pickups; the `OPENAI_API_KEY` /
  `GROQ_API_KEY` fallbacks; their coercion/normalization branches in `load_config`; and their
  rows in `Config.public()` (so `/health` and startup logs no longer mention them).
  `_SECRET_KEYS` shrinks to `{"token", "hf_token"}`. `jobs.py` resolves the LLM engine as
  `provider or BUILTIN_ENGINE` where it previously read `config.llm_provider`
  (`jobs.py` line ~296).
- **FR-4** (must): Installed configs keep loading. A `config.json` that still contains any
  removed key loads without error — the removed keys fall into `load_config`'s existing
  unknown-key-ignored path. (A leftover `"provider": "cloud"` value loads too; it then fails
  per-job with the FR-1 `invalid_request`, not a crash.)
- **FR-5** (must): The dependency is gone. `litellm>=1.60` is removed from
  `services/transcription/pyproject.toml` `[project.dependencies]`; `uv.lock` is regenerated
  (`scripts/verify_locks.py --check` in `make lint` must pass); the project `description`
  no longer claims "litellm cloud providers". After this, `litellm`, `openai`, and `groq`
  strings appear nowhere in `src/transcription/` — the attribution isolation test
  (`tests/test_attribution.py::test_provider_libraries_are_confined_to_the_provider_seam`)
  still passes, and its prose/comments that justify litellm's presence are updated.
- **FR-6** (must): Tests follow the code. `tests/test_provider_cloud.py` is deleted;
  cloud/openai_compat cases are removed or updated in `tests/test_llm_units.py` (the
  `validate_llm_provider_name("openai_compat")` expectation), `tests/test_config.py`,
  `tests/test_cli.py`, `tests/test_provider_registry.py`, `tests/test_ledger.py`,
  `tests/test_jobs.py`, `tests/test_transcript.py`, and `tests/fakes.py`. New/updated tests
  assert the FR-1/FR-2 rejections and the FR-4 unknown-key tolerance.
- **FR-7** (should): Living docs match reality. `services/transcription/README.md` (config
  table rows for the removed keys, the cloud-cost prose at ~lines 239–241, the intro),
  `docs/config-contract.md` (the `llm_*` key example list at ~line 72),
  `services/transcription/src/transcription/__init__.py` docstring ("or a cloud" wording),
  and the `litellm.exe` example in `scripts/build_pyenv.py`'s comment (~line 237) are updated.
  Historical documents (`specs/*` for shipped features, `IDEA.md`, `vexa/`) are not touched.
- **FR-8** (should): The desktop app is verified unaffected: no Rust/TS change is required
  (no cloud/LLM-endpoint settings surface exists); `cargo test --workspace` and the Vitest
  suite pass unchanged; the ledger/transcript `provider` display fields keep working
  (they will only ever show `"local"` / `"none"`).

## Non-functional requirements

- **NFR-1**: The full QA gate passes: `make format`, `make lint`, `make type`
  (mypy `--strict` on `src`), `make test` — all three languages.
- **NFR-2**: Import-time isolation is preserved: importing `transcription.providers` or
  `transcription.llm` imports no engine library (the existing lazy-registry contract);
  service startup and the ready-line behavior are unchanged.
- **NFR-3**: No secrets regression: `errors.py::redact` and its `_SECRET_PATTERNS` stay
  active (they still guard `token`, `hf_token`, and Bearer headers); `load_config` still
  refuses `token`/`hf_token` via argv-shaped overrides.

## Acceptance criteria

- **FR-1**:
  - [ ] `services/transcription/src/transcription/providers/litellm_cloud.py` does not exist.
  - [ ] `known_provider_names() == {"local"}`.
  - [ ] `POST /v1/jobs` with `{"provider": "cloud", ...}` returns the `invalid_request`
        error shape naming known providers; no job row is created.
- **FR-2**:
  - [ ] `services/transcription/src/transcription/llm/openai_compat.py` does not exist.
  - [ ] `known_llm_provider_names() == {"llama_cpp"}` (plus any test-registered fakes only
        within a test's own scope).
  - [ ] An LLM job (`summarize`/`action_items`/`facts`) with `provider="openai_compat"` is
        rejected as `invalid_request`.
- **FR-3**:
  - [ ] `Config` has none of: `cloud_model`, `provider_api_key`, `max_cloud_upload_mb`,
        `llm_provider`, `llm_base_url`, `llm_api_key`; `grep` over `src/transcription/`
        finds none of these identifiers.
  - [ ] Setting `OPENAI_API_KEY`/`GROQ_API_KEY`/`TRANSCRIBER_PROVIDER_API_KEY`/
        `TRANSCRIBER_LLM_API_KEY`/`TRANSCRIBER_LLM_BASE_URL`/`TRANSCRIBER_LLM_PROVIDER` in
        the environment has no effect on the loaded `Config`.
  - [ ] `GET /health` / `Config.public()` output contains no removed key.
  - [ ] LLM jobs run through `BUILTIN_ENGINE` with no config-file engine selector.
- **FR-4**:
  - [ ] `load_config` over a config file containing all seven removed keys returns a valid
        `Config` (values ignored), covered by a test.
- **FR-5**:
  - [ ] `litellm` absent from `pyproject.toml` and from the regenerated `uv.lock`;
        `make lint` (including `verify_locks.py --check`) passes.
  - [ ] `test_provider_libraries_are_confined_to_the_provider_seam` passes with
        `litellm`/`openai`/`groq` appearing in zero files under `src/transcription/`.
- **FR-6**:
  - [ ] `tests/test_provider_cloud.py` does not exist; `uv run --directory
        services/transcription pytest -q` passes with no skipped remnants referencing cloud.
- **FR-7**:
  - [ ] `grep -i "cloud\|litellm"` over `services/transcription/README.md` and
        `docs/config-contract.md` returns no hit describing a current capability.
- **FR-8**:
  - [ ] `cargo test --workspace` and `npm --prefix apps/desktop run test` pass with no
        source change under `apps/desktop` or `crates/` (or only comment-level changes).

## Out of scope

- The error taxonomy (`ErrorKind.PROVIDER_AUTH`, `PROVIDER_PAYMENT_REQUIRED`,
  `PROVIDER_RATE_LIMITED`, `PROVIDER_UNAVAILABLE`) and the CLI exit-code map stay — they are
  a public contract consumed by the desktop app (`apps/desktop/src-tauri/src/service/fake.rs`
  asserts on `provider_unavailable`). Dead private helpers (e.g. `classify_http_status` if it
  loses its last caller) may be pruned as an implementation detail.
- The `cost_usd` column/field (ledger, `JobStatus`, CLI output, desktop `LedgerRow`) stays;
  local jobs already report `null`. No DB schema migration.
- The `provider` seam itself stays: the `Config.provider` key, CLI `--provider`,
  `JobCreate.provider`, `Health.provider`, the ledger `provider` column, and the lazy registry
  with its `register()` test hook. Only the `"cloud"` registration is removed.
- No desktop UI work; no settings page changes (nothing exists to remove).
- The `vexa/` reference tree, historical `specs/`, and `IDEA.md` are untouched.
- Features F2–F9 of this batch.

## Applicable toolkits

- `testing-toolkit:python-testing-patterns` — Python service test work (pytest in
  `services/transcription` dev dependency group; `make test` runs it).
- `frontend-toolkit:internal-ui` — webview UI layer (React + Vite in
  `apps/desktop/package.json`; operator-facing internal tool). Note: this feature expects no
  UI changes, so it should not attach to any task here unless one appears.
- `frontend-toolkit:ui-ux-pro-max` — same signal as above, same caveat.
- `devops-toolkit:devops-rollout-plan` — packaging/installer layer
  (`apps/desktop/src-tauri/tauri.conf.json` bundle config; the installer bakes the Python
  env whose dependency set this feature shrinks).

(No E2E row: no Playwright/Cypress in the repo — `apps/desktop` tests with Vitest. No
Docker/Postgres signals.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (from the `web`
  profile). This feature plans no UI tasks; the requirement binds only if one is created.

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — scope was fixed by the intake's binding decisions; everything else was resolvable
from the codebase (see Decisions log).

## Decisions log

- 2026-08-24 — How far does "remove cloud" go? → (AUTO: intake Decisions log, grounded in
  project memory "local-only direction — no cloud LLM or STT, ever") Remove BOTH the cloud
  LLM client (`llm/openai_compat.py`) and the cloud STT provider
  (`providers/litellm_cloud.py`) plus all related config keys, secrets handling, tests,
  deps, and docs.
- 2026-08-24 — `openai_compat` is usually pointed at *local* servers (LM Studio, Ollama) —
  keep it as a local-server option? → (AUTO: binding intake/operator decision explicitly
  lists `llm_provider`, `llm_base_url`, `llm_api_key`, and `openai_compat.py` for removal)
  No. The built-in llama.cpp engine is the only LLM runtime; it is also the only reason
  litellm can be dropped from the dependency tree.
- 2026-08-24 — Remove the `provider` seam entirely or only the `"cloud"` option? → (AUTO:
  intake decision phrases it as `provider=cloud`, i.e. the value; and the key threads
  through the public API — `JobCreate.provider`, `Health.provider`, the ledger column, and
  the desktop's `LedgerRow`/transcript display) Keep the key and registry seam; register
  `"local"` only. A leftover `provider: "cloud"` in an installed config fails per-job with
  the existing explicit `invalid_request`, never silently re-routes.
- 2026-08-24 — Remove now-unproducible error kinds and `cost_usd`? → (AUTO: codebase — the
  desktop asserts on taxonomy strings and consumes `cost_usd`; removing them is a
  cross-language contract break with zero user value) Keep both; prune only dead private
  helpers.
