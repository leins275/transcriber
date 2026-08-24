---
slug: remove-cloud-llm-support
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Remove cloud LLM and cloud STT support (local-only)

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 2 | 0 | 0 |

The diff implements the spec completely. Both cloud modules (`providers/litellm_cloud.py`,
`llm/openai_compat.py`) and `tests/test_provider_cloud.py` are deleted; both registries ship
exactly one entry; all six removed config fields (plus their env pickups, `OPENAI_API_KEY`/
`GROQ_API_KEY` fallbacks, coercion branches, and `public()` rows) are gone; `_SECRET_KEYS`
is `{"token", "hf_token"}`; `jobs.py` resolves `provider or BUILTIN_ENGINE` in both the
factory and per-job paths and drops `llm_provider` from the health payload; `litellm` is out
of `pyproject.toml` and the lock diff removes only litellm and its orphaned transitives
(openai, tiktoken, jiter, distro, ...) with nothing added. Every acceptance sweep was
re-executed independently: pytest (exit 0), ruff check + format --check, mypy --strict,
`verify_locks.py --check`, `sync_version.py --check`, cargo clippy `-D warnings`, cargo test
--workspace (0 failures), eslint, tsc, vitest (268 passed), scripts tests (161 passed).
`git status`/diff confirm zero changes under `apps/desktop/` or `crates/` (FR-8). Two minor
prose remnants remain; neither affects behavior or trips any gate.

## Findings

### E1 [minor] [spec-drift] [status: open]

- **Where**: services/transcription/tests/test_api_contract.py:107
- **Spec ref**: FR-6 ("no skipped remnants referencing cloud" — read broadly as test-suite hygiene)
- **Expected**: Test prose no longer names litellm as a library the service could pay an import for.
- **Actual**: `test_health_never_imports_a_provider_library` docstring still says "`/health` must never pay for a `faster_whisper`/`litellm` import" — litellm is no longer installed or importable, so the docstring describes an impossible hazard.
- **Suggested fix**: Drop "`/litellm`" from the docstring (comment-only change).

### E2 [minor] [spec-drift] [status: open]

- **Where**: services/transcription/src/transcription/llm/base.py:47; services/transcription/src/transcription/llm/shapes.py:4
- **Spec ref**: FR-5 / FR-7 (docs match reality)
- **Expected**: Docstrings describe only the shipping engine's structured-output mechanism (llama.cpp grammar compilation).
- **Actual**: Both docstrings still offer "(OpenAI protocol: `response_format`)" as an alternative constraint mode — that mode belonged to the deleted `openai_compat` engine. Note: capital-case "OpenAI" does not trip the case-sensitive `test_provider_libraries_are_confined_to_the_provider_seam` guard, and both files sit inside the exempt `llm/` seam anyway, so no gate catches this.
- **Suggested fix**: Reword both parentheticals to name only the grammar-compiled llama.cpp path.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (cloud STT provider gone, per-job rejection) | `providers/litellm_cloud.py` deleted; `providers/__init__.py:20-22` (registry = `local` only) | `test_provider_registry.py::test_known_provider_names_is_local_only_without_importing_the_model_library`, `::test_validate_provider_name_rejects_cloud_naming_the_known_providers`; `test_api_contract.py::test_post_job_with_the_removed_cloud_provider_is_rejected_and_creates_no_ledger_row` | ✓ |
| FR-2 (openai_compat engine gone, rejection) | `llm/openai_compat.py` deleted; `llm/__init__.py:23-25` | `test_llm_units.py::test_the_builtin_llama_cpp_engine_is_the_only_registered_engine`, `::test_the_external_openai_compatible_engine_is_rejected`; `test_api_contract.py::test_llm_job_naming_the_removed_openai_compatible_engine_is_rejected` | ✓ |
| FR-3 (config surface purged; jobs.py uses BUILTIN_ENGINE) | `config.py` (fields, `_SECRET_KEYS:23`, `public():140-152`, env/coercion branches deleted); `jobs.py:133,213-218,288-293` | `test_config.py::test_config_has_no_cloud_fields`, `::test_cloud_env_variables_have_no_effect_on_the_loaded_config`, `::test_public_reports_no_removed_cloud_keys`; `test_jobs.py::test_llm_job_without_a_provider_resolves_to_the_builtin_engine`, `::test_the_default_llm_factory_asks_the_registry_for_the_builtin_engine`, `::test_llm_info_reports_the_builtin_gguf_presence_and_no_engine_selector`; `test_api_contract.py::test_health_returns_ok_...` (exact health dict without `llm_provider`) | ✓ |
| FR-4 (installed configs keep loading) | `load_config` unknown-key-ignored path (unchanged) | `test_config.py::test_installed_config_file_with_removed_cloud_keys_still_loads` (all seven keys, incl. `provider: "cloud"` value) | ✓ |
| FR-5 (litellm dependency gone) | `pyproject.toml` (dep + description), `uv.lock` (litellm/openai + 13 orphaned transitives removed, nothing added) | `test_attribution.py::test_provider_libraries_are_confined_to_the_provider_seam`; `verify_locks.py --check` passes; case-sensitive grep for `litellm|openai|groq` over `src/transcription/` = zero hits | ✓ |
| FR-6 (tests follow the code) | `tests/test_provider_cloud.py` deleted; 12 test files updated | pytest exit 0; no skips referencing cloud; remaining `cloud`/`openai_compat` strings appear only in the FR-1/FR-2/FR-4 rejection/tolerance tests | ✓ (E1 minor) |
| FR-7 (living docs) | `README.md`, `docs/config-contract.md`, `__init__.py` docstring, `build_pyenv.py:237` | `grep -i "cloud\|litellm"` over README + config-contract: zero hits of any kind | ✓ (E2 minor, docstrings outside the FR-7 file list) |
| FR-8 (desktop unaffected) | no file changed under `apps/desktop/` or `crates/` (git diff/status verified) | cargo test --workspace: all green; vitest: 268 passed; clippy/eslint/tsc clean | ✓ |
| NFR-1 (full QA gate) | — | every component of `make format/lint/type/test` run individually, all pass | ✓ |
| NFR-2 (import-time isolation) | lazy registries unchanged | `test_llm_units.py::test_importing_the_llm_package_never_imports_an_llm_library`; `test_provider_registry.py::test_importing_providers_package_does_not_import_provider_libraries`; `test_api_contract.py::test_health_never_imports_a_provider_library` | ✓ |
| NFR-3 (no secrets regression) | `errors.py::redact`/`_SECRET_PATTERNS` intact, actively called from `cli.py` (5 call sites) | `test_config.py::test_credentials_in_overrides_raise_config_error` (token + hf_token), `::test_public_contains_no_secrets` (retargeted to hf_token); redaction tests kept in `test_errors.py` | ✓ |

## Positive notes

- **Order-independent registry assertions**: `test_provider_registry.py::_shipping_registry()` re-executes `providers/__init__.py` in a throwaway namespace so `known_provider_names() == {"local"}` holds regardless of which test modules registered fakes first, and the api-contract rejection tests `monkeypatch.delitem(..., "cloud", raising=False)` for the same reason. This is exactly the right answer to the process-global registry; do not "simplify" it back to asserting on the live module.
- **Ordering discipline held**: `jobs.py` was moved to `BUILTIN_ENGINE` independently of the config-field deletion (plan T4 before T3), and the `_stray_engine_selector` tests prove a leftover `llm_provider` value on a loaded config is ignored entirely — stronger than the spec strictly required.
- **`classify_http_status` pruned with a tombstone test** (`test_no_http_status_classifier_ships`) while the full `ErrorKind` taxonomy and `redact` survive, matching the out-of-scope contract exactly; `redact` remains live via `cli.py`, not dead code.
- **Lock regeneration was surgical**: the `uv.lock` diff removes only litellm and its now-orphaned transitives; no unrelated re-pins.
- **`_PROVIDER_LIBRARY_STRINGS` keeps `litellm`/`openai`/`groq` as a standing guard** against reintroducing a remote model client, with a comment explaining why — good future-proofing of the local-only invariant.
