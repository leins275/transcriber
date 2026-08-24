---
slug: project-view-recordings-only
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Project view shows recordings only

## Architecture overview

This is a deletion-heavy feature across three languages plus one small rebuild. Nothing new is invented; the surviving ProjectPage reuses components that already exist.

**What survives (the rebuild).** `apps/desktop/src/components/ProjectPage.tsx` becomes a recordings-only full-window page (operator decision Q1): breadcrumb ("← Recordings" + project pill, kept from the current page head) plus the project's recordings rendered with the existing `VaultList` (`apps/desktop/src/components/VaultList.tsx`) filtered by the existing `entriesForProject` helper (`apps/desktop/src/lib/vaultGroups.ts`). `App.tsx` keeps owning which page is open (`openProject` state, "Open project →" in `VaultPanel.tsx:197-203` unchanged) and now passes the page `entries` + the same `onOpen`/`onReveal` handlers `VaultPanel` gets — the page stays presentational, like every other component in this app.

**What is deleted, per language (each track independently green under its own gate):**

- **Python** (`services/transcription`): `"report"` leaves `schema.py::JobType` and the `JobCreate` docstring; `jobs.py` loses the `llm.report` import (line 47), the `"report"` member of `KNOWN_JOB_TYPES` (line 67), the `_report_sync` body (~958–1005) and its dispatch arm in `_run_derived_job` (~675) — the dispatch's `else` branch currently *is* report, so the chain must be restructured to stay exhaustive, not just trimmed; `llm/report.py` is deleted whole; `report_messages` leaves `llm/prompts.py` (209–231; `chunk_summary_messages` stays — `llm/summarize.py:13,33` uses it); `REPORTS_DIR_NAME` leaves `artifacts.py:37` (its only src caller is the deleted `llm/report.py` — re-verify against F6's landed state in this worktree). `POST /v1/jobs` with `job_type: "report"` then fails pydantic literal validation → 4xx.
- **Rust, Tauri app** (`apps/desktop/src-tauri/src`): `commands/llm.rs` loses `export_project_essence`, `list_project_artifacts`, `read_artifact`, `reveal_artifact`, `list_project_reports`, `read_report`, `reveal_report` (handlers + `#[tauri::command]` wrappers + their tests, incl. `reports_list_and_read_and_reveal_prefers_the_pdf` at ~1036), the `ArtifactView`/`ArtifactContentView`/`ArtifactImageView`/`ReportView` structs, the image/markdown size caps and `base64` use that lose their callers, and the `REPORTS_DIR_NAME` import; `lib.rs:230-236` drops the seven registrations; `service/mod.rs` drops `LlmJobKind::Report` + its `wire_name` arm (127–128, 140). Surviving in that module: `summarize_vault_entry`, `extract_vault_entry` (still uses `vault::ArtifactKind`), `export_recording` (still uses `EXPORTS_DIR_NAME` + `dated_subdir`), and the LLM model-download trio.
- **Rust, vault crate** (`crates/vault/src`): `artifacts.rs` loses `ReportEntry`/`list_reports` (+ tests); `ArtifactEntry`/`list_project_artifacts` go too **iff** F6's landed state left them with no caller. `paths.rs` is untouched: `REPORTS_DIR_NAME` and `RESERVED_PROJECT_DIR_NAMES` stay (FR-6), and the guarding test `list.rs:364` (`reserved_artifact_directories_are_never_listed_as_meetings`, covering `"REPORTS"`) must keep passing. Ordering matters: the app-side callers (llm.rs) go first; the then-uncalled `pub` vault items don't trip clippy in a lib crate, so both tasks stay green.
- **Frontend** (`apps/desktop/src`): `types.ts` drops `"report"` from `JobType` (line 27) and the `ArtifactView`/`ArtifactImageView`/`ArtifactContentView`/`ReportView` types (136–161; `ArtifactKind` stays — the extract flow uses it); `api.ts` drops the seven report/artifact wrappers (131–144); `JobRow.tsx` drops the `report` keys from `RUNNING_TEXT`/`FAILED_TEXT` (32, 44 — these are `Record<JobType, string>`, so the type change forces it); `App.tsx` drops `handleExportEssence` (378–383), `essenceBusy` (428–432) and `projectReloadToken` (425–427 — ProjectPage was its only consumer); copy/doc cleanups in `SettingsPage.tsx:147,171`, `VaultPanel.tsx:39`, `App.tsx:86`.

**FR-7 doc audit result**: `docs/*` contains no report-job promises (only unrelated uses of the word "report"); the doc surface reduces to `services/transcription/README.md:109` (job-type table row) plus in-code comments (`commands.rs:53`, `jobs.rs:73` Rust-side; `config.py:106`, `ledger.py:61`, `pdf.py` docstring, `schema.py` docstrings Python-side).

## Risks

- **`_run_derived_job` dispatch collapse** (`jobs.py` ~666–679): report is the current `else` arm. Deleting it naively either leaves a dead `else` or makes `extract` the silent catch-all for future unknown types. T1 must restructure the chain to stay explicit (e.g. `elif`-per-type; an unknown LLM type can't reach here — `KNOWN_JOB_TYPES` + pydantic gate it — but the code should not *rely* on a catch-all).
- **F6 reconciliation** (sibling `artifacts-in-sync-folder`, ordered before this feature in the batch): this plan's line numbers and the "only caller" claims for `vault::list_project_artifacts`/`ArtifactEntry` and Python `artifacts.py` constants are a snapshot. T1 and T4 must grep the worktree's *landed* state before deleting — delete only what is actually uncalled there (spec sibling-coordination clause).
- **Over-deletion in `commands/llm.rs`**: `require_safe_component`, `dated_subdir`, `EXPORTS_DIR_NAME` and the extract/export handlers share the file with the deleted code. Clippy `-D warnings` catches under-deletion (dead private items); the surviving extract/export tests catch over-deletion (FR-4 acceptance).
- **`ArtifactKind` over-deletion on either side of the IPC boundary** (Rust `vault::ArtifactKind`, TS `types.ts::ArtifactKind`): both stay — extraction from a recording keeps working (FR-4, out-of-scope note).
- **App-launch smoke on Windows**: `tauri dev` boots the Python sidecar; the desktop profile requires driving the real app for the final verification, so T6 budgets for that rather than trusting unit suites (profile: "a passing unit suite is not evidence that a page renders").

## Waves

Three independent language tracks first (no deps, disjoint files), then the two dependent cleanups, then integration.

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3 |
| 2 | T4, T5 |
| 3 | T6 |

## Tasks

### [x] T1: Python — delete the `report` job type end-to-end  [deps: —]

- **Files**: `services/transcription/src/transcription/schema.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/src/transcription/llm/report.py` (delete), `services/transcription/src/transcription/llm/prompts.py`, `services/transcription/src/transcription/artifacts.py`, `services/transcription/src/transcription/config.py` (comment only), `services/transcription/src/transcription/ledger.py` (comment only), `services/transcription/src/transcription/pdf.py` (docstring only), `services/transcription/tests/test_llm_jobs.py`, `services/transcription/tests/test_api_llm.py`, `services/transcription/README.md`
- **Test first**: `services/transcription/tests/test_api_llm.py` — new case: `POST /v1/jobs` with `job_type: "report"` returns a 4xx validation error (FR-2 acceptance). `services/transcription/tests/test_llm_jobs.py` — delete `test_report_reads_all_project_materials_and_writes_md_plus_pdf` (537) and `test_a_report_over_an_empty_project_fails_as_unsupported_input` (564); the surviving summarize/extract/export tests are the regression net for FR-2's "`chunk_summary_messages` still used" and the out-of-scope export flow.
- **Implement**: Remove `"report"` from `schema.py:23` `JobType` and the `JobCreate` docstring (121–129); in `jobs.py` remove the line-47 import, the `KNOWN_JOB_TYPES` member (67), `_report_sync` (~958–1005), and restructure the `_run_derived_job` dispatch (~666–679) to stay explicit without report as the `else` arm; delete `llm/report.py`; remove `report_messages` from `llm/prompts.py` (209–231), keeping `chunk_summary_messages`; remove `REPORTS_DIR_NAME` from `artifacts.py` (verify no F6-landed caller first) and the `reports/` line in its module docstring; FR-7 comment/doc sweep: `config.py:106`, `ledger.py:61`, `pdf.py` "export/report" docstring, `README.md:109` table row.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: `uv run --directory services/transcription pytest -q` passes; `grep -ri "report_messages\|_report_sync\|collect_project_materials"` over `services/transcription/src` finds nothing; `grep -r '"report"' services/transcription` finds nothing; `make format`, `make lint`, `make type` pass for the Python tree.

### [x] T2: Rust (Tauri app) — delete report + artifact-browsing commands  [deps: —]

- **Files**: `apps/desktop/src-tauri/src/commands/llm.rs`, `apps/desktop/src-tauri/src/lib.rs`, `apps/desktop/src-tauri/src/service/mod.rs`, `apps/desktop/src-tauri/src/commands.rs` (doc comment line 53 only), `apps/desktop/src-tauri/src/jobs.rs` (doc comments lines 73, 726 only)
- **Test first**: `commands/llm.rs` test module — delete the report/artifact-browsing tests (`reports_list_and_read_and_reveal_prefers_the_pdf` ~1036, the `list_project_artifacts_handler` case ~988, and any `read_artifact`/`reveal_artifact`/`export_project_essence` cases); the surviving extract/export/summarize tests must still pass unchanged — they are FR-4's "extraction still compiles and its tests pass" evidence.
- **Implement**: In `commands/llm.rs` delete `export_project_essence` (~270–279), the artifact handlers (~285–430), the report handlers (~440–510), their `#[tauri::command]` wrappers (~600–680), the `ArtifactView`/`ArtifactContentView`/`ArtifactImageView`/`ReportView` structs, the now-uncalled size-cap consts and `base64` use, and the `REPORTS_DIR_NAME` import (keep `ArtifactKind`, `EXPORTS_DIR_NAME`, `dated_subdir`, `require_safe_component` where extract/export still call them); update the module header (lines 1–3). Drop the seven registrations from `lib.rs:230-236`. In `service/mod.rs` delete `LlmJobKind::Report` (127–128), its `wire_name` arm (140), and fix the doc comments (83–84). FR-3/FR-7 doc sweep: `commands.rs:53`, `jobs.rs:73/726`.
- **Skills**: —
- **Done when**: `cargo test --workspace` and `cargo clippy --workspace --all-targets -- -D warnings` pass (the vault crate's now-uncalled `pub` items stay green until T4); `grep -ri "export_project_essence\|list_project_reports\|read_report\|reveal_report\|LlmJobKind::Report\|ReportView\|ArtifactView\|ArtifactContentView"` over `apps/desktop/src-tauri/src` finds nothing.

### [x] T3: Frontend — rebuild ProjectPage as a recordings-only page  [deps: —]

- **Files**: `apps/desktop/src/components/ProjectPage.tsx`, `apps/desktop/src/components/ProjectPage.test.tsx` (new), `apps/desktop/src/components/ProjectPage.module.css`, `apps/desktop/src/App.tsx`, `apps/desktop/src/components/VaultPanel.tsx` (doc comment line 39 only)
- **Test first**: `apps/desktop/src/components/ProjectPage.test.tsx` (new) — cases: renders the breadcrumb (back button + project pill) and the project's recordings via `VaultList` (FR-1); renders **no** tablist, no "Action items"/"Facts"/"Reports" tabs, and no "Export project essence" button (FR-1 acceptance); clicking a recording row calls `onOpen` with its entry id (FR-1 acceptance: existing open flow unchanged); an empty project renders an empty state, not a crash.
- **Implement**: Rewrite `ProjectPage` per the Q1 decision (spec Decisions log 2026-08-24): props `{ project, entries, onBack, onOpen, onReveal }`; keep the page-head/breadcrumb pattern from the current file, render `VaultList` (as `VaultPanel.tsx:205` does) instead of tabs/panes; trim `ProjectPage.module.css` to the surviving classes. In `App.tsx`: pass `entriesForProject(vaultEntries, openProject)` (from `lib/vaultGroups`) plus the existing `handleRevealVaultEntry`/open handlers; delete `handleExportEssence` (378–383), `essenceBusy` (428–432), `projectReloadToken` (425–427); fix the line-86 comment. Fix `VaultPanel.tsx:39` doc comment. Leave `api.ts`/`types.ts` untouched (T5 owns them; their unused exports keep `tsc` green meanwhile).
- **Skills**: `frontend-toolkit:internal-ui` (mandatory), `frontend-toolkit:ui-ux-pro-max`
- **Done when**: `npm --prefix apps/desktop run test` and `run type` pass; `grep -rn "onListArtifacts\|onExportEssence\|essenceBusy\|reloadToken" apps/desktop/src/components/ProjectPage.tsx apps/desktop/src/App.tsx` finds nothing.

### [x] T4: vault crate — remove report listing (and artifact listing if F6 left it uncalled)  [deps: T2]

- **Files**: `crates/vault/src/artifacts.rs`, `crates/vault/src/lib.rs`
- **Test first**: `crates/vault/src/artifacts.rs` test module — delete the `list_reports` cases (~197, ~251) and, only if the functions themselves go, the `list_project_artifacts` cases (~194–237). FR-6's guard is `crates/vault/src/list.rs:364` `reserved_artifact_directories_are_never_listed_as_meetings` (fixtures include `"REPORTS"`) — it must **not** be touched and must still pass; it is this feature's proof that a vault containing `<PROJECT>/reports/260101/report.md` lists meetings without a `reports` entry.
- **Implement**: Delete `ReportEntry` (50) and `list_reports` (142) from `artifacts.rs`; grep the worktree's landed state (F6 merged before this feature) for callers of `vault::list_project_artifacts`/`ArtifactEntry` — delete both iff uncalled, keep verbatim otherwise (spec FR-4 sibling clause); trim the `lib.rs:132` re-export list accordingly, keeping `ArtifactKind`. `paths.rs` stays untouched: `REPORTS_DIR_NAME` (74) and `RESERVED_PROJECT_DIR_NAMES` (84–85) survive per FR-6.
- **Skills**: —
- **Done when**: `cargo test --workspace` and `cargo clippy --workspace --all-targets -- -D warnings` pass; `grep -ri "ReportEntry\|list_reports"` over `crates/vault/src` and `apps/desktop/src-tauri/src` finds nothing; `list.rs:364` test passes unmodified.

### [x] T5: Frontend — purge `"report"` and dead artifact types from api/types/JobRow + copy  [deps: T3]

- **Files**: `apps/desktop/src/api.ts`, `apps/desktop/src/types.ts`, `apps/desktop/src/components/JobRow.tsx`, `apps/desktop/src/components/SettingsPage.tsx`, `apps/desktop/src/components/SettingsPage.test.tsx`
- **Test first**: `apps/desktop/src/components/SettingsPage.test.tsx` — update/add the copy assertion so the LLM-model section no longer mentions "project reports" (FR-7 acceptance); `JobRow.tsx` needs no new test — `RUNNING_TEXT`/`FAILED_TEXT` are `Record<JobType, string>`, so dropping `"report"` from `JobType` makes the type gate itself the failing-first check (FR-5).
- **Implement**: `types.ts`: drop `"report"` from `JobType` (27); delete `ArtifactView` (136–139), `ArtifactImageView` (141–145), `ArtifactContentView` (149–154), `ReportView` (156–161); keep `ArtifactKind` (133 — extract flow). `api.ts`: delete `exportProjectEssence`, `listProjectArtifacts`, `readArtifact`, `revealArtifact`, `listProjectReports`, `readReport`, `revealReport` (131–144) and their type imports (module comment too). `JobRow.tsx`: drop the `report` keys (32, 44). `SettingsPage.tsx`: reword lines 147 and 171 to drop "and project reports" (FR-7).
- **Skills**: `frontend-toolkit:internal-ui` (mandatory), `frontend-toolkit:ui-ux-pro-max`
- **Done when**: `npm --prefix apps/desktop run type`, `run test`, `run lint` pass; `grep -rn '"report"' apps/desktop/src` returns no JobType/label match (FR-5 acceptance); `grep -rn "ArtifactView\|ArtifactContentView\|ReportView" apps/desktop/src` finds nothing (NFR-2).

### [x] T6: Integration — full QA gates, acceptance greps, live app smoke  [deps: T1, T2, T3, T4, T5]

- **Files**: none (read-only verification; any regression found is fixed under the owning task's file contract, not here)
- **Test first**: n/a — this task *is* the verification layer (desktop + web profile Verification sections): the full cross-language suites plus a live app drive, after all deletions have landed.
- **Implement**: Run all four gates: `make format`, `make lint` (includes `cargo clippy --workspace --all-targets -- -D warnings`), `make type`, `make test` (NFR-1). Run every acceptance grep from the spec (FR-2, FR-3, FR-4, FR-5 lists). Then the desktop-profile smoke: launch the real app (`tauri dev` via the repo's dev flow), and with a vault containing a project that has recordings **and** a legacy `reports/260101/report.md` folder, verify: "Open project →" opens the recordings-only page (no tabs, no essence button anywhere — FR-1), a recording opens from it (FR-1), the legacy `reports/` folder neither appears as a meeting nor gets deleted (FR-6), and no user-visible string mentions "Export project essence" or report jobs (FR-7).
- **Skills**: `frontend-toolkit:internal-ui` (judging the surviving page), `testing-toolkit:python-testing-patterns` (interpreting the pytest gate)
- **Done when**: all four make targets pass from a clean tree; every FR-2/3/4/5 grep in the spec's acceptance list comes back empty; the live-app smoke observations above are recorded in the task output.

## QA expectations

All four Makefile targets exist and are the gate (spec stack table): `make format`, `make lint` (`cargo clippy --workspace --all-targets -- -D warnings` — stranded dead code fails), `make type`, `make test` (`cargo test --workspace` + `npm --prefix apps/desktop run test` + `uv run --directory services/transcription pytest -q`). Nothing known-flaky; the surviving export-PDF pytest pays a multi-second first xhtml2pdf/reportlab render (its own timeout already accounts for it). Per memory: use `uv`, never bare `python`. Batch note: this feature runs in its own worktree after F6 (`artifacts-in-sync-folder`) — T1 and T4 re-verify callers against the landed state before deleting shared artifact code.
