---
slug: artifact-language-follows-transcript
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Facts, action items and summaries follow the transcript language

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 1 | 0 | 0 |

The diff implements the spec fully. All five per-meeting prompt builders take a keyword-only `language` parameter, normalize case/whitespace, and emit the hard "Write your entire answer in Russian/English." directive for {ru, en} while falling back to the verbatim soft `_LANGUAGE_RULE` for missing/null/non-string/out-of-set values (FR-1, FR-3). Both job paths read `data.get("language")` from the document `_load_transcript_lines` already returns and thread it into every call — every extraction chunk via `functools.partial`, and the single-chunk, every map, and the reduce call via `summarize_chunks` (FR-2). The repair retry replays the original pinned system message (FR-4). Test coverage is exhaustive at both the builder level (`test_llm_prompts.py`, new) and the job level via `FakeLlm.calls` (`test_llm_jobs.py`), fully model-free (FR-5, NFR-1). `report_messages` is untouched and guarded by a test so it stays out of scope. All Python QA legs pass: `pytest -q` (all green, 2 pre-existing GPU skips), `mypy src` (44 files, no issues), `ruff check .`, `ruff format --check .` (no diff). The change is confined to `services/transcription` with no new dependencies (NFR-2).

## Findings

### E1 [minor] [improvement] [status: open]

- **Where**: `services/transcription/src/transcription/llm/prompts.py:30`
- **Spec ref**: FR-3 (non-string `language` values from hand-edited transcripts)
- **Expected**: The declared type of `_language_rule` should match its documented and tested contract, which explicitly accepts non-strings (docstring: "``None``, a non-string and any other code fall back…"; tests pass `42`, `["ru"]`, `{"code": "ru"}`).
- **Actual**: Annotated `language: str | None`, while the `isinstance(language, str)` guard exists precisely because callers thread `data.get("language")` (i.e. `Any`) from untrusted JSON. A future strict-typing cleanup could "simplify away" the guard on the strength of the annotation.
- **Suggested fix**: Annotate the parameter as `object` (or `Any`) on `_language_rule` — the five public builders can keep `str | None` as the intended surface.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 hard directive per builder, soft wording removed | `llm/prompts.py:30-45` (`_language_rule`), builders at 79, 103, 127, 166, 198 | `test_llm_prompts.py::test_supported_language_pins_the_output_language` (5 builders x ru/en) | ✓ |
| FR-1 case/whitespace variants | `_language_rule` (`strip().lower()`) | `test_llm_prompts.py::test_language_code_is_normalized` (`RU`, `En`, `  ru `, `EN`) | ✓ |
| FR-1 terms clause retained in both modes | `_TERMS_RULE` appended in both branches | `test_llm_prompts.py::test_technical_terms_clause_survives_in_both_modes` | ✓ |
| FR-2 extraction jobs thread language per chunk | `jobs.py:850-866` (`functools.partial(..., language=language)`) | `test_llm_jobs.py::test_facts_pin_russian_for_every_chunk_of_a_long_transcript` (multi-chunk, asserts >1 call), `::test_action_items_pin_english_when_the_transcript_says_en` | ✓ |
| FR-2 summarize threads language through map and reduce | `jobs.py:771-796`, `llm/summarize.py:21-39` | `test_llm_jobs.py::test_a_single_chunk_summary_is_pinned_to_the_transcript_language`, `::test_every_map_call_and_the_reduce_call_are_pinned` (distinguishes map vs reduce prompts) | ✓ |
| FR-2 `prompts.py` purity (no package imports, no I/O) | `prompts.py` imports only `__future__`, `collections.abc`, `typing` | `test_llm_prompts.py::test_prompts_module_imports_nothing_from_the_package_and_does_no_io` (AST-based) | ✓ |
| FR-3 fallback for missing/null/unsupported/non-string | `_language_rule` isinstance + dict-lookup guard | `test_llm_prompts.py::test_unsupported_language_falls_back_to_the_soft_rule` (None, "de", "", "ru-RU", 42, list, dict); `test_llm_jobs.py::test_a_transcript_without_a_language_key_keeps_the_soft_rule`, `::test_a_null_or_unsupported_language_keeps_the_soft_rule[None|de]` (jobs succeed) | ✓ |
| FR-4 repair replays pinned system message | `prompts.py:221-234` (`repair_messages` prepends `*original`) | `test_llm_prompts.py::test_repair_replays_the_pinned_system_message_verbatim`; `test_llm_jobs.py::test_the_repair_call_replays_the_pinned_system_message` (end-to-end via `FakeLlm.calls`) | ✓ |
| FR-5 all criteria as tests, suite green | — | `uv run --directory services/transcription pytest -q`: all pass (2 pre-existing GPU-marked skips); `mypy src`, `ruff check`, `ruff format --check` clean | ✓ |
| NFR-1 model-free verification | — | All new tests are string assertions / `FakeLlm`-driven | ✓ |
| NFR-2 no new deps, confined to service | diff touches only `services/transcription` (+ plan checkboxes); `pyproject.toml` unchanged | — | ✓ |

Out-of-scope guard: `report_messages` keeps the soft rule and gained no parameter — verified by `test_llm_prompts.py::test_report_messages_keeps_the_soft_rule`; `llm/report.py`'s positional `chunk_summary_messages(chunk, i, len(chunks))` call still typechecks because the new parameter is keyword-only with a default (also asserted by `test_language_is_a_keyword_parameter_with_a_default`).

## Positive notes

- The `functools.partial` binding in `_extract_sync` keeps the local `Callable[[str], list[Message]]` annotation and every `messages_fn(chunk)` call site untouched — exactly the low-ripple option the plan proposed, and mypy stays green without widening any signatures.
- Test design respects the fixture contract: `_meeting_with_language` is a sibling helper, so the legacy language-less `_transcript_doc()` shape every existing test depends on is untouched, and it doubles as the FR-3 legacy-shape proof.
- The multi-chunk tests assert `len(prompts) > 1` / "exactly one reduce call" before asserting pinning, so they cannot silently pass on an accidentally single-chunk transcript — the reduce call (the easy one to miss per the plan) is explicitly separated by prompt fingerprint.
- The purity test is AST-based rather than a brittle string grep, and also asserts the keyword-only signature that protects the out-of-scope `report.py` caller.
- `repair[: len(original)] == original` in the job-level FR-4 test proves verbatim replay, not just substring presence.
