---
slug: artifact-language-follows-transcript
created: 2026-08-24
status: approved
---

# Spec: Facts, action items and summaries follow the transcript language

## Summary

The LLM job types (facts, action items, summarize) currently carry only a soft "answer in the transcript's language" instruction, which the model ignores — an English meeting can come back with Russian facts, or vice versa. This feature pins the output language explicitly: the `language` field that `transcript.json` already records ("ru" or "en", made trustworthy by sibling feature F2) is threaded into every prompt as a hard directive naming the output language. Backend-only; no UI changes.

## Problem & context

- `services/transcription/src/transcription/llm/prompts.py` lines 18–21 define `_LANGUAGE_RULE`: *"Write your answer in the same language the transcript is written in."* It is appended to the system prompt of every LLM job (summary, chunk-summary map, merge-summaries reduce, action items, facts, report). It is advisory — local models routinely disregard it, which is the operator's complaint.
- The correct language is already known before any prompt is built: `transcript.json` has a top-level `language` field (`transcription/schema.py` `TranscriptDoc`, written by `transcription/transcript.py` `build_document`). `jobs.py` `_load_transcript_lines` (line 723) returns the full transcript document; `_extract_sync` (line 847) already keeps it as `data`, while `_summarize_sync` (line 771) currently discards it (`lines, _ = ...`). Nothing reads `data["language"]` today.
- Sibling feature F2 (`transcript-language-selection`, ships before this one in the F1→F9 order) constrains transcription to ru/en, so for new transcripts the field holds a trustworthy "ru" or "en". Legacy transcripts may have `null` or another autodetected code — the design must degrade gracefully for them.
- Downstream is already Unicode-safe: `artifacts.py` `slugify` explicitly passes Cyrillic titles through (docstring, lines 72–84), and `transcript.py` writes UTF-8 with `ensure_ascii=False`. No filename or encoding work is needed when Russian output becomes reliable.
- Test infrastructure exists: `services/transcription/tests/fakes.py` `FakeLlm` records every messages list it receives (`self.calls`, line 186/211), so language pinning is assertable at the job level without a real model; `services/transcription/tests/test_llm_jobs.py` drives `JobManager` end-to-end with it. Note its `_transcript_doc()` fixture (line 58) currently writes no `language` key — exactly the legacy shape the fallback must handle.

## Users

- The operator (single-user desktop app), running Facts, Action items or Summarize on a recorded meeting and reading the artifacts in the vault or external tools. They expect a Russian meeting to yield Russian artifacts and an English meeting English ones, without any new setting.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists (Tauri). Confirmed from the orchestrator's match.
- `web` — `apps/desktop/package.json` names `react` and `vite` (Tauri webview UI). Confirmed. Per the `desktop` profile, UI toolkits come from `web`; this feature, however, has no UI surface.
- `cli` — `[project.scripts]` in `services/transcription/pyproject.toml` (`transcription-service = "transcription.cli:main"`). The Python service this feature exclusively touches is a headless service + console entry point; the `cli` profile's verification discipline (observable contract, no UI skills on its tasks) is the one that governs here.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Backend service | Python 3.12+, FastAPI + Pydantic v2, uv-managed | `services/transcription/pyproject.toml` |
| LLM layer | Local llama.cpp provider; prompt builders are pure string assembly | `services/transcription/src/transcription/llm/prompts.py` module docstring |
| Desktop shell | Tauri (Rust) | `apps/desktop/src-tauri/tauri.conf.json` |
| Frontend | React + Vite (webview) — untouched by this feature | `apps/desktop/package.json` |
| Testing | pytest (service), cargo test, vitest via `npm run test` | `Makefile` `test` target; `services/transcription/tests/` |

Makefile QA targets present: format, lint, type, test (all four; each fans out to cargo / npm / uv-run tooling).

## Functional requirements

- **FR-1** (must): The prompt builders for all five per-meeting LLM calls — `summary_messages`, `chunk_summary_messages`, `merge_summaries_messages`, `action_items_messages`, `facts_messages` in `llm/prompts.py` — accept an explicit target-language parameter. When it is a supported code (`"ru"` or `"en"`, case-insensitively), the system prompt contains a hard directive naming the output language (e.g. "Write your entire answer in Russian." / "…in English."), replacing the soft mirror rule. The existing clause preserving technical terms, product names and code identifiers as they appear is retained in both modes.
- **FR-2** (must): `jobs.py` `_summarize_sync` and `_extract_sync` read the `language` value from the transcript document already returned by `_load_transcript_lines` and pass it to every prompt they build (including every map chunk and the reduce call in the summarize path). `prompts.py` stays free of filesystem access — the value is threaded in by the caller.
- **FR-3** (must): Graceful fallback. When `transcript.json` has no `language`, a null one, or a code outside {ru, en} (legacy pre-F2 transcripts), the prompts carry today's soft `_LANGUAGE_RULE` behavior unchanged, and the job never fails or warns because of the language field.
- **FR-4** (must): The bounded repair retry (`repair_messages`) preserves the pinned directive — it must keep replaying the original system message so the second attempt is still language-pinned.
- **FR-5** (must): Tests prove the pinning without a real model: prompt-level unit tests on the builders, plus job-level tests in `test_llm_jobs.py` asserting via `FakeLlm.calls` that a transcript with `"language": "ru"` produces prompts containing the Russian directive (and `"en"` the English one), and that the existing language-less fixture takes the fallback path.

## Non-functional requirements

- **NFR-1**: Verification is fully deterministic and model-free — every acceptance criterion is assertable on prompt strings via `FakeLlm`; no LLM inference in the test suite.
- **NFR-2**: No new dependencies; the change is confined to `services/transcription` (prompts, jobs, tests).

## Acceptance criteria

- **FR-1**:
  - [ ] With target language `"ru"`, each of the five builders' system messages contains an explicit "answer in Russian" directive and does not contain the soft "same language the transcript is written in" wording.
  - [ ] Same for `"en"` with English named explicitly.
  - [ ] In both modes the system message still instructs keeping technical terms, product names and code identifiers as they appear.
  - [ ] `"RU"` / `"En"` (case variants) pin the same as lowercase.
- **FR-2**:
  - [ ] A facts or action-items job on a meeting whose `transcript.json` has `"language": "ru"` sends only Russian-pinned prompts to the provider (asserted on `FakeLlm.calls`), for every chunk of a multi-chunk transcript.
  - [ ] A summarize job on the same meeting sends Russian-pinned prompts for the single-chunk call, each map call, and the reduce call.
  - [ ] `grep`-level check: `prompts.py` imports nothing from the rest of the package and performs no I/O (its documented module contract survives).
- **FR-3**:
  - [ ] A job on a transcript with no `language` key (the current `test_llm_jobs.py` fixture) completes successfully and its prompts contain the soft mirror rule, not a pinned directive.
  - [ ] Same for `"language": null` and for an out-of-set code such as `"de"`.
- **FR-4**:
  - [ ] When the first structured-output attempt is invalid, the repair call's message list (recorded by `FakeLlm`) still begins with the original language-pinned system message.
- **FR-5**:
  - [ ] All of the above are covered by tests under `services/transcription/tests/`; `make test` (Python leg: `uv run --directory services/transcription pytest -q`) passes.

## Out of scope

- `report_messages` / the project-essence report — F7's approved decision deletes the report job type and `llm/report.py` entirely; pinning its language would be dead work. It keeps the soft rule until F7 removes it.
- Changing how the transcript language is detected or constrained — that is F2 (`transcript-language-selection`), which this spec assumes has landed.
- Any UI: no language indicator, no per-job language override control.
- Re-generating or migrating existing artifacts written in the wrong language.
- Supporting pinned languages beyond ru/en (mirrors F2's two-option scope).
- Filename/slug handling for Cyrillic titles — already correct in `artifacts.py` `slugify`.

## Applicable toolkits

- `testing-toolkit:python-testing-patterns` — Python service tests; signal: pytest suite under `services/transcription/tests/` and the Makefile `test` target (from `desktop`, `web` and `cli` profiles' Tests rows).
- `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max` — repo-level match only (React internal-tool UI in `apps/desktop/package.json`, from `web`); this feature plans no UI tasks, so the architect should not attach them here.
- `devops-toolkit:devops-rollout-plan` — repo-level match only (Tauri bundle config in `apps/desktop/src-tauri/tauri.conf.json`, `desktop` Packaging row); irrelevant to this backend-only feature.

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (carried unchanged from the `web` profile). This feature contains no UI tasks, so it should not fire, but it binds any task that does touch the webview UI.

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — see Decisions log for the points resolved from the codebase and the batch's approved decisions.

## Decisions log

- 2026-08-24 — (AUTO: intake batch order + F2 spec) Trust `transcript.json`'s `language` only for the F2-constrained set {ru, en}; F2 ships first, so new transcripts always carry one of these.
- 2026-08-24 — (AUTO: codebase — legacy transcripts predate F2 and `test_llm_jobs.py`'s fixture has no `language` key) Missing/null/unsupported language falls back to today's soft `_LANGUAGE_RULE`, never fails the job.
- 2026-08-24 — (AUTO: intake decisions log, F7 "remove the report job type … entirely") `report_messages` is excluded from language pinning.
- 2026-08-24 — (AUTO: orchestrator batch context) Summarize output follows the same pinning rule as facts and action items — all three per-meeting job types behave identically.
- 2026-08-24 — (AUTO: `prompts.py` module docstring contract) The language value is threaded in from `jobs.py`; `prompts.py` remains pure string assembly with no filesystem access.
- 2026-08-24 — (AUTO: `artifacts.py` `slugify` docstring) Cyrillic artifact titles already produce valid Unicode folder slugs; no filename work in scope.
