# Changelog

Every release of Transcriber, newest first. Generated from conventional
commits by git-cliff — edit the commit messages, not this file.

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


