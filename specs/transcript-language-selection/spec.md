---
slug: transcript-language-selection
created: 2026-08-24
status: approved
---

# Spec: Transcript language follows the recording (Russian or English)

## Summary

Transcription must come out in the language actually spoken in the recording. Today the service lets faster-whisper auto-detect across ~100 languages, and an English recording was mis-detected and transcribed as Russian. The operator's language universe is exactly two: Russian and English. The service will constrain language detection to that set, honor an explicit per-job language, validate the field at every entry point, and always record the decode language in `transcript.json` and the ledger — the field sibling feature F3 (artifact language) will depend on.

## Problem & context

- The operator's English recording was transcribed in Russian. Root cause surface: `services/transcription/src/transcription/providers/local_whisper.py` passes `decode_kwargs["language"] = language` (line ~264) and every job arrives with `language=None`, so faster-whisper free-detects over its full language set and can commit to the wrong one.
- The per-job `language` channel already exists end-to-end but is never used: `apps/desktop/src-tauri/src/jobs.rs:480` hardcodes `language: None` in `SubmitRequest`; `apps/desktop/src-tauri/src/service/http.rs:134` serializes it into `POST /v1/jobs`; `schema.py` `JobCreate.language: str | None` accepts it unvalidated; `jobs.py` stores it on the job and the ledger row and hands it to the provider (`language=job.language`, line ~559).
- `local_whisper.py:331` sets `language_out = getattr(info, "language", None) or language`, and `jobs.py` writes it into `transcript.json` (`language`, `language_probability`) and the ledger. F3 ("Facts & action items follow the transcript language") will pin LLM output to this field — F2 must make it trustworthy.
- The desktop app has no language control anywhere: `apps/desktop/src/components/SettingsPage.tsx` is a read-mostly ledger (only the vault root is mutable), and `apps/desktop/src/components/RecordingPage.tsx:201` already offers a "Transcribe / Re-transcribe" button (`transcribe_vault_entry` command, `apps/desktop/src-tauri/src/commands/meetings.rs:593`) — the natural home for a per-recording override.
- The CLI one-shot path (`cli.py --language`, line 72; applied at line 242) and the service config default (`config.py:71` `language: str | None = None`) accept any string today.
- `faster-whisper>=1.2,<2` (`services/transcription/pyproject.toml:11`) exposes language probabilities, so restricting the detection choice to {ru, en} is implementable without a second full decode.
- Everything runs locally; no cloud path exists for this feature (project direction: local-only).

## Users

- The single desktop-app operator, who records meetings in Russian *and* in English and needs each transcript to come out in the language actually spoken — without babysitting a setting.
- Downstream: the F3 LLM jobs (facts / action items / summaries), which will read `transcript.json.language` as ground truth.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists; Rust privileged process under `apps/desktop/src-tauri/`.
- `web` — `apps/desktop/package.json` names `react` (^18.3.1) and `vite` (^5.4.10); the Tauri UI is a webview SPA.
- `cli` — `services/transcription/pyproject.toml` has `[project.scripts]` (line 25) and `cli.py` uses `argparse`; the service's one-shot CLI (`--language`) is a real entry point this feature touches. (The profile's negative signal targets repos with no UI at all; here `cli` is additive for the Python service layer, not the whole app.)

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri (Rust) | `apps/desktop/src-tauri/tauri.conf.json`, `src-tauri/src/lib.rs` command registry |
| Frontend | React 18 + Vite 5 + TypeScript | `apps/desktop/package.json` |
| Transcription service | Python, faster-whisper >=1.2, localhost HTTP + one-shot CLI | `services/transcription/pyproject.toml`, `src/transcription/app.py`, `cli.py` |
| Service<->app seam | `POST /v1/jobs` JSON (`language?` optional field) | `apps/desktop/src-tauri/src/service/http.rs`, `services/transcription/src/transcription/schema.py` |
| Persistence | SQLite ledger (`jobs.language` column) + `transcript.json` per meeting | `services/transcription/src/transcription/ledger.py`, `jobs.py` |
| Testing | cargo test (workspace), Vitest, pytest | Makefile `test` target: `cargo test --workspace`, `npm --prefix apps/desktop run test` (vitest), `uv run --directory services/transcription pytest -q` |

Makefile QA targets present: format, lint, type, test (each fans out to Rust + npm + uv/Python).

## Functional requirements

- **FR-1** (must): **Constrained auto-detection.** When a transcribe job has no explicit language, the service picks the decode language by comparing the model's language probabilities restricted to `{ru, en}` and forces the winner into `decode_kwargs["language"]`. The decode language is always exactly `"ru"` or `"en"`; no third language can ever be chosen. Applies to both the sequential and the `BatchedInferencePipeline` paths in `local_whisper.py`.
- **FR-2** (must): **Explicit language honored.** A job submitted with `language="en"` decodes in English; `language="ru"` decodes in Russian — regardless of what detection would have said.
- **FR-3** (must): **Validation at every entry point.** `JobCreate.language` (HTTP), `cli.py --language`, and the config default `config.py:language` accept only `"ru"`, `"en"`, or unset. An invalid value over HTTP is rejected as `invalid_request` before any ledger row exists; an invalid CLI/config value fails startup/one-shot with a clear message and nonzero exit.
- **FR-4** (must): **Trustworthy language field.** For every new transcribe job, `transcript.json.language` and the ledger's `jobs.language` record the language actually used for decoding (`"ru"` or `"en"`), whether forced or auto-selected; `language_probability` carries the constrained-detection probability (or the model-reported value on a forced run). This is the field F3 consumes.
- **FR-5** (should): **Desktop app passes the language.** `SubmitRequest.language` (today hardcoded `None` in `jobs.rs:480`) is populated from the operator-facing control chosen in Q1, for both the ingest path and the `transcribe_vault_entry` re-transcribe path. Exact UI shape is Q1's outcome.
- **FR-6** (could): **Language visible on the recording page.** `RecordingPage` shows the transcript's detected/forced language (the view models already carry `language: string | null` — `apps/desktop/src/types.ts:96,198`), so the operator can see at a glance when to re-transcribe with an override.

## Non-functional requirements

- **NFR-1**: Constrained detection adds no second full decode; it uses a single detection window (one extra encoder pass, ≤ ~2 s overhead on the CUDA path for a typical meeting-length recording).
- **NFR-2**: No `transcript.json` schema change — same fields, values now constrained; existing consumers (`meetings.rs` parser, F3's `_load_transcript_lines`) keep parsing unchanged.
- **NFR-3**: An invalid `language` on `POST /v1/jobs` returns a classified `invalid_request` error within the existing submit-time budget (no model load, no ledger write) — never a raw 500.

## Acceptance criteria

- **FR-1**:
  - [ ] An English-speech fixture submitted with no `language` produces `transcript.json` with `language: "en"` and English text; a Russian fixture produces `language: "ru"` and Russian text.
  - [ ] Unit test: with mocked model probabilities where a non-target language (e.g. `uk`) outranks both `ru` and `en`, the chosen decode language is still the higher of `ru`/`en`.
  - [ ] The constraint applies on both the batched (CUDA) and sequential decode paths.
- **FR-2**:
  - [ ] Submitting with `language="en"` yields `decode_kwargs["language"] == "en"` (asserted via a fake/spy provider test) and `transcript.json.language == "en"`; symmetric for `"ru"`.
- **FR-3**:
  - [ ] `POST /v1/jobs` with `language="de"` (and with `language=""`) returns an `invalid_request` error and creates no ledger row and no job.
  - [ ] `--language de` on the one-shot CLI exits nonzero with a message naming the allowed values; `"ru"`, `"en"`, and omission are accepted.
- **FR-4**:
  - [ ] After any successful transcribe job (forced or auto), `transcript.json.language ∈ {"ru", "en"}` and the ledger row's `language` matches it.
  - [ ] `language_probability` is populated on auto-detected runs.
- **FR-5**:
  - [ ] The wire body of `POST /v1/jobs` contains the operator-selected language (observed via the existing `http.rs` submit-body tests) on both the drag-drop ingest path and the Re-transcribe path.
  - [ ] With the control at its default, behavior is FR-1's constrained auto — never a silent hard-force the operator didn't ask for (subject to Q1's decision).
- **FR-6**:
  - [ ] A recording whose transcript has `language: "en"` shows an English indicator on the recording page; a transcript with `language: null` (legacy) shows nothing rather than a placeholder.

## Out of scope

- F3 itself (LLM facts/action-items/summaries following the transcript language) — separate sibling feature; F2 only guarantees the field it reads.
- Languages beyond Russian and English, translation, or per-segment/mixed-language handling within one recording.
- Backfilling `language` on existing ledger rows or already-written `transcript.json` files (the operator can Re-transcribe any recording to fix it).
- Any cloud detection/transcription path (project is local-only by decision).
- Diarization, VAD tuning, or other decode-quality changes not needed for language selection.

## Applicable toolkits

- `frontend-toolkit:internal-ui` — webview UI layer; React+Vite in `apps/desktop/package.json`, single-operator internal tool (via `web` UI-internal row, carried into `desktop`).
- `frontend-toolkit:ui-ux-pro-max` — same UI layer and signal (via `web` UI-internal row).
- `testing-toolkit:python-testing-patterns` — pytest suite under `services/transcription/tests/`, `make test` runs `pytest -q` (rows in `web`, `desktop`, and `cli`).
- `devops-toolkit:devops-rollout-plan` — packaging/bundle config `apps/desktop/src-tauri/tauri.conf.json` (via `desktop` Packaging row); relevant only if the shipped installer's defaults are touched.

(No Playwright/Cypress, Docker, Django, or PostgreSQL signals exist in this repo — those profile rows are dropped.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (from the `web` profile; the `desktop` profile defers to it for webview UIs).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — Q1 resolved at the batch clarification gate (see Decisions log).

## Decisions log

- 2026-08-24 — (OPERATOR, batch gate) Q1: Where does the operator control the language? → **Auto by default + per-recording override**: the Re-transcribe control on the recording page gains an Auto / Russian / English choice; the default stays constrained auto-detection (FR-1). FR-5 and FR-6 are in scope with this shape; no global setting.

- 2026-08-24 — (AUTO: operator text "basically could be two options - russian or english") The language universe is exactly `{ru, en}`; auto-detection is constrained to that set instead of faster-whisper's full ~100-language detection.
- 2026-08-24 — (AUTO: codebase) Transport is the existing per-job `language` field on `POST /v1/jobs` — already plumbed through `http.rs::SubmitBody`, `schema.py::JobCreate`, `jobs.py::JobState`, and `local_whisper.py::decode_kwargs`. No new endpoint, no service-config round-trip.
- 2026-08-24 — (AUTO: sibling F3 dependency) `transcript.json.language` is the single source of truth for the recording's language; F2 guarantees it is always populated with the actual decode language for new jobs.
- 2026-08-24 — (AUTO: project memory "local-only direction") No cloud-assisted detection; everything runs in the local faster-whisper provider.
- 2026-08-24 — (AUTO: codebase, `RecordingPage.tsx:201`) The existing Transcribe/Re-transcribe button on the recording page is the established per-recording action surface; any per-recording override attaches there rather than inventing an ingest-time dialog (drag-drop filing is deliberately silent/batch, `apps/desktop/src-tauri/src/jobs.rs` FR-6 comments).
