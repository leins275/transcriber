---
slug: project-view-recordings-only
created: 2026-08-24
status: approved
---

# Spec: Project view shows recordings only

## Summary

Strip the project view down to a recordings list: the action-items, facts and reports tabs and the "Export project essence" button go away, and the project-essence `report` machinery is deleted end-to-end (job type, LLM orchestration module, Tauri commands, UI). The operator manages project essence in external tools; the app's job for a project is only to show its recordings.

## Problem & context

Today, clicking "Open project →" in the vault's projects tab (`apps/desktop/src/components/VaultPanel.tsx:197-203`) opens `ProjectPage` (`apps/desktop/src/components/ProjectPage.tsx`), a full-window page with three tabs — Action items, Facts, Reports — plus an "Export project essence" button that queues a `report` LLM job. The operator does not want this in-app "essence of the project" surface at all ("I'll do it outside better"). Notably, the projects tab in `VaultPanel.tsx` (lines 184–207) *already* renders each project's recordings via `VaultList` — the ProjectPage currently adds nothing recordings-related.

The report pipeline behind the button spans all three languages:

- **Frontend**: `App.tsx` (`handleExportEssence` ~378–383, `essenceBusy` ~428–432, `projectReloadToken` ~425–427, ProjectPage wiring ~517–528), `api.ts` (`exportProjectEssence`, `listProjectArtifacts`, `readArtifact`, `revealArtifact`, `listProjectReports`, `readReport`, `revealReport`), `types.ts` (`ArtifactView`, `ArtifactContentView`, `ReportView`, `"report"` in `JobType`), `JobRow.tsx` (report labels, lines 32/44).
- **Rust (Tauri)**: `apps/desktop/src-tauri/src/commands/llm.rs` (`export_project_essence`, `list_project_artifacts`, `read_artifact`, `reveal_artifact`, `list_project_reports`, `read_report`, `reveal_report` + their handlers, views and tests), `lib.rs:230-236` (command registration), `service/mod.rs:127-140` (`LlmJobKind::Report` → `"report"`), `crates/vault/src/artifacts.rs` (`ReportEntry`, `list_reports` + tests).
- **Python service**: `services/transcription/src/transcription/schema.py:23` (`"report"` in `JobType`), `jobs.py` (import at line 47, `_ALLOWED`-style set at line 67, dispatch at ~675, `_report_sync` at ~958–1005), `llm/report.py` (whole module), `llm/prompts.py:209+` (`report_messages`), `artifacts.py:37` (`REPORTS_DIR_NAME`), tests in `tests/test_llm_jobs.py` (~lines 482–580) and job-type enumerations in `test_api_llm.py` / `test_jobs.py` / `test_cli.py`.

Binding operator decisions from batch intake (`specs/_intake/various-improvements/intake.md`): (a) the project view keeps ONLY the recordings list; (b) the `report` machinery is deleted entirely; (c) F8 (archive/grouping) is data-only with no in-app UI, so the artifact-browsing commands have no future caller either. Dead code is removed, not stranded.

**Sibling coordination**: F6 (`artifacts-in-sync-folder`, ordered before this feature) moves action-item/fact artifacts into per-meeting folders; F8 is front-matter-only. Neither needs the project-view UI or the project-level artifact browsing commands, so deleting them here is safe — but the implementer must reconcile against F6's landed state for `vault::list_project_artifacts` / `ArtifactEntry` (delete them only if F6 left them with no remaining caller).

## Users

- The single operator of this local desktop app: browses recordings per project inside the app; curates action items, facts and any project synthesis in external tools reading the vault folder directly.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists (Tauri); Cargo workspace with a `tauri` dependency.
- `web` — `apps/desktop/package.json` names `react` (^18.3.1) and `vite` (^5.4.10); webview UI per the desktop profile's cross-reference.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 (Rust) | `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/src/lib.rs` |
| UI | React 18 + TypeScript + Vite 5 | `apps/desktop/package.json` |
| Shared core | Rust `vault` crate | `crates/vault/src/artifacts.rs`, `paths.rs` |
| Local service | Python (FastAPI sidecar, pydantic schemas, uv-managed) | `services/transcription/src/transcription/schema.py`, `pyproject.toml` |
| Testing | cargo test, vitest, pytest | `make -n test`: `cargo test --workspace`, `npm --prefix apps/desktop run test`, `uv run --directory services/transcription pytest -q` |

Makefile QA targets present: format, lint, type, test (all four; lint runs `cargo clippy -- -D warnings`, so stranded dead code fails the gate).

## Functional requirements

- **FR-1** (must): The project view shows only recordings. The action-items/facts/reports tabs, the artifact/report reading panes, and the "Export project essence" button are removed from the UI. The exact surviving mechanism (dedicated page vs. the existing projects-tab grouping) is Open question Q1.
- **FR-2** (must): The `report` job type is deleted from the Python service: removed from `schema.py` `JobType` and the `JobCreate` docstring/validator context, from the allowed-type set and dispatch in `jobs.py` (`_report_sync` deleted), `llm/report.py` deleted, `report_messages` removed from `llm/prompts.py` (keeping `chunk_summary_messages`, which `llm/summarize.py` uses), `REPORTS_DIR_NAME` removed from `artifacts.py` if no caller remains. `POST /v1/jobs` with `job_type: "report"` is rejected as a validation error.
- **FR-3** (must): The report machinery is deleted from the Rust side: `export_project_essence`, `list_project_reports`, `read_report`, `reveal_report` commands + handlers + `ReportView` in `commands/llm.rs`; their registrations in `lib.rs`; `LlmJobKind::Report` in `service/mod.rs`; `ReportEntry` / `list_reports` in `crates/vault/src/artifacts.rs`; their unit tests.
- **FR-4** (must): The artifact-browsing commands that lose their only caller (the deleted ProjectPage tabs) are deleted: `list_project_artifacts`, `read_artifact`, `reveal_artifact` + `ArtifactView` / `ArtifactContentView` / `ArtifactImageView` in `commands/llm.rs`, their `lib.rs` registrations, and their `api.ts` / `types.ts` counterparts. `vault::list_project_artifacts` / `ArtifactEntry` are deleted too unless F6's landed code still calls them. `ArtifactKind` stays wherever extraction (action items / facts from a recording) still needs it.
- **FR-5** (must): No frontend surface knows `"report"` anymore: `types.ts` `JobType` drops it, `JobRow.tsx` drops its labels, `App.tsx` drops `handleExportEssence` / `essenceBusy` and the ProjectPage-only reload bookkeeping (`projectReloadToken`) if nothing else consumes it.
- **FR-6** (must): Existing on-disk data is untouched and stays invisible to meeting listing. Nothing deletes `reports/`, `action items/` or `facts/` folders in the operator's vault, and `RESERVED_PROJECT_DIR_NAMES` in `crates/vault/src/paths.rs` continues to exclude all three names (including `"reports"`) from `list_meetings`, so legacy folders are never misread as meetings.
- **FR-7** (should): User-facing copy and module docs no longer promise project reports: `SettingsPage.tsx` lines 147 and 171 ("…and project reports"), `commands.rs:53` / `commands/llm.rs` header / `VaultPanel.tsx:39` doc comments, `services/transcription/README.md` and `docs/*` where the report job is described.

## Non-functional requirements

- **NFR-1**: All four QA gates pass after the removal: `make format`, `make lint` (including `cargo clippy --workspace --all-targets -- -D warnings`), `make type`, `make test`. No `#[allow(dead_code)]`, unused-export, or skipped-test escape hatches are added to make deletion compile.
- **NFR-2**: The frontend bundle contains no dead exports from this feature: `api.ts` and `types.ts` export nothing that no module imports (verified by lint/type gates).

## Acceptance criteria

- **FR-1**:
  - [ ] Launching the app and navigating to a project shows that project's recordings and nothing else: no Action items / Facts / Reports tabs, no "Export project essence" button anywhere.
  - [ ] Opening a recording from the project view still works (existing `onOpen` flow unchanged).
- **FR-2**:
  - [ ] `POST /v1/jobs` with `job_type: "report"` returns a 4xx validation error (pydantic rejects the literal).
  - [ ] `services/transcription/src/transcription/llm/report.py` does not exist; `grep -ri "report_messages\|_report_sync\|collect_project_materials"` over `services/transcription/src` finds nothing.
  - [ ] `llm/summarize.py` still imports and uses `chunk_summary_messages`; the summarize job's tests still pass.
  - [ ] Report-specific tests in `test_llm_jobs.py` are removed; remaining suites pass under `uv run --directory services/transcription pytest -q`.
- **FR-3**:
  - [ ] `grep -ri "export_project_essence\|list_project_reports\|read_report\|reveal_report\|LlmJobKind::Report\|ReportEntry\|list_reports"` over `apps/desktop/src-tauri/src` and `crates/vault/src` finds nothing (excluding unrelated words like "reported").
  - [ ] `cargo clippy --workspace --all-targets -- -D warnings` passes.
- **FR-4**:
  - [ ] `list_project_artifacts`, `read_artifact`, `reveal_artifact` are not registered in `lib.rs` and do not exist in `commands/llm.rs`; `api.ts`/`types.ts` no longer reference `ArtifactView`/`ArtifactContentView`/`ReportView`.
  - [ ] Extracting action items / facts from a recording (the surviving `extract` flow) still compiles and its tests pass — `ArtifactKind` was not over-deleted.
- **FR-5**:
  - [ ] `grep -n '"report"' apps/desktop/src` returns no JobType/label match; `npm --prefix apps/desktop run type` and `run test` pass.
- **FR-6**:
  - [ ] A vault containing `<PROJECT>/reports/260101/report.md` (fixture) lists the project's meetings without a `reports` entry and without errors — the existing `list_meetings` reserved-name test still covers `"reports"`.
  - [ ] No code path added or changed by this feature deletes any file in the operator's vault.
- **FR-7**:
  - [ ] Settings-page copy no longer mentions project reports; no user-visible string references "Export project essence" or report jobs.

## Out of scope

- Deleting or migrating the operator's existing on-disk `reports/`, `action items/`, `facts/` folders (they stay where they are; the operator owns them).
- Per-recording export (`export` job type, `EXPORTS_DIR_NAME`) and its PDF path — that is F4's scope and survives.
- Action-item/fact *extraction* jobs and storage location — F6's scope; extraction from a recording keeps working.
- Artifact front-matter changes (archive status, grouping) — F8, data-only.
- Any replacement UI for browsing artifacts (explicitly none, per the operator).

## Applicable toolkits

- `frontend-toolkit:internal-ui` — React webview UI; single-operator internal tool (`apps/desktop/package.json` names `react`).
- `frontend-toolkit:ui-ux-pro-max` — same UI rows, per the web profile's internal-UI signal.
- `testing-toolkit:python-testing-patterns` — pytest suite at `services/transcription/tests` (desktop + web profiles' Tests row).
- `devops-toolkit:devops-rollout-plan` — packaging/bundle config present (`tauri.conf.json`); relevant only if release notes/installer copy mention reports.

(No Playwright/Cypress, Docker, Django or Postgres signals in this repo, so those profile rows do not apply.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — on every UI task in this feature (web profile mandate for internal-tool UI).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — Q1 resolved at the batch clarification gate (see Decisions log).

## Decisions log

- 2026-08-24 — (OPERATOR, batch gate) Q1: What remains of the project view? → **Keep `ProjectPage` as a dedicated full-window recordings-only page**: breadcrumb + the project's recordings via `VaultList` (or equivalent), reached through the existing "Open project →" button. The tabs, artifact/report panes and "Export project essence" are removed; the page pattern is preserved for future per-project features. FR-1's "surviving mechanism" is this option.

- 2026-08-24 — Does F7 delete report generation? → (OPERATOR, batch intake) Yes: remove the `report` job type, `llm/report.py` and all report UI entirely; F4's PDF fix stays scoped to per-meeting export.
- 2026-08-24 — Scope of the project view after F7 → (OPERATOR, batch intake) Only the recordings list; action-items/facts/reports tabs and "Export project essence" are removed.
- 2026-08-24 — Do artifact-browsing commands survive for F8? → (AUTO: intake decision "F8 is data-only, no in-app UI") No; they lose their only caller and are deleted per "dead code removed, not stranded".
- 2026-08-24 — What happens to existing `reports/` folders on disk? → (AUTO: codebase — `RESERVED_PROJECT_DIR_NAMES` exists precisely to shield non-meeting folders; deleting user data is never implied by a UI removal) Left untouched; the `"reports"` listing exclusion stays.
- 2026-08-24 — Internal or public UI? → (AUTO: desktop+web profiles — single-operator local Tauri app, no public surface) Internal; `frontend-toolkit:internal-ui` is mandatory on UI tasks.
- 2026-08-24 — Does `chunk_summary_messages` go with the report module? → (AUTO: codebase — `llm/summarize.py:13,33` uses it) No; only `report_messages` is report-only.
