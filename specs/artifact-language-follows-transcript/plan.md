---
slug: artifact-language-follows-transcript
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Facts, action items and summaries follow the transcript language

## Architecture overview

Backend-only change inside `services/transcription`. Three layers, one value flowing down:

```
transcript.json ("language": "ru" | "en" | null | absent | other)
        |
        v
jobs.py  _load_transcript_lines()  -- already returns (lines, data); nothing new here
        |            data["language"] read by the two callers
        |
        +-- _summarize_sync ----> llm/summarize.py summarize_chunks(chunks, complete, language)
        |                              |-- summary_messages(text, language=...)         (1 chunk)
        |                              |-- chunk_summary_messages(text, i, n, language=...)  (map)
        |                              `-- merge_summaries_messages(parts, language=...)     (reduce)
        |
        `-- _extract_sync ------> action_items_messages(chunk, language=...) /
                                  facts_messages(chunk, language=...)   (every chunk)
                                       |
                                  repair_messages(original, ...)  -- replays the original
                                  system message verbatim, so pinning survives the retry
```

- **`llm/prompts.py`** (`services/transcription/src/transcription/llm/prompts.py`): each of the five per-meeting builders gains a keyword parameter `language: str | None = None`. A private helper (e.g. `_language_rule(language)`) normalizes the value (`str.strip().lower()` when it is a string) and returns either a hard directive — "Write your entire answer in Russian." / "…in English." — or, for `None`/non-string/out-of-set codes, today's soft `_LANGUAGE_RULE` verbatim. Both variants carry the existing "Keep technical terms, product names and code identifiers as they appear." clause. `report_messages` is untouched (F7 deletes it), and because the new parameter is keyword-with-default, `llm/report.py`'s existing positional call to `chunk_summary_messages(chunk, i, len(chunks))` at line 111 keeps compiling with today's behavior. The module stays pure string assembly — no I/O, no intra-package imports (its docstring contract, FR-2).
- **`llm/summarize.py`**: `summarize_chunks` gains `language: str | None = None` and forwards it to all three builders it calls (lines 30/33/36). Still pure control flow.
- **`jobs.py`**: `_summarize_sync` (line 771) stops discarding the document (`lines, data = ...`), reads `data.get("language")`, passes it to `summarize_chunks`. `_extract_sync` (line 847) already keeps `data`; it reads the same value and binds it into every `messages_fn(chunk)` call (via a closure/`functools.partial`, or by widening the local `Callable` annotation). `_constrained_items`' repair path needs no change — `repair_messages` already replays the original (now pinned) system message (FR-4).

No schema, API, config or UI changes. No new dependencies (NFR-2).

## Risks

- **Non-string `language` values in legacy transcripts** (e.g. `null`, or junk from hand-edited files). Mitigated: the normalization helper in `prompts.py` treats anything that is not a case-variant of `"ru"`/`"en"` — including non-strings — as "unpinned" and falls back to the soft rule; T1 tests this at the builder level, T2/T3 at the job level. The job can never fail on this field (FR-3).
- **Signature ripple**: `_extract_sync` types `messages_fn` as `Callable[[str], list[Message]]`; threading a second value must keep `mypy src` green. Mitigated in T2 by binding the language in a closure/partial so the local callable stays single-argument, or by updating the annotation — either way `make type` gates it.
- **Test-fixture coupling**: `test_llm_jobs.py`'s `_transcript_doc()` deliberately has no `language` key (the legacy shape). New pinning tests must build ru/en variants without mutating the shared fixture, or every existing test's prompt expectations shift. T2/T3 add a parameterized doc builder instead of editing the fixture's default shape.
- **Sibling-feature overlap** (batch worktrees): F7 will delete `report_messages`; this plan never touches it, so the merge stays clean. F2 makes `language` trustworthy but this plan is written against the current tree — the fallback path is what makes that safe.

## Waves

Strictly linear: T2 and T3 both edit `jobs.py` and `test_llm_jobs.py`, so they cannot share a wave; everything downstream of T1 is serialized by file overlap anyway.

| Wave | Tasks |
|---|---|
| 1 | T1 |
| 2 | T2 |
| 3 | T3 |
| 4 | T4 |

## Tasks

### [x] T1: Language-aware prompt builders in prompts.py  [deps: —]

- **Files**: `services/transcription/src/transcription/llm/prompts.py`, `services/transcription/tests/test_llm_prompts.py` (new)
- **Test first**: `services/transcription/tests/test_llm_prompts.py` — cases:
  - For each of the five builders (`summary_messages`, `chunk_summary_messages`, `merge_summaries_messages`, `action_items_messages`, `facts_messages`) with `language="ru"`: the system message contains an explicit "in Russian" directive and does NOT contain "same language the transcript is written in" (FR-1).
  - Same five with `language="en"`: explicit "in English" directive, no soft wording (FR-1).
  - Case variants `"RU"`, `"En"`, `"  ru "` pin identically to lowercase (FR-1).
  - In both pinned and fallback modes the system message contains the "technical terms, product names and code identifiers" clause (FR-1).
  - `language=None`, omitted entirely, `"de"`, and a non-string value each yield today's soft `_LANGUAGE_RULE` wording unchanged, no exception raised (FR-3).
  - `repair_messages(original, raw, error)` on a `"ru"`-pinned `facts_messages` original: the returned list begins with the original pinned system message verbatim (FR-4).
  - Purity guard: the `transcription.llm.prompts` module's imports include nothing from `transcription.*` and no I/O modules (`os`, `pathlib`, `json`, ...) — assert on the module's namespace/source (FR-2 grep-level criterion).
- **Implement**: Add `_language_rule(language: str | None) -> str` mapping normalized `{"ru": "Russian", "en": "English"}` to a hard "Write your entire answer in <Language>." directive plus the retained technical-terms clause; everything else returns the existing `_LANGUAGE_RULE` constant. Add `language: str | None = None` keyword param to the five builders and swap the inline `_LANGUAGE_RULE` concatenations for the helper. Leave `report_messages` on the soft rule (out of scope, F7 deletes it). Update the module docstring's language sentence.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: new tests pass and the rest of the suite is untouched (`uv run --directory services/transcription pytest -q`); `uv run --directory services/transcription mypy src` and `ruff check .` pass; `llm/report.py`'s positional `chunk_summary_messages` call still typechecks unmodified.

### [x] T2: Thread language through the extraction jobs  [deps: T1]

- **Files**: `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` — cases (all asserted on `FakeLlm.calls`, no model — NFR-1; add a language-aware variant of the `_transcript_doc()` helper rather than changing its default legacy shape):
  - Facts job on a multi-chunk transcript (reuse the 200-segment pattern at lines 190–196) with `"language": "ru"`: every recorded call's system message carries the Russian directive — every chunk, not just the first (FR-2).
  - Action-items job with `"language": "en"`: English-pinned prompts (FR-1/FR-2).
  - Facts job on the existing language-less `meeting_dir` fixture: job succeeds and prompts contain the soft mirror rule, not a pinned directive (FR-3).
  - Same fallback for `"language": null` and `"language": "de"` (FR-3).
  - Repair path (extend the pattern of `test_extraction_repairs_invalid_json_once`, line 353) on a `"ru"` transcript: `llm.calls[1]` (the repair call) begins with the original Russian-pinned system message (FR-4).
- **Implement**: In `_extract_sync` (line 847), read `language = data.get("language")` from the document `_load_transcript_lines` already returns, and bind it into the per-chunk prompt build — e.g. wrap `action_items_messages`/`facts_messages` in a closure or `functools.partial(..., language=language)` so the `messages_fn(chunk)` call sites and the local `Callable[[str], list[Message]]` annotation stay intact. No behavior change anywhere else in the job.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: all new and existing tests pass (`uv run --directory services/transcription pytest -q`); `mypy src` and `ruff check .` clean.

### [x] T3: Thread language through the summarize path  [deps: T1, T2]

- **Files**: `services/transcription/src/transcription/llm/summarize.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` — cases (on `FakeLlm.calls`):
  - Summarize job on a single-chunk `"language": "ru"` transcript: the one recorded call is Russian-pinned (FR-2).
  - Summarize job on a multi-chunk `"language": "ru"` transcript: every map call AND the final reduce call are Russian-pinned (FR-2 — the reduce call is the easy one to miss).
  - Summarize job on the language-less legacy fixture: succeeds with the soft mirror rule in every prompt (FR-3).
- **Implement**: `summarize_chunks(chunks, complete, language: str | None = None)` in `llm/summarize.py` forwards `language` to `summary_messages`, `chunk_summary_messages` and `merge_summaries_messages` (lines 30/33/36). In `jobs.py` `_summarize_sync` (line 771), change `lines, _ = ...` to `lines, data = ...`, read `data.get("language")`, and pass it to `summarize_chunks` at line 792.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: all new and existing tests pass (`uv run --directory services/transcription pytest -q`); `mypy src` and `ruff check .` clean.

### [x] T4: Full-service verification sweep  [deps: T2, T3]

- **Files**: fix-ups only, confined to `services/transcription/src/transcription/llm/prompts.py`, `services/transcription/src/transcription/llm/summarize.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_prompts.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: no new tests — this task executes the full acceptance sweep: `uv run --directory services/transcription pytest -q` (the FR-5 gate, covering every FR-1..FR-4 criterion added in T1–T3) and re-checks the FR-2 grep-level criterion (`prompts.py` has no `transcription.*` imports and no I/O).
- **Implement**: Run the Python legs of the repo QA targets exactly as the Makefile defines them: `ruff format .`, `ruff check .`, `mypy src`, `pytest -q` (all via `uv run --directory services/transcription`). Per the `cli` profile's verification discipline, also exercise the public surface once as a consumer would: a scratch-script invocation of `facts_messages(..., language="ru")` / `summarize_chunks` confirming the pinned system text (scratch file in the session scratchpad, not committed). Fix any residual lint/type/format fallout inside the declared file set only.
- **Skills**: testing-toolkit:python-testing-patterns
- **Done when**: `uv run --directory services/transcription pytest -q`, `mypy src`, `ruff check .` and `ruff format .` (no diff) all pass; every acceptance checkbox in the spec maps to a green test; no files outside the declared set changed.

## QA expectations

All four repo targets exist: `make format`, `make lint`, `make type`, `make test` — each fans out to Rust (cargo), frontend (npm) and Python (uv) legs. This feature only moves the Python legs; implementers should run those directly to avoid paying for the cargo/npm legs in a backend-only worktree:

- `uv run --directory services/transcription ruff format .`
- `uv run --directory services/transcription ruff check .`
- `uv run --directory services/transcription mypy src`
- `uv run --directory services/transcription pytest -q`

Nothing known-flaky in the Python suite; `test_llm_jobs.py` is fully fake-driven (no model, no network — NFR-1). The full `make test` additionally runs `uv run --with pytest -- pytest scripts/tests -q`, which this feature does not touch.
