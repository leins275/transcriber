---
slug: project-view-recordings-only
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Project view shows recordings only

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 2 | 0 | 0 |

The diff implements the spec. The report machinery is deleted end-to-end across all three languages — Python job type/dispatch/module/prompt, Rust command handlers/wrappers/registrations/`LlmJobKind::Report`, vault `ReportEntry`/`list_reports` (plus the F6-uncalled `ArtifactEntry`/`list_project_artifacts`), and the frontend types/api/labels — and `ProjectPage` is rebuilt as a presentational recordings-only page per the Q1 decision, with a new focused test file. Every spec acceptance grep comes back empty (verified independently); `RESERVED_PROJECT_DIR_NAMES`, `paths.rs` and the `list.rs:364` reserved-name guard are untouched; no code path added deletes vault data (the diff is pure removal plus a presentational page). All gates verified in this worktree: pytest (exit 0), vitest 277/277 + `tsc --noEmit`, `cargo test --workspace` (all suites ok), `cargo clippy --workspace --all-targets -- -D warnings`, `cargo fmt --check`, eslint, ruff check + format — with no `#[allow(dead_code)]`/`noqa`/skip escape hatches added anywhere in the diff. Two minors remain: a stale doc surface in `Markdown.tsx`, and a plan-internal grep criterion that contradicts the plan's own mandated test.

## Findings

### E1 [minor] [spec-drift] [status: open]

- **Where**: `apps/desktop/src/components/Markdown.tsx:5-21` (worktree path `specs/_intake/various-improvements/worktrees/project-view-recordings-only/...`)
- **Spec ref**: FR-7 ("module docs no longer promise project reports"); spec context clause "Dead code is removed, not stranded" (NFR-1's spirit)
- **Expected**: No module doc promises reports, and nothing stranded loses its last caller silently.
- **Actual**: The component doc still reads "The one markdown renderer in this app (summaries, action items, facts, reports)" — both artifacts and reports are gone from the app. And the `images` prop (with its data-URL/artifact-screenshot doc block) lost its last caller when the ProjectPage artifact pane was deleted: the sole remaining call site, `SummaryPanel.tsx:71`, passes no `images`. Because the prop is optional, neither `tsc` nor eslint can catch it — exactly the stranded-dead-surface case the gates were expected to police.
- **Suggested fix**: Drop "reports" (and "action items, facts") from the doc comment; delete the `images` prop and its map lookup, keeping the alt-text fallback for any relative/remote `img` (that guard is still correct for model-emitted markdown in summaries).

### E2 [minor] [spec-drift] [status: open]

- **Where**: `specs/project-view-recordings-only/plan.md` T1 "Done when" (`grep -r '"report"' services/transcription` finds nothing) vs `services/transcription/tests/test_api_llm.py:153-175`
- **Spec ref**: FR-2 acceptance (src-scoped grep) vs plan T1 done-when (tree-wide grep)
- **Expected**: Every done-when criterion is satisfiable alongside the same task's mandated test.
- **Actual**: The plan's tree-wide literal grep can never pass: T1's own required rejection test deliberately posts `"job_type": "report"`, so the grep finds it (verified: the only remaining `"report"` literals in `services/transcription` are that test's payload and docstring). The spec's actual acceptance criterion is scoped to `services/transcription/src` and passes clean. The implementation made the right call — the test is the FR-2 acceptance evidence and must stay.
- **Suggested fix**: Amend the plan's T1 done-when to scope the grep to `services/transcription/src` (matching the spec). Do **not** delete or weaken the rejection test to satisfy the literal grep.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (recordings-only project view) | `apps/desktop/src/components/ProjectPage.tsx` (full rewrite), `App.tsx:501-511` (entries + onOpen/onReveal wiring), `ProjectPage.module.css` | `ProjectPage.test.tsx` — no tablist/tabs, no essence button, onOpen("v-42") via VaultList row, empty-state case | ✓ (live-app smoke deferred to the pipeline's UI-validation phase; component tests cover every listed assertion) |
| FR-2 (Python `report` deleted) | `schema.py:18-23,121-129`, `jobs.py` (import, `KNOWN_JOB_TYPES`, dispatch restructured to an explicit raise, `_report_sync` gone), `llm/report.py` deleted, `prompts.py` (`report_messages` gone, `chunk_summary_messages` kept), `artifacts.py` (`REPORTS_DIR_NAME` gone; `list_items` kept — `exporting.py:22,75` still calls it) | `test_api_llm.py::test_a_report_job_type_is_rejected_as_a_validation_error` (400 + `invalid_request`); surviving summarize/extract/export suites pass; report tests deleted | ✓ |
| FR-3 (Rust report machinery deleted) | `commands/llm.rs` (handlers, wrappers, views, size caps, base64, `resolve_project_dir`, `require_safe_component` all gone), `lib.rs:227-233` (7 registrations dropped), `service/mod.rs` (`LlmJobKind::Report` + wire_name arm gone), `crates/vault/src/artifacts.rs` (`ReportEntry`/`list_reports` gone) | `cargo test --workspace` ok; clippy `-D warnings` ok; acceptance grep over `src-tauri/src` + `crates/vault/src` empty | ✓ |
| FR-4 (artifact browsing deleted, extraction survives) | Same `commands/llm.rs`/`lib.rs` deletions; `vault::list_project_artifacts`/`ArtifactEntry` deleted (no F6-landed caller — workspace compiles), `ArtifactKind` kept both sides (`crates/vault/src/artifacts.rs:29-40`, `types.ts:133`); `api.ts`/`types.ts` counterparts gone | Surviving extract/export handler tests in `commands/llm.rs` test module pass; grep for `ArtifactView\|ArtifactContentView\|ReportView` empty in both trees | ✓ |
| FR-5 (frontend knows no `"report"`) | `types.ts:27` (JobType), `JobRow.tsx` (both label maps — `Record<JobType, string>` forces it), `App.tsx` (`handleExportEssence`/`essenceBusy`/`projectReloadToken` deleted) | `tsc --noEmit` + vitest 277/277 pass; `grep '"report"' apps/desktop/src` — zero JobType/label matches (remaining `reloadToken` hits are `SummaryPanel`'s own unrelated prop) | ✓ |
| FR-6 (on-disk data untouched, reserved names hold) | `crates/vault/src/paths.rs` — zero diff (`REPORTS_DIR_NAME:74`, `RESERVED_PROJECT_DIR_NAMES:84`); diff adds no filesystem writes/deletes anywhere | `list.rs:364::reserved_artifact_directories_are_never_listed_as_meetings` (fixture includes `"REPORTS"`) unmodified and passing | ✓ |
| FR-7 (copy/doc sweep) | `SettingsPage.tsx:147,171`, `commands.rs:53`, `commands/llm.rs` header, `VaultPanel.tsx:39`, `App.tsx:86`, `service/mod.rs:83-84`, `jobs.rs:73,726`, Python `config.py`/`ledger.py`/`pdf.py`/`schema.py` docstrings, `services/transcription/README.md` table; `docs/*` and top-level README verified clean | `SettingsPage.test.tsx:111-124` — two explicit no-"project reports" copy assertions | ✓ except E1 (`Markdown.tsx` doc) |
| NFR-1 (all gates, no escape hatches) | — | pytest, vitest+tsc, cargo test, clippy `-D warnings`, cargo fmt, eslint, ruff check+format all pass in this worktree; diff adds no `allow(dead_code)`/`noqa`/`eslint-disable`/skips | ✓ |
| NFR-2 (no dead frontend exports) | `api.ts` (7 wrappers gone), `types.ts` (4 view types gone) | eslint + tsc pass; grep for the deleted names empty | ✓ (E1's `images` prop is component-local, not an api/types export) |

## Positive notes

- The `_run_derived_job` dispatch restructure (`jobs.py:665-677`) is exactly what the plan's top risk demanded: report was the old `else` arm, and instead of letting `extract` become a silent catch-all, the new `else` raises an explicit `invalid_request` with a comment explaining why it is unreachable. Do not "simplify" this away.
- The FR-2 rejection test is well-constructed: it passes deliberately *valid* paths so the 400 can only come from the `JobType` literal, not path validation — the failure mode it pins is precise.
- `ArtifactKind` survived correctly on both sides of the IPC boundary, and `artifacts.rs` was reduced to a pure name-mapping module with an honest new doc explaining why enumeration left. `list_items`/`exporting.py` (F4's surviving export path) were correctly spared on the Python side.
- The new `ProjectPage` is genuinely presentational (no invoke/listen/fetch), reuses `VaultList`/`entriesForProject` rather than inventing a parallel list, upgrades the breadcrumb to a labelled `<nav>` landmark, and its test file asserts absence (no tablist, no essence button) as first-class FR-1 evidence — the right way to test a removal.
- The plan-tracked checkboxes and `base_ref` were filled in; the worktree diff contains nothing outside the feature's file contract.
