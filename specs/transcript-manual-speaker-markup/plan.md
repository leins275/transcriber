---
slug: transcript-manual-speaker-markup
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Manual speaker markup on a selected part of the transcript

## Architecture overview

Purely frontend, inside `apps/desktop/src`. Zero changes under `services/transcription/` (NFR-1) and zero Rust changes (FR-4): storage, IPC, and downstream consumers are already segment-granular.

Components and data flow:

```
TranscriptViewer.tsx (Timeline view)
  ├─ renders each turn's paragraph as per-segment <span data-segment-id="…">   (new, T3)
  ├─ on pointer-up / selection change over the transcript <ol>:
  │    lib/selection.ts  segmentIdsFromRange(range, root) -> string[]           (new, T2)
  │       closest('[data-segment-id]') on both boundary points, snap outward,
  │       slice the document-ordered id list between them (cross-turn safe)
  ├─ SelectionSpeakerMenu.tsx                                                   (new, T4)
  │       presentational popover: known names (reuse-first, same order as
  │       SpeakerTag) + new-name input; Escape / click-away -> onDismiss
  ├─ on confirm:
  │    lib/turns.ts  assignSpeakerToSegments(speakers, ids, name) -> next map   (new, T1)
  │    persist(next)  — existing callback path: setSpeakers + onSaveSpeakers
  └─ groupIntoTurns(segments, next)  — existing rule 1 splits the turn at the
       speaker change; no grouping changes (FR-3)
```

- **`apps/desktop/src/lib/turns.ts`** — add `assignSpeakerToSegments(speakers, segmentIds, speaker)`: the id-list generalization of the existing `assignSpeaker` (which becomes a one-line delegate over `turn.segmentIds`). Pure; caller persists.
- **`apps/desktop/src/lib/selection.ts`** (new) — the selection→segment mapping as a pure function over a DOM `Range` plus the transcript root. Returns the document-ordered segment ids the selection overlaps, whole-segment snapped outward; `[]` for collapsed selections or selections outside transcript text. Implementation resolves each boundary point via `closest('[data-segment-id]')` (falling back to the nearest segment span in range via `intersectsNode` when a boundary lands on chrome — timestamp, speaker cell, inter-span whitespace) and then slices the ordered id list — O(segments) once per pointer-up, no layout reads, which keeps NFR-2's ~100 ms budget trivially.
- **`apps/desktop/src/components/SelectionSpeakerMenu.tsx`** (new, + `.module.css`) — presentational popover in the `SpeakerTag` mold: buttons for every known name (`aria-label="Attribute selection to <name>"`) plus a new-name input; `onAssign(name)` / `onDismiss()` callbacks; document-level Escape and pointer-down-outside handlers (cleaned up on unmount). No `invoke`/`listen`/`fetch` (NFR-4).
- **`apps/desktop/src/components/TranscriptViewer.tsx`** — render segments as spans inside each turn's `<p>` (visible text unchanged); own the selection lifecycle (detect, open menu anchored near the selection rect, dismiss on clear/Escape/click-away); on confirm call `assignSpeakerToSegments` and the existing `persist`, which already handles save failure inline via the `role="alert"` element (FR-6) and re-groups via the existing `useMemo` on `speakers` (FR-3).

Schema/API changes: **none**. `speakers.json` stays v1; `setSpeakerLabels` (`apps/desktop/src/api.ts`) and `set_speaker_labels` (Rust) untouched.

## Risks

- **jsdom fidelity for selection APIs.** Tests must build `Range`s programmatically (`document.createRange`, `window.getSelection().addRange`). jsdom implements `Range`, `intersectsNode`, and `closest`, but not layout — so the mapping function (T2) is designed around document order, never `getBoundingClientRect`; the menu's pixel positioning is trivial, untested at unit level, and verified live in T7.
- **Popover anchored to a selection has no focused trigger**, so Escape/click-away need document-level listeners (T4). Risk of leaks/double-handling is contained by owning both listeners in one effect with cleanup, tested explicitly (FR-7).
- **Boundary points landing outside segment spans** (turn chrome, cross-turn gaps). The fallback path in T2 is where FR-9 and the "no control outside transcript text" case live; the test list names these edges explicitly.
- **Performance on 1-hour transcripts (NFR-2).** Mapping is a single array slice; segment lookup in the viewer is a memoized `Map`. No per-segment event listeners — one handler on the `<ol>`. T7 sanity-checks responsiveness on a real long transcript.
- **Regression risk to turn-level flows (FR-10).** T3 changes how turn text renders; the existing `TranscriptViewer.test.tsx` suite must stay green throughout, and T6 adds coexistence cases (rename-after-sub-turn-assign).

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3, T4 |
| 2 | T5 |
| 3 | T6 |
| 4 | T7 |

## Tasks

### [x] T1: assignSpeakerToSegments in lib/turns  [deps: —]

- **Files**: `apps/desktop/src/lib/turns.ts`, `apps/desktop/src/lib/turns.test.ts`
- **Test first**: `apps/desktop/src/lib/turns.test.ts` — cases: assigning ids N..M adds exactly those entries, no more/fewer (FR-2); `null` / blank name deletes the ids' entries; input map is not mutated; regroup after mid-turn reassign — segments 1–5 "Maxim", assign id 3 to "Anna" via the new function, `groupIntoTurns` yields turns 1–2 "Maxim" / 3 "Anna" / 4–5 "Maxim" (FR-3); same on an unlabelled turn leaves flanking ranges `speaker: null` (FR-3); existing `assignSpeaker` tests stay green (FR-10).
- **Implement**: Add pure `assignSpeakerToSegments(speakers, segmentIds, speaker)` mirroring `assignSpeaker`'s trim/delete semantics; refactor `assignSpeaker` to delegate to it over `turn.segmentIds`. No changes to `groupIntoTurns` (FR-3 rides the existing rule 1).
- **Skills**: `frontend-toolkit:internal-ui`
- **Done when**: new cases plus the whole existing `turns.test.ts` pass; `make format lint type test` pass.

### [x] T2: selection→segment mapping (lib/selection)  [deps: —]

- **Files**: `apps/desktop/src/lib/selection.ts`, `apps/desktop/src/lib/selection.test.ts`
- **Test first**: `apps/desktop/src/lib/selection.test.ts` — build a jsdom fixture mimicking the viewer (an `<ol>` of turns, each `<p>` containing `<span data-segment-id>` spans with chrome elements between) and construct `Range`s over its text nodes. Cases: range covering segments N..M exactly returns `[N..M]` (FR-2); range starting/ending mid-segment includes that whole segment — snap outward (FR-2); range spanning the tail of one turn and the head of the next returns ids from both, in document order (FR-9); collapsed range returns `[]` (FR-1); range entirely outside segment spans (toolbar, timestamps) returns `[]` (FR-1); boundary point on turn chrome between spans still resolves via the in-range fallback.
- **Implement**: `segmentIdsFromRange(range: Range, root: HTMLElement): string[]`. Resolve each boundary via `Element.closest('[data-segment-id]')`; when a boundary misses, fall back to the first/last segment span for which `range.intersectsNode` is true; slice the document-ordered list of `root.querySelectorAll('[data-segment-id]')` ids between the two. Pure, no layout reads (NFR-2).
- **Skills**: `frontend-toolkit:internal-ui`
- **Done when**: all cases pass under Vitest/jsdom; `make format lint type test` pass.

### [x] T3: per-segment spans in the turn paragraph  [deps: —]

- **Files**: `apps/desktop/src/components/TranscriptViewer.tsx`, `apps/desktop/src/components/TranscriptViewer.test.tsx`, `apps/desktop/src/components/TranscriptViewer.module.css`
- **Test first**: `apps/desktop/src/components/TranscriptViewer.test.tsx` — cases: each turn's paragraph contains one element per segment carrying `data-segment-id` matching the segment's id (FR-2's "segments render as identifiable spans"); the paragraph's visible text is unchanged from today's joined form (guarded by the existing "groups segments into turns" and Cyrillic tests staying green); whitespace-only segments still render nothing.
- **Implement**: In the Timeline branch, replace `<p>{turn.text}</p>` with spans built from `turn.segmentIds` via a memoized `Map<id, segment>` over `transcript.segments`, space-separated to preserve the current reading text. Keep `turn.text` (search/filter) untouched. No behavior change otherwise — this task must leave the whole existing suite green.
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: new + all existing `TranscriptViewer.test.tsx` cases pass; `make format lint type test` pass.

### [x] T4: SelectionSpeakerMenu component  [deps: —]

- **Files**: `apps/desktop/src/components/SelectionSpeakerMenu.tsx`, `apps/desktop/src/components/SelectionSpeakerMenu.module.css`, `apps/desktop/src/components/SelectionSpeakerMenu.test.tsx`
- **Test first**: `apps/desktop/src/components/SelectionSpeakerMenu.test.tsx` — cases: renders a button per known name in the given (first-speech) order plus a new-name input (FR-1); clicking a known name calls `onAssign(name)`; typing a new name + Enter calls `onAssign(trimmed)`; empty/whitespace new name does not assign; Escape anywhere calls `onDismiss` (FR-7); pointer-down outside the menu calls `onDismiss`, pointer-down inside does not (FR-7); listeners are removed on unmount.
- **Implement**: Presentational popover styled after `SpeakerTag`'s reuse-first row (`aria-label="Attribute selection to <name>"` per button, action-named like SpeakerTag's buttons). Props: `known: string[]`, `anchor: {x, y}` (or rect), `onAssign(name: string): void`, `onDismiss(): void`. One `useEffect` owning document `keydown` (Escape) + `pointerdown` (outside) with cleanup. No `invoke`/`listen`/`fetch` (NFR-4). Positioning is simple fixed offset from the anchor — behavior tested, pixels verified live in T7.
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: all component cases pass; `make format lint type test` pass.

### [x] T5: wire selection flow into TranscriptViewer  [deps: T1, T2, T3, T4]

- **Files**: `apps/desktop/src/components/TranscriptViewer.tsx`, `apps/desktop/src/components/TranscriptViewer.test.tsx`, `apps/desktop/src/components/TranscriptViewer.module.css`
- **Test first**: `apps/desktop/src/components/TranscriptViewer.test.tsx` (selection built via `document.createRange` + `window.getSelection().addRange`, then pointer-up on the transcript list) — cases: selecting text inside a turn surfaces the menu offering every known speaker plus a new-name input (FR-1); collapsed/empty selection surfaces nothing (FR-1); assigning "Anna" over segments N..M calls `onSaveSpeakers` with exactly the merged map — prior entries kept, precisely N..M added (FR-2, FR-4); a mid-segment selection snaps outward to the whole segment (FR-2); after assigning an inner segment the list re-renders as three turns with the flanks keeping their previous attribution, including `null` (FR-3); with `onSaveSpeakers` rejecting, the new attribution stays visible and the `role="alert"` shows the message (FR-6).
- **Implement**: One `pointerup` handler on the turns `<ol>` reads `window.getSelection()`, maps via `segmentIdsFromRange`, and opens `SelectionSpeakerMenu` anchored at the selection's bounding rect; confirm calls `assignSpeakerToSegments` + existing `persist`, clears the selection, closes the menu; re-grouping is free via the existing `groupIntoTurns` memo. No IPC in the component (NFR-4); no per-segment listeners (NFR-2).
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: new cases plus the entire pre-existing `TranscriptViewer.test.tsx` suite pass; `make format lint type test` pass.

### [x] T6: dismissal, filtered view, cross-turn, coexistence  [deps: T5]

- **Files**: `apps/desktop/src/components/TranscriptViewer.tsx`, `apps/desktop/src/components/TranscriptViewer.test.tsx`
- **Test first**: `apps/desktop/src/components/TranscriptViewer.test.tsx` — cases: Escape closes the menu and `onSaveSpeakers` is never called — speaker map unchanged (FR-7); click-away closes it likewise (FR-7); clearing the selection (collapsed re-select) closes it (FR-7); with an active search query, assigning inside a visible turn updates the correct segment ids and the match count/grouping refresh, hidden turns untouched (FR-8); a selection spanning the tail of turn A and head of turn B assigns all overlapped segments of both to the one name (FR-9); after a sub-turn assignment, renaming that speaker via `SpeakerTag` still renames every segment holding the name, including the newly assigned ones (FR-10); turn-level assign via `SpeakerTag` still works on the new smaller turns (FR-10).
- **Implement**: Close gaps the tests expose in T5's wiring — dismissal state resets (menu closes without persisting), filtered-view correctness falls out of real segment ids on spans, cross-turn falls out of T2's document-order slice. Expect mostly tests plus small state fixes; no new modules.
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: all cases pass with the full suite; `make format lint type test` pass.

### [x] T7: end-to-end verification and compatibility proof  [deps: T6]

- **Files**: none — verification only; no source changes may be introduced by this task (regressions found here are fixed under the owning task's file contract)
- **Test first**: not applicable (verification task); evidence is the checks below, mapped to FR-4, FR-5, NFR-1, NFR-2 and the desktop profile's Verification section.
- **Implement**: (1) Prove NFR-1/FR-5 mechanically: `git diff --stat base_ref -- services/transcription/` is empty, and the Python suite passes unchanged (`make test` or `uv run pytest` in `services/transcription/`) — schema compatibility proven, not asserted. (2) Launch the app (per the desktop profile's Verification: `make test` alone does not prove the app runs — use the run skill / `npm run tauri dev` in `apps/desktop`) against a meeting with a real transcript; drive: select a sentence inside a paragraph, assign an existing and a new name, confirm three-way re-group, reopen the meeting and confirm `speakers.json` on disk has `schema_version: 1` with the merged map (FR-4); confirm a pre-existing v1 `speakers.json` renders unchanged (FR-5); confirm the menu appears promptly on a long (~1 h) transcript with no visible stutter (NFR-2). (3) Full QA sweep: `make format lint type test` all green.
- **Skills**: `frontend-toolkit:internal-ui`, `testing-toolkit:python-testing-patterns`
- **Done when**: all three checks recorded with evidence; `make format`, `make lint`, `make type`, `make test` all pass; zero diff under `services/transcription/`.

## QA expectations

- `make format`, `make lint`, `make type`, `make test` all exist (verified in the spec via `make -n`); every task must leave all four green.
- UI unit tests run under Vitest + Testing Library (jsdom). No Playwright/Cypress in the repo — the desktop profile's "drive the affected flow" requirement is met by T7's live `tauri dev` run, and final adversarial UI validation happens in pipeline Phase 6 (`browser-toolkit:ui-test`).
- Known jsdom limits: no layout, so nothing may assert popover pixel positions; selection tests must construct `Range`s programmatically.
- `services/transcription/` must show zero diff for the whole feature (NFR-1) — any diff there is a defect, not a fix.
