---
slug: service-log-original-file-name
created: 2026-08-24
status: approved
---

# Spec: Service log shows the original file name

## Summary

Every row of the Service log (the durable jobs ledger panel) currently labels transcription jobs `source.mp4` / `source.m4a`, because the vault renames every ingested recording to `source.<ext>` inside its meeting folder. The operator cannot tell which recording a row is about. This feature records the recording's original file name at submit time and shows it in the ledger panel, with a sensible derived fallback for rows that predate the change.

## Problem & context

- On ingest, `crates/vault` files a dropped recording as `<project>/<date> - <title>/source.<ext>` (sorted) or `unsorted/<ingest date> - <sanitized stem>/source.<ext>` (unsorted) — see `crates/vault/src/ingest.rs` (`compute_destination`, `Destination.base_name`). The original file name survives only as the meeting folder's name (transformed), never verbatim.
- The desktop submits the transcription job with `audio_path = .../source.<ext>` (`apps/desktop/src-tauri/src/jobs.rs`, `process_one` → `SubmitRequest`); the Python service inserts that into the ledger's `source_path` column (`services/transcription/src/transcription/jobs.py` line ~362, `ledger.py`).
- The panel `apps/desktop/src/components/LedgerPanel.tsx` (`fileNameOf`) renders the last path component of `source_path` — so every transcribe row reads `source.<ext>`.
- The original dropped path is known in `apps/desktop/src-tauri/src/jobs.rs` (`PendingWork::Ingest { source_path }`; `new_pending_snapshot` already derives `file_name` from it for the session Jobs panel) but is never forwarded to the service.
- **Unused persistence path already exists**: `JobCreate.meeting: dict | None` (`services/transcription/src/transcription/schema.py` line 138) is accepted by `POST /v1/jobs` (`app.py` line 215), stored verbatim in the ledger's `meeting_json` column (`jobs.py` line 365, `ledger.py` DDL line 50), and `GET /v1/jobs` returns the full row (`SELECT *`) — `meeting_json` already crosses the wire and is merely dropped by the Rust deserializer (`apps/desktop/src-tauri/src/service/http.rs`, `LedgerJobResponse`). Nothing populates or reads `meeting` today (desktop `SubmitBody` omits it; `cli.py` never posts jobs). This feature can therefore ship with **zero Python service changes and no ledger schema migration**.

## Users

The single operator of the Transcriber desktop app, opening the Service log tab to answer "what did the service do to which recording, and what went wrong".

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists (Tauri).
- `web` — `apps/desktop/package.json` names `react` and `vite` (webview UI).
- `cli` — `services/transcription/pyproject.toml` has `[project.scripts]` (`transcription-service = "transcription.cli:main"`). Matches additively; this feature does not touch the CLI surface.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 (Rust, tokio) | `apps/desktop/src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml` |
| Frontend | React 18 + Vite 5 + TypeScript | `apps/desktop/package.json` |
| Backend service | Python FastAPI transcription service | `services/transcription/src/transcription/app.py`, `pyproject.toml` |
| Database | SQLite job ledger (WAL), `SCHEMA_VERSION = 2` | `services/transcription/src/transcription/ledger.py` |
| Shared library | Rust `vault` crate (ingest/naming rules) | `crates/vault/src/ingest.rs` |
| Testing | cargo test (wiremock), Vitest, pytest | `apps/desktop/src-tauri/src/service/http.rs` tests, `apps/desktop/src/components/LedgerPanel.test.tsx`, `services/transcription/pyproject.toml` |

Makefile QA targets present: format, lint, type, test (all four).

## Functional requirements

- **FR-1** (must): When the desktop submits a `transcribe` job for a freshly ingested recording, it sends the recording's **original file name** (the dropped file's base name, e.g. `ELS - 260812 - Security issue.mp4`) in the `POST /v1/jobs` body's existing `meeting` object (e.g. `{"original_file_name": ...}`). The service persists it unchanged in the existing `meeting_json` ledger column — no Python code or schema change.
- **FR-2** (must): The Service log row's file label renders the recorded original file name whenever the row's `meeting_json` carries one, instead of `source.<ext>`. The read path (`http.rs` `LedgerJobResponse` → `mod.rs` `LedgerJob` → `commands/ledger.rs` `LedgerJobView` → `types.ts` → `LedgerPanel.tsx`) surfaces the value; parsing `meeting_json` happens once on the Rust side so the panel stays presentational.
- **FR-3** (must): Rows **without** a recorded original name (all pre-feature rows; any future row whose `meeting_json` lacks the key) fall back to a derived display name: when the `source_path` base name matches `source.<ext>`, show `<meeting folder name>.<ext>` (e.g. `260812 - Security issue.mp4`); otherwise keep today's base-name behavior (LLM/derived-job rows, whose `source_path` is a meeting or project directory, are unchanged). Path splitting handles both `\` and `/`, as `fileNameOf` does today.
- **FR-4** (must): The full `source_path` remains available exactly as today (the row's `title` tooltip); the job id, status, and all other row fields are unchanged.
- **FR-5** (must): A retranscribe of an already-filed recording (`PendingWork::Filed` in `jobs.rs`, where only `source.<ext>` exists on disk) does **not** invent an original name: it omits the field and the row falls back per FR-3. `source.<ext>` is never recorded as an "original file name".
- **FR-6** (must): No ledger schema migration: `SCHEMA_VERSION` stays 2, `_MIGRATIONS` gains no entry, and existing ledger databases open and render unchanged. A ledger row lacking `meeting_json` (or an older service omitting the key) deserializes as absent via `#[serde(default)]`, matching every other optional row field.

## Non-functional requirements

- **NFR-1**: Wire compatibility both ways — the new app against the current service works fully (the `meeting` field already exists in `JobCreate`); the new app reading rows that lack `meeting_json` degrades to FR-3's fallback, never to an error.
- **NFR-2**: A malformed or unexpected `meeting_json` value (not JSON, wrong shape, non-string name) never breaks the panel — the row renders via the FR-3 fallback.
- **NFR-3**: Windows is the primary platform; display-name derivation must be correct for `\`-separated absolute paths (and keep working for `/`, as the current code does).

## Acceptance criteria

- **FR-1**:
  - [ ] The `submit_posts_exact_body_keys...` wiremock test (extended) shows the `POST /v1/jobs` body carrying the original file name for an ingest-originated job, alongside `audio_path`/`output_dir`/`language`.
  - [ ] After dropping `ELS - 260812 - Security issue.mp4` into a running app, `GET /v1/jobs` returns that row with `meeting_json` containing `"original_file_name": "ELS - 260812 - Security issue.mp4"`.
  - [ ] `git diff` shows no changes under `services/transcription/` for this feature.
- **FR-2**:
  - [ ] Vitest: a `LedgerJobView` row with a recorded original name renders that name — not `source.mp4` — in the row head.
- **FR-3**:
  - [ ] Vitest: a row with no recorded name and `source_path` = `C:\...\ELS\260812 - Security issue\source.mp4` renders `260812 - Security issue.mp4`.
  - [ ] Vitest: a row whose `source_path` base name is not `source.<ext>` (e.g. an LLM job pointing at a meeting directory) renders exactly what it renders today.
  - [ ] Vitest: `meeting_json` of `"not json"` (and of `{}`) renders via the fallback with no thrown error (NFR-2).
- **FR-4**:
  - [ ] Vitest: the row's `title` attribute still equals the full `source_path`.
- **FR-5**:
  - [ ] Rust test: a `PendingWork::Filed` submission produces a request body without an original-file-name value.
- **FR-6**:
  - [ ] `services/transcription` test suite passes with zero modifications; `SCHEMA_VERSION` remains 2.
  - [ ] Rust test: a `GET /v1/jobs` response element without a `meeting_json` key deserializes successfully (serde default).
- **App-level check** (desktop profile verification): launch the app (`tauri dev`), drop a recording, open the Service log — the new row shows the dropped file's name; a pre-existing row shows its meeting-folder-derived name.

## Out of scope

- Backfilling `meeting_json` for historic ledger rows (the original name was never persisted; FR-3's display-time fallback covers them — see Decisions log).
- Renaming `source.<ext>` files in the vault or changing any `crates/vault` naming rule.
- The session Jobs panel (`jobs.rs` `JobSnapshot.file_name` already shows the original dropped name).
- Sending `meeting` metadata from the service CLI (`cli.py`), or reading `meeting_json` anywhere else.
- Any interaction with F6/F7's artifact-location changes; LLM-job row labels keep today's behavior.

## Applicable toolkits

- `frontend-toolkit:internal-ui` — webview UI layer; React + Vite in `apps/desktop/package.json`, single-operator (internal) tool.
- `frontend-toolkit:ui-ux-pro-max` — same UI row of the `web` profile.
- `testing-toolkit:python-testing-patterns` — pytest in `services/transcription/pyproject.toml` (regression safety; no Python changes expected).
- `devops-toolkit:devops-rollout-plan` — packaging/bundle config in `apps/desktop/src-tauri/tauri.conf.json`.

(No Playwright/Cypress, Docker, Django, or PostgreSQL signals present — those profile rows are omitted.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (carried from the `web` profile; the `desktop` profile defers to it for webview UI).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — see Decisions log.

## Decisions log

- 2026-08-24 — Where is the original name persisted? → (AUTO: codebase) Reuse the existing, currently unused `JobCreate.meeting` → `meeting_json` path (`schema.py:138`, `jobs.py:365`, `ledger.py:50`); `GET /v1/jobs` already returns the column. No new ledger column, no `SCHEMA_VERSION` bump, zero Python changes — per the intake directive to prefer already-persisted metadata over new columns.
- 2026-08-24 — Backfill old rows? → (AUTO: data availability) No. The original name was never recorded, so a DB backfill is impossible; but the meeting folder name (derived from the original name by `crates/vault`) is recoverable from `source_path` at render time, so old rows get the FR-3 display fallback instead.
- 2026-08-24 — Internal-tool vs public-facing UI (the `web` profile's standing question)? → (AUTO: desktop profile) Internal: a single-operator local desktop app; `frontend-toolkit:internal-ui` applies.
- 2026-08-24 — What does a retranscribe row show? → (AUTO: codebase) `PendingWork::Filed` has no original name on disk (only `source.<ext>`), so it records nothing and falls back to the meeting-folder-derived name; recording `source.<ext>` as "original" would be the log lying.
