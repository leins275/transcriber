# Changelog

Every release of Transcriber, newest first. Generated from conventional
commits by git-cliff — edit the commit messages, not this file.

## 0.18.0 — 2026-09-02

### Features

- Per-vault search index + "Enable vector search" download in Settings

## 0.17.0 — 2026-08-31

### Features

- Chat redesigned as the library's own tab (turn 9)

## 0.16.0 — 2026-08-31

### Features

- Rebuild-search-index button in Settings and a startup catch-up pass

## 0.15.1 — 2026-08-31

### Bug fixes

- Enable tokio's macros feature for the chat stream's select!

## 0.15.0 — 2026-08-31

### Bug fixes

- Canonicalize search/chat hit paths before the vault-index lookup

### Documentation

- Reflect the search/chat/MCP architecture in CLAUDE.md and the library docstring

### Features

- Project page with streaming local-LLM chat over project materials
- Transcriber-mcp — standalone stdio MCP server over the vault
- Hybrid search over the vault — /v1/search endpoint and library search UI
- Hybrid search foundation — vault index job, embeddings, index DB
- Project-level speaker memory with voice-embedding groundwork
- Editable per-meeting note (note.md) on the recording page

## 0.14.0 — 2026-08-30

### Features

- Fold action items into the summary, export into the meeting folder, keep the library filter

## 0.13.1 — 2026-08-27

### Bug fixes

- Run prettier on RecordingPage
- Swap Export PDF and Reveal in Explorer on recording page

## 0.13.0 — 2026-08-25

### Chores

- Landing screenshot with English meeting titles
- Add Astro landing page, deployed to GitHub Pages from CI
- Add MIT license
- Untrack the local scratch folder

### Documentation

- Reflect the extended pipeline and the one-model catalog

### Features

- Auto-export at the end of the drop-to-insights chain
- Pin the assistant to Qwen3.5-9B and remove model switching
- One flat recordings list with optional project grouping

## 0.12.1 — 2026-08-25

### Bug fixes

- Keep the open recording across post-job refreshes, narrate progress in the header

## 0.12.0 — 2026-08-25

### Features

- Action-items-first pipeline with in-app view and factored UI

## 0.11.1 — 2026-08-25

### Bug fixes

- **llm**: Split truncated chunks by their own size; cap item timestamps

## 0.11.0 — 2026-08-25

### Documentation

- Updated readme

### Features

- **llm**: Curated model catalog with Qwen3.5-9B default, share-ready export PDF name

## 0.10.2 — 2026-08-25

### Bug fixes

- Block rename during active jobs, monotone extraction progress, thin vault rows

## 0.10.1 — 2026-08-24

### Bug fixes

- **llm**: Detect output truncation, recover by splitting, and budget for unlimited context

## 0.10.0 — 2026-08-24

### Bug fixes

- **pdf-cyrillic-rendering**: Bridge registered fonts into xhtml2pdf so Cyrillic renders

### Chores

- **sdd**: Batch various-improvements complete — 9/9 features merged
- **sdd**: Factory bookkeeping before batch integration

### Documentation

- **sdd**: Specs and plans for batch various-improvements

### Features

- **transcript-manual-speaker-markup**: Assign a speaker to a selected part of the transcript
- **action-item-archive-grouping**: Archived flag and source_date in artifact front matter
- **project-view-recordings-only**: Recordings-only project page, report machinery removed
- **artifacts-in-sync-folder**: Store action items and facts in the recording's own folder
- **service-log-original-file-name**: Show the recording's original file name in the Service log
- **artifact-language-follows-transcript**: Pin LLM output language to the transcript language
- **transcript-language-selection**: Constrain transcription language to ru/en
- **remove-cloud-llm-support**: Local-only — remove cloud LLM and cloud STT

### Other

- Merge sdd/transcript-manual-speaker-markup (F9)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Merge sdd/action-item-archive-grouping (F8)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

# Conflicts:
#	crates/vault/src/artifacts.rs
#	services/transcription/pyproject.toml
#	services/transcription/src/transcription/jobs.py
#	services/transcription/tests/test_llm_jobs.py
#	services/transcription/tests/test_llm_units.py
- Merge sdd/project-view-recordings-only (F7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

# Conflicts:
#	apps/desktop/src-tauri/src/commands/llm.rs
#	crates/vault/src/artifacts.rs
#	crates/vault/src/lib.rs
#	services/transcription/src/transcription/artifacts.py
#	services/transcription/src/transcription/config.py
#	services/transcription/src/transcription/pdf.py
#	services/transcription/tests/test_llm_jobs.py
- Merge sdd/artifacts-in-sync-folder (F6)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

# Conflicts:
#	services/transcription/tests/test_llm_jobs.py
- Merge sdd/service-log-original-file-name (F5)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

# Conflicts:
#	apps/desktop/src-tauri/src/jobs.rs
- Merge sdd/pdf-cyrillic-rendering (F4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

# Conflicts:
#	services/transcription/tests/test_llm_jobs.py
- Merge sdd/artifact-language-follows-transcript (F3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Merge sdd/transcript-language-selection (F2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Merge sdd/remove-cloud-llm-support (F1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

## 0.9.0 — 2026-08-23

### Features

- **llm**: GPU acceleration for the installed app; keep chain-of-thought out of artifacts

## 0.8.0 — 2026-08-23

### Features

- **llm**: GPU offload — CUDA llama.cpp variant and auto-fit gpu layers

## 0.7.0 — 2026-08-23

### Features

- MacOS Apple Silicon build target — two-installer release pipeline

## 0.6.1 — 2026-08-23

### Bug fixes

- **llm**: Pin the GGUF download to the real ggml-org repo

## 0.6.0 — 2026-08-23

### Features

- Local-LLM jobs — summaries, action items, facts, reports and PDF exports

## 0.5.1 — 2026-08-23

### Bug fixes

- **installer**: Kill the orphaned pyenv sidecar via WMI so updates stop failing on locked DLLs

## 0.5.0 — 2026-08-23

### Build & CI

- Stop cutting releases for commits that are not bump-worthy

### Features

- Speaker diarization with pyannote, surfaced as pre-filled speaker labels

## 0.4.1 — 2026-08-23

### Build & CI

- Gate direct pushes to main with the full CI suite before tagging

### Tests

- **vault**: Make the filename-parse timing test robust to CI runner noise

## 0.4.0 — 2026-08-23

### Bug fixes

- **updater**: Stop the bundled Python sidecar before the installer overwrites pyenv

### Documentation

- Move README content to docs/overview.md

### Features

- **desktop**: No-sidebar redesign with settings page, transient notices, cancelled-job cleanup

## 0.3.0 — 2026-08-22

### Features

- **vault**: Whitespace around the name separators is optional
- **service**: Utterance-level segments -- stop concatenating replicas

## 0.2.1 — 2026-08-22

### Bug fixes

- **ci**: Read the Tauri v2 updater artifact (signed installer, not .nsis.zip)

## 0.2.0 — 2026-08-22

### Bug fixes

- **build**: Sync the Python package's __version__ with version.txt
- **build**: Pick the installer by name, never the first exe in the bundle
- **build**: Sync uv.lock's project version too, or the release cannot build
- **desktop**: Contain paths against either spelling of the meetings root
- **build**: Normalize Cargo.lock after the trash dependency
- **build**: Stop the pyenv bake deleting the tracked .gitkeep
- **service**: Write transcript.json as UTF-8 instead of ascii escapes

### Build & CI

- Replace the standing release PR with a tag-driven release flow
- Stop running the gate twice for every merge
- Run the gate as four parallel jobs instead of one serial one
- Refuse to publish a release without the installer attached
- Widen the eol=lf rule to the whole tree
- Quality gate and conventional-commit release pipeline

### Chores

- **design**: Second Claude Design handoff bundle

### Documentation

- Replace the developer's Windows username with a placeholder
- Smoke steps for the vault-management flows

### Features

- **desktop**: Check for updates at launch and offer to install them
- **ui**: Reading view with speakers, and one list where jobs are recordings
- **desktop**: Speaker labels, summary, re-transcribe and cancel
- **ui**: New app mark, in the sidebar and on every icon
- **ui**: Vault tabs, transcript viewer, meeting editor and service log
- **desktop**: Transcript, rename, delete and service-log IPC commands
- **vault**: Rename, re-file and delete existing meetings
- **vault**: Accept lowercase project codes, capitalized on the way in

### Tests

- **service**: Assert /health version against __version__, not a literal
- **scripts**: Run the host-independent bootstrap assertions in CI
- Fix a racy frontend assertion and mark the host-only bootstrap tests
- **desktop**: Canonicalize the root in the e2e containment assertion too
- **desktop**: Compare filed paths against the canonical meetings root

## 0.1.0 — 2026-08-22

### Bug fixes

- Field-report bugs from first real install

### Chores

- **sdd**: Consolidated QA fixes for merged tree
- Initial commit with IDEA.md

### Documentation

- **sdd**: Specs and plans for batch idea

### Features

- **vault**: Persistent vault browser
- **ui**: Ledger redesign from Claude Design handoff
- **windows-installer-build**: Windows installer and build system
- **meeting-vault-layout**: Vault library crate with naming, routing and ingest

### Other

- Feat(windows-installer-build)
- Wip(tauri-desktop-app)
- **tauri-desktop-app**: Tauri 2 drag-and-drop desktop app [sdd: needs attention]
- Wip(transcription-service)
- **transcription-service**: Whisper transcription microservice [sdd: needs attention]
- Feat(meeting-vault-layout)


