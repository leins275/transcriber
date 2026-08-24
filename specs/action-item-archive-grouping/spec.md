---
slug: action-item-archive-grouping
created: 2026-08-24
status: approved
---

# Spec: Archive status and source grouping for action items

## Summary

Add an `archived` flag (default `false`) to the front matter of every action-item and fact artifact the LLM extraction job writes, and guarantee a complete, stable set of `source_*` grouping fields so the operator's external tools (Obsidian and similar) can group items by their source recording. This is a pure data-model feature: there is no in-app UI for it (operator decision); archiving is toggled by editing the file in an external editor, and the app must tolerate and never destroy hand-edited front matter.

## Problem & context

Action items extracted from meetings accumulate with no way to mark them "done/archived", and grouping them by origin outside the app relies on front matter that is only partially fit for the purpose. Today:

- `services/transcription/src/transcription/jobs.py` (`_extract_sync`, lines ~931–942) writes `source_project`, `source_meeting`, `source_recording` plus `type`/`kind`, `title`, `timestamps`, `created`, `model`, `job_id`, `screenshots`. Nothing named "archive" exists anywhere in the repo.
- `source_recording` is always the literal stored filename `source.<ext>` (the vault stores every recording as `source.<ext>`; `crates/vault/src/lib.rs` layout docs), so it identifies nothing by itself. The meeting folder name `<YYMMDD> - <original stem>` is the real identity carrier.
- `source_project` is derived as `Path(job.output_path).parent.name` — a derivation that breaks when sibling **F6** (`artifacts-in-sync-folder`) moves artifacts from `<PROJECT>/action items/` into the per-meeting folder. This spec targets the **post-F6 layout**.
- Front matter is written as `key: <json value>` lines by `render_front_matter` and read best-effort by `parse_front_matter` / `StoredItem` / `list_items` (`services/transcription/src/transcription/artifacts.py`); the Rust side (`crates/vault/src/artifacts.rs`) lists item folders but does not parse front matter. The directory-name contract is already pinned cross-language; the front-matter field contract is not.
- Sibling **F7** (`project-view-recordings-only`) removes the project-view artifact UI, which is why this feature is data-only: whatever survives in the app merely writes and passively reads these files.

## Users

- **The operator**, working in external tools (Obsidian or any YAML-front-matter-aware editor): groups items by source fields, toggles `archived` by editing the file.
- **The transcription service** (writer): stamps the contract fields on every new item.
- **Downstream in-app readers** that survive F6/F7 (per-meeting export in `exporting.py`): must keep working against edited files, without acting on `archived`.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists (Tauri), `[dependencies] tauri` in the app's Cargo manifest.
- `web` — `apps/desktop/package.json` names `react` (^18.3.1) and `vite` (^5.4.10); the Tauri UI is a webview, so both profiles apply per the desktop profile's own rule.

Note: although both UI-bearing profiles match the repository, **this feature deliberately has no UI surface** (binding operator decision). The touched layers are the Python service and, for the contract, the Rust vault crate.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Service backend | Python 3 service (`transcription` package) | `services/transcription/pyproject.toml`, `src/transcription/jobs.py` |
| Desktop shell | Tauri 2 (Rust) | `apps/desktop/src-tauri/tauri.conf.json` |
| Frontend | React 18 + Vite 5 (webview) | `apps/desktop/package.json` |
| Vault core | Rust crate `vault` | `crates/vault/src/artifacts.rs`, `paths.rs` |
| Testing | pytest (+ pytest-asyncio); `cargo test` in crates | `services/transcription/pyproject.toml` (`[tool.pytest.ini_options]`), `crates/vault/src/artifacts.rs` `#[cfg(test)]` |

Makefile QA targets present: format, lint, type, test (all four resolve via `make -n`).

## Functional requirements

- **FR-1** (must): Every newly written extraction item — both action items (`type`) and facts (`kind`), since both flow through the single writer `_extract_sync` → `artifacts.write_item` — carries `archived: false` in its front matter, written explicitly (not implied by absence) so external property editors surface a toggleable field.
- **FR-2** (must): Source grouping fields are correct under the post-F6 per-meeting layout:
  - `source_meeting`: the meeting folder's name (unchanged semantics).
  - `source_project`: the vault project folder containing the meeting; JSON `null` when the meeting lives under the reserved `unsorted/` root (`crates/vault/src/paths.rs` `UNSORTED_DIR_NAME`) — never the literal string `"unsorted"` posing as a project.
  - `source_recording`: the actual stored recording filename (`source.<ext>`), `null` when no source file is found (unchanged semantics).
  - Field names stay as they are today — no renames (see Decisions log).
- **FR-3** (must, per batch-gate decision): A `source_date` field, ISO `YYYY-MM-DD`, parsed from the meeting folder's leading `YYMMDD` (per the vault naming contract `<YYMMDD> - <stem>`); JSON `null` when the folder name does not carry a parseable date. Gives external tools a real date property for grouping/sorting, matching the ISO convention of the existing `created` field.
- **FR-4** (must): Hand-edited front matter is tolerated and preserved. Given a file whose front matter was rewritten by an external editor (reordered keys, YAML-quoted values, added unknown keys, `archived` flipped to `true`):
  - `parse_front_matter` / `list_items` still read it without error; unknown keys are retained in `StoredItem.meta`; non-JSON scalar values fall back to the raw string (existing behavior, now pinned by tests as part of the contract).
  - The app has **no code path that rewrites an existing artifact's `.md`** after its atomic creation; this absence is part of the contract (a future mutation feature must round-trip unknown keys and the body byte-exactly outside the keys it changes).
- **FR-5** (must): The app's behavior ignores `archived`. Per-meeting export (`exporting.py`) and any surviving artifact listing include archived items exactly like unarchived ones. `archived` is consumed only by the operator's external tools (binding operator decision).
- **FR-6** (must): The front-matter field contract is a documented cross-language contract, like the directory-name contract already is:
  - The field set (names, JSON types, null-ability, semantics) is documented in `services/transcription/src/transcription/artifacts.py`'s module docstring and mirrored in the surviving Rust artifact module's docs (`crates/vault/src/artifacts.rs`, or its post-F6/F7 successor; if F7 deletes the Rust artifact module entirely, the Python-side documentation and tests are the contract's single home and say so).
  - A Python test pins the exact key set `_extract_sync` writes, so an accidental drift fails CI.
  - Any Rust code that reads or writes artifact front matter (now or later) must use these field names verbatim.

## Non-functional requirements

- **NFR-1**: The front-matter block remains a strict YAML subset (`---` fences, `key: <JSON scalar/array>` lines) so Obsidian and generic YAML parsers read it unchanged — verified by round-tripping a written item through a real YAML parser in a test.
- **NFR-2**: `parse_front_matter` never raises on arbitrary text (fuzz-ish test over malformed/edited inputs); a malformed line degrades to a raw-string value or is skipped, never fails the listing.
- **NFR-3**: All artifact writes stay atomic (`write_text_atomic` tmp-file + `os.replace` pattern); no new write path bypasses it.

## Acceptance criteria

- **FR-1**:
  - [ ] Running an `action_items` job writes items whose front matter contains the line `archived: false`.
  - [ ] Running a `facts` job writes items whose front matter contains `archived: false`.
  - [ ] `parse_front_matter` on a written item yields `meta["archived"] is False` (JSON boolean, not string).
- **FR-2**:
  - [ ] An item extracted from `<vault>/<PROJECT>/<YYMMDD - name>/` has `source_project: "<PROJECT>"` and `source_meeting: "<YYMMDD - name>"`.
  - [ ] An item extracted from `<vault>/unsorted/<YYMMDD - name>/` has `source_project: null`.
  - [ ] `source_recording` equals the actual `source.<ext>` filename in the meeting folder; `null` when absent.
- **FR-3**:
  - [ ] A meeting folder named `260824 - standup` yields `source_date: "2026-08-24"`.
  - [ ] A meeting folder without a leading valid `YYMMDD` yields `source_date: null` (job still succeeds).
- **FR-4**:
  - [ ] A test feeds `list_items` a file whose front matter was Obsidian-style rewritten (reordered keys, `archived: true`, an unknown `tags: [x]` key, quoted strings) and asserts every key/value survives into `StoredItem.meta` and the body is intact.
  - [ ] A repo-wide check (test or review gate) confirms no production code path opens an existing artifact `.md` for writing.
- **FR-5**:
  - [ ] Per-meeting export output is byte-identical whether an included item has `archived: true` or `false`.
- **FR-6**:
  - [ ] A pytest asserts the exact set of front-matter keys `_extract_sync` writes (fails on drift).
  - [ ] The field contract (names, types, defaults) is present in `artifacts.py`'s docstring and mirrored (or explicitly ceded to Python with a pointer) in the surviving Rust artifact module.

## Out of scope

- Any in-app UI for archiving, filtering, or grouped display (operator decision; F7 removes the artifact UI).
- App behavior conditioned on `archived` (hiding archived items anywhere in-app or in exports).
- Moving artifacts into the per-meeting folder — that is F6; this spec only targets F6's layout.
- Reports (`llm/report.py`) — deleted by F7.
- Renaming existing `source_*` fields.
- Backfilling/migrating already-written artifacts — unless Q2 resolves otherwise.
- Watching for or reacting to external edits (no file watcher, no sync logic).

## Applicable toolkits

- `testing-toolkit:python-testing-patterns` — service tests; pytest in `services/transcription/pyproject.toml`.
- `devops-toolkit:devops-rollout-plan` — packaging layer; `tauri.conf.json` bundle config (desktop profile Packaging row). Contract change ships inside the app bundle.
- `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max` — React/Vite webview present in the repo (web profile, internal-tool UI: a single-operator desktop app). Listed because the signal is present; **no F8 task should be a UI task**, so these should not attach to any task in this feature's plan.

(No Playwright, Docker, Django, or PostgreSQL signals observed; those rows are dropped.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (carried verbatim from the `web` profile). F8 defines no UI tasks, so it applies only if the plan deviates.

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — Q1 resolved at the batch clarification gate; Q2 auto-resolved (see Decisions log).

## Decisions log

- 2026-08-24 — (OPERATOR, batch gate) Q1: Grouping fields → **add `source_date`** (ISO `YYYY-MM-DD` parsed from the meeting folder's leading `YYMMDD`, `null` when unparseable). FR-3 is promoted to **must**. No composite `source` field.
- 2026-08-24 — (AUTO: consistent with F6's "leave legacy artifacts in place, no migration" operator decision) Q2: Backfill → **new items only**. External tools treat a missing `archived` as false; no migration code is written.

- 2026-08-24 — (OPERATOR, batch intake) Where does F8 live after F7? → Data only: `archived` and source-grouping metadata in artifact front matter; no in-app UI; consumed by external tools.
- 2026-08-24 — (AUTO: operator intake decision) Does the app act on `archived`? → No. Exports and listings ignore it (FR-5); archiving is toggled by external editors and the app's only obligation is tolerance/preservation (FR-4).
- 2026-08-24 — (AUTO: codebase) Does `archived` cover facts too? → Yes. Action items and facts share one writer (`_extract_sync` → `write_item`) and one front-matter contract; forking the schema per kind costs more than the one field it saves.
- 2026-08-24 — (AUTO: codebase convention) Field formats → `archived` written explicitly as JSON `false` (discoverable/toggleable in property editors); any date field is ISO, matching the existing `created` field.
- 2026-08-24 — (AUTO: external compatibility) Keep `source_project` / `source_meeting` / `source_recording` names unchanged — renames would churn the contract without adding grouping power.
- 2026-08-24 — (AUTO: sibling F6 binding decision) Spec targets the post-F6 per-meeting artifact layout; `source_project` derivation moves from `output_path.parent.name` to the meeting folder's position under the vault root.
