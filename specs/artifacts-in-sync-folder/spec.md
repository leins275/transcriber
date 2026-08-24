---
slug: artifacts-in-sync-folder
created: 2026-08-24
status: approved
---

# Spec: Store action items and facts under the recording's own folder

## Summary

Extracted action items and facts currently land in project-level trees (`<PROJECT>/action items/<slug>/`, `<PROJECT>/facts/<slug>/`). The operator wants them stored "under the sync folder", which the batch intake pinned as the per-recording (meeting) folder — the directory where that recording's `transcript.json`, `summary.md` and `exports/` already live. This feature relocates the write target for both artifact kinds to `<meeting>/action items/` and `<meeting>/facts/`, makes extraction available for `unsorted/` recordings (no project required anymore), and points the readers at the new location.

## Problem & context

- Extraction jobs write to `<PROJECT>/<kind>/<slug>/` — the contract is pinned on both sides of the language boundary: `crates/vault/src/paths.rs` (`ACTION_ITEMS_DIR_NAME = "action items"`, `FACTS_DIR_NAME = "facts"`, `RESERVED_PROJECT_DIR_NAMES`) and `services/transcription/src/transcription/artifacts.py` (mirrored constants, `write_item`, `list_items`). Both sides must change together.
- Because artifacts need a project folder, `require_project_dir` in `apps/desktop/src-tauri/src/commands/llm.rs` (line 170) refuses extraction for `unsorted/` meetings, and `RecordingPage.tsx` (line ~217) disables the Action items / Facts buttons for unsorted recordings. With per-meeting storage there is no reason to refuse.
- The per-recording export (`services/transcription/src/transcription/exporting.py`, `items_for_meeting`) reads project-level items and filters them by the `source_meeting` front-matter key; `jobs.py` `_export_sync` (line ~1008) special-cases unsorted meetings to skip items entirely. Both must follow the new location.
- `jobs.py` `_extract_sync` (line ~895) derives the `source_project` front-matter value as `Path(job.output_path).parent.name` — under the new layout that expression would yield the meeting name, so the derivation must change.
- The operator's meeting folders sync externally (hence "sync folder"); artifacts stored per-meeting travel with the recording when it is filed, renamed or synced. Sibling F7 (separate spec) removes the in-app project artifact browsing; sibling F8 (separate spec) extends artifact front matter. F6 is storage location only.

## Users

- The single operator of this local-only desktop app: runs extraction from the recording page, reads the resulting `.md` files via the per-recording export and via external tools over the synced meeting folders.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists; `tauri` dependencies in `apps/desktop/package.json`.
- `web` — `apps/desktop/package.json` names `react` 18 and `vite` (webview UI; per the desktop profile, UI toolkits come from `web`, process/IPC rules from `desktop`).

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 (Rust) | `apps/desktop/src-tauri/tauri.conf.json`, `src-tauri/src/lib.rs` |
| Frontend | React 18 + Vite + TypeScript | `apps/desktop/package.json` |
| Vault/domain (Rust) | `vault` crate | `crates/vault/src/paths.rs`, `crates/vault/src/artifacts.rs` |
| Backend service | Python 3 sidecar (FastAPI-style HTTP jobs) | `services/transcription/pyproject.toml`, `src/transcription/jobs.py` |
| Frontend tests | Vitest + Testing Library | `apps/desktop/package.json` (`vitest run`) |
| Python tests | pytest (+ pytest-asyncio) | `services/transcription/pyproject.toml` |
| Rust tests | built-in `#[cfg(test)]` | `crates/vault/src/artifacts.rs`, `commands/llm.rs` tests |

Makefile QA targets present: format, lint, type, test (all four).

## Functional requirements

- **FR-1** (must): Action-item and fact extraction writes items to `<meeting>/action items/<slug>/` and `<meeting>/facts/<slug>/` — inside the recording's own folder, alongside `transcript.json`, `summary.md` and `exports/`. The directory names remain the existing cross-language constants (`ACTION_ITEMS_DIR_NAME`, `FACTS_DIR_NAME`); only their anchor moves from the project folder to the meeting folder. Both sides of the contract (`crates/vault/src/paths.rs` docs/constants and `services/transcription/src/transcription/artifacts.py`) are updated together, and the tests that pin the contract are updated on both sides.
- **FR-2** (must): Extraction works for any meeting with a transcript, including `unsorted/` recordings: `require_project_dir` and its refusal are removed from `extract_vault_entry_handler` (`apps/desktop/src-tauri/src/commands/llm.rs`), and the Action items / Facts buttons in `RecordingPage.tsx` are no longer disabled for unfiled recordings (the "file under a project first" tooltip goes away).
- **FR-3** (must): Item front matter still records provenance: `source_meeting` stays the meeting folder name; `source_project` is derived from the meeting folder's parent (`meeting_dir.parent.name`), written as `null` for `unsorted/` meetings (`jobs.py` `_extract_sync`, replacing the `Path(job.output_path).parent.name` derivation). No other front-matter changes (F8 owns new fields).
- **FR-4** (must): The per-recording export reads items from the meeting folder: `exporting.py` `items_for_meeting` (or its replacement) lists `<meeting>/<kind>/` directly — the `source_meeting` filter and the `project_dir` parameter become unnecessary — and `_export_sync` drops its unsorted special case. Exports of unsorted recordings include their extracted items.
- **FR-5** (must): The vault listing still never mistakes an artifact directory for a meeting: `RESERVED_PROJECT_DIR_NAMES` keeps excluding legacy project-level `action items/`, `facts/`, `reports/` directories from `list_meetings`, regardless of the migration decision. (Meeting-level artifact dirs need no exclusion — the listing never recurses into meeting folders, same as `exports/`.)
- **FR-6** (must): Existing project-level artifacts are handled per the migration decision (Open question Q1). Whatever the answer, nothing is silently deleted.
- **FR-7** (won't, recorded for clarity): The project-page artifact browsing commands (`list_project_artifacts`, `read_artifact`, `reveal_artifact` and the Rust `vault::list_project_artifacts`) are not reworked to aggregate across meeting folders. Sibling F7 (approved in this batch) deletes that UI and those commands; in the interim the project page may show only legacy project-level items. (AUTO: batch Decisions log — avoiding throwaway work.)

## Non-functional requirements

- **NFR-1**: The Windows 260-character path budget still holds: `fit_slug` in `artifacts.py` is called with the (deeper) meeting-level parent, so `<meeting>/<kind>/<slug>/<slug>.md` plus the longest screenshot sibling fits within 260 characters, trimming the slug as needed — covered by a test using a realistically deep meeting path.
- **NFR-2**: Artifact writes remain atomic (images first, then the `.md` via `write_text_atomic`) — the relocation must not weaken the existing crash-safety ordering.
- **NFR-3**: All four `make` QA targets (format, lint, type, test) pass on both the Rust and Python sides after the change.

## Acceptance criteria

- **FR-1**:
  - [ ] Running "Action items" on a filed recording creates `<root>/<PROJECT>/<meeting>/action items/<slug>/<slug>.md` (and screenshots); nothing new appears under `<root>/<PROJECT>/action items/`.
  - [ ] Same for "Facts" into `<meeting>/facts/`.
  - [ ] The llm.rs test `extraction_targets_the_project_level_artifact_directory` is replaced by one asserting the job's `output_dir` is `<meeting>/<kind>`.
  - [ ] Contract tests on both sides (Rust + Python) pin the meeting-level location and the unchanged directory-name strings.
- **FR-2**:
  - [ ] Extraction on an `unsorted/` meeting with a transcript enqueues a job and produces items in `unsorted/<meeting>/<kind>/…` (the old refusal test `extraction_on_an_unsorted_meeting_is_refused_with_an_actionable_message` is replaced by a success test).
  - [ ] In the UI, an unfiled recording's Action items / Facts buttons are enabled (given a transcript) — Vitest assertion on `RecordingPage`.
  - [ ] A meeting without a transcript is still refused with the existing "transcribe it first" message.
- **FR-3**:
  - [ ] An item extracted from `<root>/ELS/<meeting>/` carries `source_project: "ELS"`, `source_meeting: "<meeting>"`.
  - [ ] An item extracted from `<root>/unsorted/<meeting>/` carries `source_project: null`.
- **FR-4**:
  - [ ] A per-recording export of a meeting with meeting-level items includes them under "Action items" / "Facts", with screenshot links resolving relative to the export dir.
  - [ ] A per-recording export of an unsorted meeting with items includes them (no more empty-section special case).
- **FR-5**:
  - [ ] A vault containing legacy `<PROJECT>/action items/` and `<PROJECT>/facts/` directories lists no meeting named "action items"/"facts" (existing vault-crate tests keep passing).
- **FR-6**:
  - [ ] Behavior matches the Q1 decision; in no branch is a legacy artifact file deleted.

## Out of scope

- Removing project-artifact browsing UI/commands and report generation (`llm/report.py`) — sibling F7.
- New front-matter fields (`archived`, grouping metadata) — sibling F8.
- Artifact language (F3), PDF rendering (F4), any change to `exports/` location or the `reports/` tree.
- Any new "sync folder" setting in `config.rs` — the operator confirmed the sync folder *is* the meeting folder; no new configuration is introduced.

## Applicable toolkits

- `frontend-toolkit:internal-ui` — webview UI (React 18 + Vite in `apps/desktop/package.json`); single-operator internal tool.
- `frontend-toolkit:ui-ux-pro-max` — same UI signal.
- `testing-toolkit:python-testing-patterns` — pytest in `services/transcription/pyproject.toml`.
- `devops-toolkit:devops-rollout-plan` — packaging/bundle config (`apps/desktop/src-tauri/tauri.conf.json`, updater plugin).

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — on every UI task (the `web` profile mandates it for internal-tool UI; the only UI touch here is the RecordingPage button ungating).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — Q1 resolved at the batch clarification gate (see Decisions log).

## Decisions log

- 2026-08-24 — (OPERATOR, batch gate) Q1: Existing project-level artifacts → **leave in place, stop reading**. No migration code; files stay on disk for external tools; re-run extraction where still needed. FR-6 resolves to: readers switch to the meeting-level location only, legacy files are never touched or deleted.

- 2026-08-24 — (OPERATOR, batch intake) "Sync folder" = the per-recording meeting folder; artifacts live alongside transcript/summary, not in project-level trees.
- 2026-08-24 — (AUTO: codebase contract) Keep the exact directory-name strings `action items` / `facts` and the `<slug>/<slug>.md` item shape; only the anchor directory moves. Preserves the cross-language contract style, the slug/260-char machinery, and external-tool recognizability.
- 2026-08-24 — (AUTO: batch Decisions log, F7 approved in same batch) Do not rework project-page artifact browsing to aggregate meeting-level items — F7 deletes that surface; interim staleness accepted.
- 2026-08-24 — (AUTO: `vault::list` semantics) `RESERVED_PROJECT_DIR_NAMES` keeps excluding the legacy names from the meeting listing; meeting-level artifact dirs need no exclusion because the listing never recurses (same rule as `EXPORTS_DIR_NAME`).
- 2026-08-24 — (AUTO: web profile) UI classified as internal-tool (single-operator desktop app), so `frontend-toolkit:internal-ui` is the mandatory UI skill; no public-facing UI exists.
