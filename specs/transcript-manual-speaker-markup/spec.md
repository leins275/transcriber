---
slug: transcript-manual-speaker-markup
created: 2026-08-24
status: approved
---

# Spec: Manual speaker markup on a selected part of the transcript

## Summary

In the transcript reading view, the operator can select a contiguous stretch of text inside a rendered paragraph (turn), and attribute exactly that stretch to a speaker — an existing name or a new one. Today attribution is turn-granular only: when one sentence in the middle of a paragraph was spoken by someone else, the operator cannot correct just that sentence. Storage stays in the existing `speakers.json` schema v1 (`segment id -> speaker name`); the feature is a UI-granularity fix, not a data-model change.

## Problem & context

- The viewer (`apps/desktop/src/components/TranscriptViewer.tsx`) groups Whisper segments into turns (`apps/desktop/src/lib/turns.ts`, `groupIntoTurns`) and renders one paragraph per turn. The only attribution control is `SpeakerTag` (`apps/desktop/src/components/SpeakerTag.tsx`), which assigns a speaker to **every** segment of a turn via `assignSpeaker`. There is no way to attribute a subset of a turn, even though the storage already supports it.
- Storage is already segment-granular: `speakers.json` (`apps/desktop/src-tauri/src/commands/meetings.rs`, `SPEAKERS_SCHEMA_VERSION = 1`) is a `segment id -> speaker name` map, written wholesale and atomically by `set_speaker_labels` / `write_speaker_labels`. The current granularity limit lives **only in the UI**.
- Segments are sentence-sized by construction: `services/transcription/src/transcription/segmentation.py` (`resegment`) cuts every raw Whisper segment at sentence-ending punctuation and at word-level pauses ≥ 0.6 s, using word timestamps that are on by default (`word_timestamps: bool = True` in `services/transcription/src/transcription/config.py:76`). So "one sentence inside a paragraph" maps to one or more whole segments in practice, and snapping a selection to segment boundaries attributes exactly the sentence the operator selected.
- Downstream consumers need no change: `services/transcription/src/transcription/exporting.py` `load_speaker_overrides` reads the same v1 map, and `render_transcript_lines` (`services/transcription/src/transcription/llm/prompts.py:34`) already emits one `[m:ss] Speaker: text` line **per segment**, applying per-segment overrides. Finer per-segment attribution flows into every LLM job and export automatically.
- Re-grouping after a mid-turn reassignment is also already implemented: `groupIntoTurns` rule 1 splits a turn at any change of assigned speaker, so a reassigned inner range renders as its own paragraph between the two remainders.

## Users

The single operator of the Transcriber desktop app, reading a finished transcript and correcting mis-attributed speech. Corrections feed the operator's own reading and every downstream LLM artifact (summary, facts, action items) and export.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists; `@tauri-apps/api ^2.1.1` in `apps/desktop/package.json` (Tauri 2).
- `web` — `react ^18.3.1` and `vite ^5.4.10` in `apps/desktop/package.json` (webview UI; per the desktop profile, UI toolkits come from `web`, process/IPC rules from `desktop`).

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 | `apps/desktop/src-tauri/tauri.conf.json`, `@tauri-apps/api ^2.1.1` |
| Privileged process | Rust (Cargo workspace) | `apps/desktop/src-tauri/`, `crates/vault/`, root `Cargo.toml` |
| UI | React 18 + TypeScript + Vite 5 | `apps/desktop/package.json` |
| Transcription service | Python (uv-managed) | `services/transcription/pyproject.toml` |
| Testing (UI) | Vitest + Testing Library | `apps/desktop/package.json` devDeps; `apps/desktop/src/lib/turns.test.ts` |
| Testing (Rust) | cargo test | `#[cfg(test)]` in `apps/desktop/src-tauri/src/commands/meetings.rs` |
| Testing (Python) | pytest | `services/transcription/tests/` |

Makefile QA targets present: format, lint, type, test (all four; verified with `make -n`).

## Functional requirements

- **FR-1** (must): In the Timeline view, the operator can select a contiguous range of transcript text with the pointer (native text selection over a turn's paragraph) and is offered an "assign speaker" control for the selection. The control offers the names already in use in this transcript (same reuse-first rule as `SpeakerTag`) plus entering a new name.
- **FR-2** (must): Confirming an assignment attributes the chosen speaker to **exactly the segments the selection overlaps**, snapping outward: a segment partially covered by the selection is included whole. Segment id resolution must survive turn grouping (segments render as identifiable spans inside a turn's paragraph, or equivalent).
- **FR-3** (must): After assignment, the transcript re-groups immediately: the reassigned range becomes its own turn, and the un-reassigned remainder(s) of the original turn keep their previous attribution (including `null`/unassigned). This uses the existing `groupIntoTurns` speaker-change rule; no new grouping logic.
- **FR-4** (must): Persistence reuses the existing wholesale path: the updated `segment id -> name` map is saved via `setSpeakerLabels` (`apps/desktop/src/api.ts`) → `set_speaker_labels_handler` → atomic `write_speaker_labels`. `speakers.json` stays at `schema_version: 1` with the same shape. No Rust command signature changes are required.
- **FR-5** (must): Backward compatibility — an existing `speakers.json` v1 file (written by any prior build) loads and renders unchanged, and a file written after a sub-turn markup is a valid v1 file that prior builds and the Python service read without modification. `load_speaker_overrides` and `render_transcript_lines` in the Python service are **not modified**; sub-turn attributions reach LLM jobs and exports through the existing per-segment override path.
- **FR-6** (must): A failed save behaves like today's turn-level flow: the labels stay on screen, and the error surfaces inline via the existing `role="alert"` element (`saveError` in `TranscriptViewer.tsx`). No silent loss of the operator's markup.
- **FR-7** (must): Dismissal — clicking away, clearing the selection, or pressing Escape closes the assign control without changing any attribution.
- **FR-8** (should): The selection-assign flow also works when the transcript is filtered by the search box (`filterTurns`): assignments land on the correct segment ids, and hidden turns are unaffected.
- **FR-9** (should): A selection spanning more than one turn is allowed and attributes all overlapped segments across those turns to the one chosen speaker.
- **FR-10** (should): Existing turn-level controls (`SpeakerTag` assign/rename, rename-merges-everywhere) keep working unchanged alongside the new selection flow, including on the new smaller turns produced by FR-3.

## Non-functional requirements

- **NFR-1**: Zero diff under `services/transcription/` — the Python test suite passes unchanged, proving schema compatibility rather than asserting it.
- **NFR-2**: Selection-to-segment mapping and re-grouping stay interactive on a 1-hour transcript (thousands of post-`resegment` segments): the assign control appears within ~100 ms of pointer-up, with no visible re-render stutter.
- **NFR-3**: `speakers.json` writes remain atomic (temp file + rename — the existing `write_speaker_labels`), and remain under the existing `MAX_SPEAKERS_BYTES` (1 MiB) cap for realistic meetings.
- **NFR-4**: The new UI remains presentational in the established sense: no `invoke`/`listen`/`fetch` inside `TranscriptViewer`/`SpeakerTag`-layer components; persistence flows only through the `onSaveSpeakers` callback.

## Acceptance criteria

- **FR-1**:
  - [ ] Selecting text inside a turn's paragraph in Timeline view surfaces an assign control offering every known speaker name plus a new-name input.
  - [ ] The control does not appear for an empty/collapsed selection or for selections outside transcript text.
- **FR-2**:
  - [ ] Selecting exactly the text of segments N..M and assigning "Anna" adds entries for precisely the ids of N..M to the speaker map — no more, no fewer.
  - [ ] A selection starting or ending mid-segment includes that whole segment in the assignment.
- **FR-3**:
  - [ ] Given a turn of segments 1–5 labelled "Maxim", assigning segment 3 to "Anna" renders three turns: 1–2 "Maxim", 3 "Anna", 4–5 "Maxim" (unit-testable against `groupIntoTurns`).
  - [ ] The same operation on an unlabelled turn leaves the flanking ranges unassigned ("Add speaker"), not attributed to anyone.
- **FR-4**:
  - [ ] After assignment, `speakers.json` on disk contains `schema_version: 1` and the merged `assignments` map; re-opening the meeting shows the same attribution.
  - [ ] No new Tauri command is added and `set_speaker_labels`'s signature is unchanged.
- **FR-5**:
  - [ ] A pre-existing `speakers.json` from a released build opens and renders identically before and after this feature ships.
  - [ ] With a sub-turn attribution saved, the export/LLM transcript rendering (`render_transcript_lines`) shows the corrected speaker on exactly the corrected segments' lines — with no change to any Python source file.
- **FR-6**:
  - [ ] With `onSaveSpeakers` rejecting, the new attribution stays visible and the alert shows the error message.
- **FR-7**:
  - [ ] Escape and click-away each dismiss the control; the speaker map is byte-identical to before the selection.
- **FR-8**:
  - [ ] With an active search query, assigning within a visible turn updates the correct segment ids and the match count/grouping refresh correctly.
- **FR-9**:
  - [ ] A selection covering the tail of turn A and the head of turn B assigns all overlapped segments of both to the chosen name.
- **FR-10**:
  - [ ] Renaming a speaker after a sub-turn assignment still renames every segment holding that name, including newly assigned ones.

## Out of scope

- **Sub-segment attribution and segment splitting.** No `speakers.json` schema v2, no time-range/character-range keys, no rewriting of `transcript.json` (it is F2's artifact, rewritten whole on re-transcription — manual splits stored there would not survive). Selection granularity is the segment; see Decisions log for why this suffices.
- Editing transcript text or timestamps.
- Any change to diarization or to the transcription pipeline (`services/transcription/`).
- Multiple speakers within one assignment action.
- Markup in the Plain text view (a read-only textarea for copying; markup lives in Timeline view).
- Touch/mobile selection ergonomics — this is a desktop app; Windows is the primary platform, pointer selection is the target interaction.

## Applicable toolkits

- `frontend-toolkit:internal-ui` — UI layer; React/Vite operator-facing app (`apps/desktop/package.json`).
- `frontend-toolkit:ui-ux-pro-max` — UI layer; same signal.
- `testing-toolkit:python-testing-patterns` — Python service tests; pytest suite at `services/transcription/tests/` (used here only to prove the no-change compatibility claim).
- `devops-toolkit:devops-rollout-plan` — packaging/distribution; `apps/desktop/src-tauri/tauri.conf.json` bundle config and `installer/` exist (unlikely to be exercised by this feature, listed because the signal is present).

Not applicable (signal absent): `testing-toolkit:e2e-testing-patterns` / `webapp-testing` (no Playwright/Cypress dependency anywhere), Django/Postgres/Docker rows.

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every UI task in this feature (carried from the `web` profile via the `desktop` profile's webview rule).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — see Decisions log. The one plausible product question (is segment granularity fine enough?) is answered by the codebase: segments are cut at sentence boundaries by design, so the selectable unit already matches the operator's stated unit ("one of the sentences").

## Decisions log

- 2026-08-24 — Attribution mechanism → **(AUTO: codebase)** Selection snaps to whole-segment boundaries; `speakers.json` stays schema v1. Evidence: `segmentation.py` `resegment` already splits every Whisper segment at sentence punctuation and ≥ 0.6 s word pauses (word timestamps default-on, `config.py:76`), so sentence ≈ segment; the storage (`meetings.rs`) and every consumer (`turns.ts`, `render_transcript_lines`, `load_speaker_overrides`) are already segment-granular — only the UI is turn-granular. Segment splitting or sub-segment ranges would ripple a schema v2 into the Python service for a case the pipeline already prevents.
- 2026-08-24 — Partially covered segments → **(AUTO: storage granularity)** snap outward (include the whole segment). Matches what will actually be stored; excluding would silently drop the operator's intent at the edges.
- 2026-08-24 — Pre-`resegment` transcripts (old coarse 30 s segments) → **(AUTO: existing remedy)** snap-to-segment may be coarse there; the app already offers re-transcription of a filed recording (`transcribe_vault_entry`), which regenerates sentence-sized segments. Not worth a schema change.
- 2026-08-24 — Which view hosts markup → **(AUTO: existing design)** Timeline only; Plain text is explicitly a copy-out surface (`readOnly` textarea).
- 2026-08-24 — Cross-turn selection → **(AUTO)** allowed (FR-9); restricting it would be extra code for less capability, since the mechanics are identical.
