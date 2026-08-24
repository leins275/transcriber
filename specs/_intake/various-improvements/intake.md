---
batch: various-improvements
source: specs/_intake/various-improvements/source.md
created: 2026-08-24
status: approved
---

# Intake: Various improvements

## Source

`specs/_intake/various-improvements/source.md` — a raw operator checkbox list of nine improvement tasks for the Transcriber desktop app, dated 2026-08-24. Plain markdown, no embedded or linked media; `specs/_intake/various-improvements/media/` is empty and nothing needed extracting or converting, so the source file is also the reading copy. All nine items are unchecked and all nine are in scope. Each candidate below carries a `Code surfaces` line (additive to the template) binding the operator's wording to the files that actually implement it, since the source text names no code.

## Features

### [x] F1: Remove cloud LLM support  (slug: remove-cloud-llm-support)

**Task text** (verbatim from the source):

> Remove cloud LLM support. We'll utilize only local models.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/llm/openai_compat.py`, `llm/__init__.py` (provider registry), `config.py` (`llm_provider`, `llm_base_url`, `llm_api_key`, and the cloud-STT keys `provider`, `cloud_model`, `provider_api_key`, `max_cloud_upload_mb`, `_SECRET_KEYS`), `providers/litellm_cloud.py` + `providers/__init__.py`, `services/transcription/tests/test_provider_cloud.py` / `test_llm_units.py` / `test_config.py`, `services/transcription/pyproject.toml`, `services/transcription/README.md`, `docs/config-contract.md`.

### [x] F2: Follow the recording's language (Russian or English)  (slug: transcript-language-selection)

**Task text** (verbatim from the source):

> My english language recording was transcribed in russian. Can we follow the language? Basically could be two options - russian or english.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/config.py` (`language: str | None = None`, i.e. today's unconstrained autodetect), `providers/local_whisper.py` (`decode_kwargs["language"]`, `language_out`/`language_probability` at lines ~250–338), `providers/base.py`, `schema.py` (`JobCreate.language`), `cli.py` (`--language`), `apps/desktop/src-tauri/src/service/http.rs` (the `language` field on `POST /v1/jobs`), `apps/desktop/src/components/SettingsPage.tsx` (no language control exists yet).

### [x] F3: Facts & action items follow the transcript language  (slug: artifact-language-follows-transcript)

**Task text** (verbatim from the source):

> Facts & action items should follow the transcript language

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/llm/prompts.py` (`_LANGUAGE_RULE`, lines 18–21, applied to the summarize/map/reduce/action-item/fact prompts — today a soft instruction only), `llm/extraction.py`, `jobs.py` (`_extract_sync` / `_summarize_sync`, which already load `transcript.json` and its `language` field via `_load_transcript_lines`), `services/transcription/tests/test_llm_jobs.py`.

### [x] F4: Fix PDF rendering and Cyrillic output  (slug: pdf-cyrillic-rendering)

**Task text** (verbatim from the source):

> PDF renders badly, russian encoding is fucked up.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/pdf.py` (the whole module: `_register_fonts` Arial-from-`%WINDIR%\Fonts` registration with a Latin-only Helvetica fallback, `_BASE_CSS`, `render_pdf` via `markdown` + `xhtml2pdf`/reportlab, `link_callback`), its consumers `llm/report.py` and `exporting.py`, `services/transcription/pyproject.toml` (PDF backend choice).

### [x] F5: Service log shows the original file name  (slug: service-log-original-file-name)

**Task text** (verbatim from the source):

> In service log, we have to write the original file name, now it shows all as source, that's correct, but not clear.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/ledger.py` (the `jobs` table DDL, `SCHEMA_VERSION = 2` and its migration path, `source_path`/`meeting_json` columns), `schema.py` (`JobCreate`), `jobs.py` (job record creation), `apps/desktop/src-tauri/src/service/http.rs` + `service/mod.rs` (`LedgerRow`), `apps/desktop/src/types.ts` (`LedgerJobView`), `apps/desktop/src/components/LedgerPanel.tsx` (`fileNameOf`, which renders `source.<ext>` for every row because the vault stores recordings as `source.<ext>`), `apps/desktop/src-tauri/src/ingest.rs` (where the original file name is still known).

### [x] F6: Store action items and facts under the sync folder  (slug: artifacts-in-sync-folder)

**Task text** (verbatim from the source):

> Action items and facts should be stored under the sync folder

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `crates/vault/src/paths.rs` (`ACTION_ITEMS_DIR_NAME = "action items"`, `FACTS_DIR_NAME = "facts"`, `REPORTS_DIR_NAME`, `RESERVED_PROJECT_DIR_NAMES`), `crates/vault/src/artifacts.rs`, `services/transcription/src/transcription/artifacts.py` (the mirrored cross-language directory-name contract, `write_item`, `list_items`), `jobs.py` (`_extract_sync`, which writes to `job.output_path`), `apps/desktop/src-tauri/src/commands/llm.rs` (`require_project_dir`, which today refuses extraction for `unsorted/` meetings because artifacts need a project folder), `apps/desktop/src-tauri/src/config.rs` (`meetings_root`; no "sync folder" setting exists yet), `services/transcription/src/transcription/llm/report.py` + `exporting.py` (readers of the same locations).

### [x] F7: Project view shows recordings only  (slug: project-view-recordings-only)

**Task text** (verbatim from the source):

> In project view, let's support only show recordings view and that's it, no essence of the project stuff. I'll do it outside better.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `apps/desktop/src/components/ProjectPage.tsx` + `ProjectPage.module.css` (the action-items/facts/reports tabs and the "Export project essence" button), `apps/desktop/src/App.tsx` (lines ~86, ~428–528: the open-project page state, `essenceBusy`, `handleExportEssence`), `apps/desktop/src/components/VaultPanel.tsx` / `VaultList.tsx` / `lib/vaultGroups.ts` (the recordings listing that stays), `apps/desktop/src-tauri/src/commands/llm.rs` (`list_project_artifacts`, `read_artifact`, `reveal_artifact`, report commands — which of these lose their only caller), `apps/desktop/src/api.ts`, `apps/desktop/src/types.ts` (`ArtifactView`, `ReportView`).

### [x] F8: Archive status and source grouping for action items  (slug: action-item-archive-grouping)

**Task text** (verbatim from the source):

> Archive status for action items + grouping by the source

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `services/transcription/src/transcription/artifacts.py` (front matter is the only per-item state today — `render_front_matter` / `parse_front_matter` / `StoredItem`; `jobs.py` lines ~931–942 write `source_project`, `source_meeting`, `source_recording`, so the grouping key already exists in the data), `crates/vault/src/artifacts.rs`, `apps/desktop/src-tauri/src/commands/llm.rs` (`ArtifactView` listing; nothing can currently mutate an artifact), `apps/desktop/src/types.ts`, and whichever UI surface survives F7. Nothing named "archive" exists anywhere in the app today.

### [x] F9: Manual speaker markup on a selected part of the transcript  (slug: transcript-manual-speaker-markup)

**Task text** (verbatim from the source):

> Sometimes I'm looking at the transcript, and see that in the middle of the paragraph one of the sentencies was spoken by another person. It'll be nice to be albe to select part of the transcript and manually markup it.

**Attachments**:

- None — the source document has no media.

**Code surfaces**: `apps/desktop/src/components/TranscriptViewer.tsx` (turn rendering, `persist`, `onSaveSpeakers`), `apps/desktop/src/lib/turns.ts` (`groupIntoTurns`, `assignSpeaker`, `renameSpeaker`, `filterTurns`), `apps/desktop/src/components/SpeakerTag.tsx`, `apps/desktop/src-tauri/src/commands/meetings.rs` (`speakers.json`: `SPEAKERS_FILE_NAME`, `SPEAKERS_SCHEMA_VERSION = 1`, `read_speaker_labels`, and the `segment id -> speaker name` map that is the current granularity limit), `apps/desktop/src/types.ts` (`TranscriptSegmentView`, `TranscriptView.speakers`), `services/transcription/src/transcription/exporting.py` (`load_speaker_overrides`, consumed by every LLM job through `render_transcript_lines`).

## Unassigned content

None — all nine checkbox items are assigned to a feature candidate, and the document contains no other text or media.

## Decisions log

- (AUTO: project memory "local-only direction — no cloud LLM or STT, ever") How far does "remove cloud" go? → F1 removes BOTH the cloud LLM client (`llm/openai_compat.py`) and the cloud STT provider (`providers/litellm_cloud.py`) plus all their config keys (`provider=cloud`, `cloud_model`, `provider_api_key`, `max_cloud_upload_mb`, `llm_provider`, `llm_base_url`, `llm_api_key`).
- (OPERATOR) What is the "sync folder" (F6)? → The per-recording folder: the meeting's own directory where summary, transcript etc. already live. Action items and facts must be written there, alongside the other per-meeting outputs — not into project-level `action items/` / `facts/` trees.
- (OPERATOR) Where does F8 live after F7 strips the project view? → Data only: `archived` status and source-grouping metadata live in artifact front matter; no in-app UI — consumed by the operator's external tools.
- (OPERATOR) Does F7 delete report generation? → Yes, remove the `report` job type, `llm/report.py` and all report UI entirely. F4 (PDF/Cyrillic fix) stays scoped to the per-meeting export path.
- (OPERATOR) Split approved: 9 features, order F1→F9.
