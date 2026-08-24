---
slug: artifacts-in-sync-folder
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Store action items and facts under the recording's own folder

## Architecture overview

The artifact *location* is decided in exactly one place per side of the language boundary, which keeps this feature small:

- **Rust app (anchor decision)** — `apps/desktop/src-tauri/src/commands/llm.rs` `extract_vault_entry_handler` computes the job's `output_dir`. Today: `require_project_dir(root, meeting_dir)?.join(artifact_kind.dir_name())`. New: `meeting_dir.join(artifact_kind.dir_name())` — the same shape `export_recording_handler` already uses for `exports/`. `require_project_dir` (llm.rs line 170) loses its only caller and is deleted, which is what un-gates `unsorted/` meetings (FR-2). The Python sidecar never chooses the anchor; it writes into whatever `output_dir` it receives.
- **Rust vault crate (contract text)** — `crates/vault/src/paths.rs` keeps the exact strings `ACTION_ITEMS_DIR_NAME = "action items"` / `FACTS_DIR_NAME = "facts"`; only their doc comments move the anchor to "inside a meeting folder" (the `EXPORTS_DIR_NAME` pattern). `RESERVED_PROJECT_DIR_NAMES` is unchanged — it now documents *legacy* project-level exclusion (FR-5, Q1: legacy dirs stay on disk, unread). `list.rs` behavior is untouched; its reserved-name tests keep passing as-is. `artifacts.rs` / `lib.rs` doc headers are re-worded only (the `list_project_artifacts` reader itself is F7's demolition, FR-7).
- **Python sidecar (provenance + readers)** —
  - `jobs.py` `_extract_sync` (line ~895): `source_project` was `Path(job.output_path).parent.name`, which under the new layout would be the *meeting* name. New derivation: `meeting_dir.parent.name`, `None` when that name casefolds to `"unsorted"` (the exact idiom `_export_sync` line 1012 already uses) (FR-3).
  - `exporting.py`: `items_for_meeting(project_dir, kind, meeting_name)` + `source_meeting` filter is replaced by listing `meeting_dir / kind_dir_name` directly via the existing `list_items` — everything in a meeting-level kind dir belongs to that meeting by construction. `build_export_md` loses its `project_dir` parameter (FR-4).
  - `jobs.py` `_export_sync` (line ~1008): drops the `project_dir = None if unsorted` special case entirely; passes only `meeting_dir` (FR-4).
  - `artifacts.py`: code is already anchor-agnostic (`write_item`/`list_items` take a parent dir); only the module docstring's layout table changes, plus the NFR-1 `fit_slug` budget test gains a realistically deep *meeting-level* parent.
- **Webview** — `RecordingPage.tsx` (lines 214–239): drop the `!entry.project ||` term from both extraction buttons' `disabled` and delete the "File this recording under a project first" tooltips (FR-2).

Data flow after the change (filed and unsorted identical):

```
RecordingPage "Action items" → extract_vault_entry (llm.rs)
  → enqueue(kind, input=<meeting>, output=<meeting>/action items)
  → sidecar _extract_sync: LLM → write_item(<meeting>/action items, meta{source_project: parent-or-null, source_meeting})
Export PDF → _export_sync → build_export_md(meeting_dir) → list_items(<meeting>/<kind>) → ../../<kind>/<slug>/screenshot links
```

No schema, IPC-shape, or config changes. Legacy `<PROJECT>/<kind>/` trees: never read, never written, never deleted (Q1/FR-6).

## Risks

- **Cross-language contract drift** — the directory-name strings are pinned on both sides; T1 and T3 add/keep explicit contract tests so a future rename cannot half-land. FR-6's "nothing deleted" is asserted negatively in T2/T5 tests (no writes under `<PROJECT>/<kind>`, legacy items absent from exports).
- **`test_llm_jobs.py` is touched by two tasks** (extraction tests in T4, export tests in T5) — serialized via `deps: T4` on T5; their `Files` sets otherwise overlap on `jobs.py` too, so they can never co-run.
- **Sibling F7 deletes artifact browsing in `llm.rs` in its own worktree** — merge-conflict surface is minimized by scoping T2 strictly to `extract_vault_entry_handler`, `require_project_dir`, and their tests; the browsing handlers/tests are not reformatted or moved (FR-7).
- **`source_project` derivation edge** — a meeting directly under the vault root has `parent == root`; `resolve_entry` only yields `<root>/<project|unsorted>/<meeting>` shapes, so `meeting_dir.parent.name` is always a project code or `unsorted`; T4's tests cover both branches rather than inventing a third.
- **Full E2E needs a downloaded GGUF model** — T7 smoke-drives the real app to the enqueue boundary (button enabled, job appears, output path correct) and runs real extraction only if the local model is present, degrading explicitly rather than silently.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3, T4, T6 |
| 2 | T5 |
| 3 | T7 |

## Tasks

### [ ] T1: Vault crate — move the artifact-dir contract's documented anchor to the meeting folder  [deps: —]

- **Files**: `crates/vault/src/paths.rs`, `crates/vault/src/artifacts.rs`, `crates/vault/src/lib.rs`, `crates/vault/src/list.rs`
- **Test first**: `crates/vault/src/paths.rs` (`#[cfg(test)]` in-module) — cases: a contract test pinning the exact strings `ACTION_ITEMS_DIR_NAME == "action items"`, `FACTS_DIR_NAME == "facts"`, and `RESERVED_PROJECT_DIR_NAMES` still containing both plus `reports` (FR-1, FR-5). Confirm `list.rs`'s existing reserved-name test (line ~371, `["action items", "Facts", "REPORTS"]` skipped by `list_meetings`) still passes unchanged (FR-5).
- **Implement**: Doc-comment-only behavior: rewrite `ACTION_ITEMS_DIR_NAME`/`FACTS_DIR_NAME` docs from "Reserved project-level directory" to "Reserved directory *inside a meeting folder*" (mirroring `EXPORTS_DIR_NAME`'s wording, incl. the no-listing-exclusion-needed rationale); annotate `RESERVED_PROJECT_DIR_NAMES` as excluding *legacy* project-level trees that stay on disk unread (Q1). Update the layout tables in `artifacts.rs`/`lib.rs` module docs to mark project-level artifact enumeration as legacy-only. No signature or behavior changes; `ArtifactKind::dir_name` and `list_project_artifacts` are left intact for F7 to remove.
- **Skills**: —
- **Done when**: New contract test passes; all existing vault-crate tests pass untouched; `make format lint test` green on the Rust side.

### [ ] T2: Retarget extraction to `<meeting>/<kind>` and drop the project gate (Tauri command)  [deps: —]

- **Files**: `apps/desktop/src-tauri/src/commands/llm.rs`
- **Test first**: `apps/desktop/src-tauri/src/commands/llm.rs` `mod tests` — cases: (a) replace `extraction_targets_the_project_level_artifact_directory` with a test asserting the fake service's submission `output_dir` ends with `ELS/<meeting>/facts` — i.e. `<meeting>/<kind>`, and that nothing targets `<root>/ELS/facts` (FR-1 crit. 3); (b) replace `extraction_on_an_unsorted_meeting_is_refused_with_an_actionable_message` with `extraction_on_an_unsorted_meeting_enqueues_into_the_meeting_folder`: an `unsorted/` meeting with a transcript enqueues, `output_dir` ends with `unsorted/<meeting>/action items` (FR-2 crit. 1); (c) extraction on a meeting *without* a transcript is still refused with the "transcribe it first" message (FR-2 crit. 3).
- **Implement**: In `extract_vault_entry_handler` (line ~236): delete the `require_project_dir` call; `let output = meeting_dir.join(artifact_kind.dir_name());` (same pattern as `export_recording_handler`). Delete the now-orphaned `require_project_dir` fn (line 170) — verify no other caller. Keep `require_transcript`. Do not touch the artifact-browsing handlers (FR-7, F7 conflict surface).
- **Skills**: —
- **Done when**: The three tests pass; the old two test names are gone; `cargo` builds warning-free (no dead `require_project_dir`); `make format lint test` green on the Rust side.

### [ ] T3: Python artifacts module — contract docs and the deeper 260-char budget  [deps: —]

- **Files**: `services/transcription/src/transcription/artifacts.py`, `services/transcription/tests/test_llm_units.py`
- **Test first**: `services/transcription/tests/test_llm_units.py` — cases: (a) contract test pinning `ACTION_ITEMS_DIR_NAME == "action items"`, `FACTS_DIR_NAME == "facts"` (the Python half of FR-1 crit. 4); (b) extend `test_fit_slug_trims_against_the_260_char_budget` with a realistically deep meeting-level parent — e.g. `<long root ~170 chars>/<PROJECT>/260101 - a long meeting title/action items` — asserting the fitted slug keeps `parent/<slug>/<slug>.md` *and* the 20-char screenshot sibling within 260 chars, and that an impossibly deep meeting path still raises `INVALID_REQUEST` (NFR-1).
- **Implement**: Update the `artifacts.py` module docstring's layout table: `<meeting>/action items/<slug>/…` and `<meeting>/facts/<slug>/…` (reports/exports lines unchanged), noting legacy project-level trees are no longer read (Q1). No function-body changes — `write_item`/`list_items`/`fit_slug` are already anchor-agnostic; atomic write ordering untouched (NFR-2).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: New/extended tests pass; `make format lint type test` green on the Python side.

### [ ] T4: `_extract_sync` provenance — `source_project` from the meeting's parent, null for unsorted  [deps: —]

- **Files**: `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` (extraction section, lines ~285–480) — cases: (a) retarget every extraction test's `items_dir` from `meeting_dir.parent / "<kind>"` to `meeting_dir / "<kind>"` and assert items land there (FR-1 crit. 1–2); (b) `test_action_items_are_written_with_screenshots_and_front_matter` still asserts `source_project == "ELS"` and `source_meeting == MEETING_NAME` with the new layout (FR-3 crit. 1); (c) new test: extraction for a meeting under `vault/unsorted/` succeeds and writes front matter with `source_project` of JSON `null` (`meta["source_project"] is None`) and items under `unsorted/<meeting>/action items/` (FR-2 crit. 1, FR-3 crit. 2); (d) collision-suffix and screenshot-degradation tests keep passing at the new anchor.
- **Implement**: In `_extract_sync` (jobs.py line ~895), replace `project_name = Path(job.output_path).parent.name` with a derivation off `meeting_dir.parent`: `None` when `parent.name.casefold() == "unsorted"` (the `_export_sync` line-1012 idiom), else `parent.name`. No other front-matter changes (F8 owns new fields).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: Extraction tests pass with meeting-level `output_dir`s and both `source_project` branches covered; `make format lint type test` green on the Python side.

### [ ] T5: Export reads meeting-level items; unsorted special case removed  [deps: T4]

- **Files**: `services/transcription/src/transcription/exporting.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` (export section, lines ~485–535) — cases: (a) rework `test_export_assembles_sections_in_order_and_renders_a_pdf`: items written via `write_item` into `meeting_dir / "action items"` appear under "## Action items"; a legacy item planted at `meeting_dir.parent / "action items"` (even with matching `source_meeting`) does **not** appear — legacy trees are unread, not deleted (FR-4, FR-6, Q1); section order unchanged; (b) new test: an `unsorted/<meeting>` export includes its meeting-level items — no empty-section special case (FR-4 crit. 2); (c) screenshot links in an exported item body resolve relative to the export dir, i.e. rewrite to `../../action items/<slug>/screenshot-*.png` from `<meeting>/exports/<YYMMDD>/` (FR-4 crit. 1).
- **Implement**: In `exporting.py`, replace `items_for_meeting(project_dir, kind_dir_name, meeting_name)` with a meeting-level lister — `list_items(meeting_dir / kind_dir_name)`, no `source_meeting` filter — and drop `build_export_md`'s `project_dir` parameter (`_relocate_screenshot_links` already handles arbitrary relative depth). In `_export_sync` (jobs.py line ~1008), delete the `parent`/`project_dir` computation and the unsorted branch; call `build_export_md(meeting_dir=…, meeting_name=…, export_dir=…)`.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: Export tests pass for filed and unsorted meetings; no reference to `project_dir` remains in `exporting.py`/`_export_sync`; `make format lint type test` green on the Python side.

### [ ] T6: RecordingPage — enable Action items / Facts for unfiled recordings  [deps: —]

- **Files**: `apps/desktop/src/components/RecordingPage.tsx`, `apps/desktop/src/components/RecordingPage.test.tsx`
- **Test first**: `apps/desktop/src/components/RecordingPage.test.tsx` — cases: (a) an entry with `project: null` and a transcript renders "Action items" and "Facts" buttons **enabled**, and clicking each calls `onExtract(id, "action_items" | "facts")` (FR-2 crit. 2); (b) the "File this recording under a project first" tooltip (`title` attribute) is absent for unfiled entries; (c) buttons still disable while their job kind is in `activeLlmJobs` (existing behavior preserved).
- **Implement**: In `RecordingPage.tsx` lines 214–239: change both `disabled={!entry.project || activeLlmJobs.includes(…)}` to `disabled={activeLlmJobs.includes(…)}` and delete both conditional `title={…}` tooltip props. No other UI changes (the `unsorted` pill at line 182 stays).
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: New Vitest cases pass, existing RecordingPage tests untouched-and-green; `make format lint type test` green on the frontend side.

### [ ] T7: Integration verification — full QA plus a driven app smoke of the unsorted flow  [deps: T1, T2, T3, T4, T5, T6]

- **Files**: — (read-only verification; no source edits — regressions found here are fixed in the owning task's files by re-opening that task)
- **Test first**: n/a (this task executes the existing suites and drives the app; it adds no new test files)
- **Implement**: Run all four `make` targets (format, lint, type, test) across Rust, Python and frontend (NFR-3). Then launch the dev app (`tauri dev`, per the desktop profile's verification rule) against a scratch vault containing a filed and an `unsorted/` meeting with transcripts: verify both recordings show enabled Action items / Facts buttons; trigger extraction and confirm the job enqueues with `output_dir = <meeting>/<kind>`. If the local GGUF model is present, let one extraction complete and confirm files land at `<root>/<PROJECT>/<meeting>/action items/<slug>/<slug>.md` with nothing new under `<root>/<PROJECT>/action items/` (FR-1 crit. 1) and that a subsequent Export PDF includes the items; if the model is absent, record the enqueue-level evidence and the degradation explicitly in the task result.
- **Skills**: `frontend-toolkit:internal-ui`, `testing-toolkit:python-testing-patterns`
- **Done when**: All four make targets pass on every side; the driven-flow evidence (button state, job `output_dir`, and — model permitting — on-disk artifact locations) matches FR-1/FR-2; legacy project-level trees are byte-identical before/after (FR-6).

## QA expectations

- `make format`, `make lint`, `make type`, `make test` all exist (Makefile lines 20/31/42/52) and cover Rust + Python + frontend; NFR-3 requires all four green.
- Rust tests: workspace `cargo test` (vault crate in-module tests, `commands/llm.rs` `mod tests` with `FakeService`). Python: `pytest` (`test_llm_jobs.py` is async via pytest-asyncio; job tests poll workers — the existing 5s deadlines are the known slow spots, not flakes). Frontend: `vitest run`.
- No known-flaky suites. T7 additionally requires a `tauri dev` launch (desktop profile verification); real-LLM completion there is conditional on the operator's downloaded model and must be reported either way.
