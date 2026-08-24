---
slug: transcript-manual-speaker-markup
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 2
---

# Evaluation report: Manual speaker markup on a selected part of the transcript

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 1 | 0 |
| major | 0 | 0 | 0 |
| minor | 0 | 2 | 0 |

**Round 2: all findings verified fixed. No new findings. Ship.**

The diff implements the spec faithfully in structure: pure `assignSpeakerToSegments` with `assignSpeaker` delegating to it, a pure DOM-order selection mapper, a presentational popover, and wiring that persists only through the existing `onSaveSpeakers` path. Zero diff under `services/transcription/`, zero Rust/API changes. Round 1's blocker — the mapper attributing segments the selection covered by zero characters — is fixed by `coversText` (boundary-point clamping, no geometry) plus an inward scan from a rejected boundary; both edges have deterministic jsdom regression tests. All 314 frontend tests plus lint/type/format green.

## Findings

### E1 [blocker] [correctness] [status: fixed]

- **Where**: `apps/desktop/src/lib/selection.ts:34-48` (`boundaryIndex`, the direct `closest` path)
- **Spec ref**: FR-2 — "attributes the chosen speaker to **exactly the segments the selection overlaps**"; acceptance criterion "adds entries for precisely the ids of N..M — no more, no fewer". Decisions log defines snap-outward as covering *partially covered* segments; a zero-character touch is not partial coverage.
- **Expected**: A range whose end boundary sits at `(nextSegmentTextNode, 0)` — zero characters of that segment covered — excludes that segment; symmetrically a start boundary at `(prevSegmentTextNode, length)` excludes the previous segment. Chromium/WebView2 routinely normalizes a drag past the end of a sentence to offset 0 of the following text node, so this is an everyday gesture, not a corner case.
- **Actual**: `boundaryIndex` resolves the boundary container via `closest('[data-segment-id]')` and returns that span's index with no check that the range actually covers any of its content. jsdom repro against the exact logic: end at `(seg5 text, 0)` over segments 3–4 → `["3","4","5"]`; start at `(seg3 text, len)` selecting 4–5 → `["3","4","5"]`. Matches the live drive: a selection visually spanning 3–4 assigned 3, 4 and 5. The extra segment is silently handed to the chosen speaker and persisted.
- **Suggested fix**: After a direct `closest` hit, verify content overlap before accepting it — e.g. `selectNodeContents(span)` and `compareBoundaryPoints`: if the range falls entirely on one side of the span's content, discard the direct hit and fall through to the backward `intersectsNode` scan. Add regression tests for both edges in `selection.test.ts`.
- **Round 2 verification**: Fixed as claimed. `coversText(range, span, overlap)` (`selection.ts:46-59`) clamps the span's contents to the range via `compareBoundaryPoints` (START_TO_START / END_TO_END directions traced and correct) and requires non-empty text — structural only, no geometry, preserving the jsdom-testable property. The fixer's **rejection of round 1's suggested fallback is correct and I confirm the reasoning independently**: for an end boundary at `(seg5Text, 0)` the boundary point lies *inside* seg5's element, so per the DOM spec `intersectsNode(seg5)` returns true ((parent, index-of-seg5) is before the range end) and the suggested backward `intersectsNode` scan would re-find the just-discarded span. Round 1's "boundary touch = no intersection" observation holds only for boundaries *between* span elements (the whitespace-gap case), not at offset 0/length inside a span's own text node. The replacement — continue inward from the rejected index with `coversText` as the predicate (`boundaryIndex:82-91`) — is sound: nothing beyond the range end (resp. before the range start) can be covered, so the inward scan cannot skip a covered span. Traced both edges: end at `(next, 0)` → overlap collapses to empty → excluded, scan lands on the previous covered span; start at `(prev, len)` symmetric; a selection of only the inter-span whitespace rejects both edges → `[]`. Regression tests present for all three (`selection.test.ts:174-228`) plus the integration-level "leaves out a sentence the drag only ran up to" (`TranscriptViewer.test.tsx:312-335`). `intersectsNode` screens before the clamp so the cost stays negligible (fixer measured ~4ms/call on a 1144-segment transcript; pre-existing chrome-only-selection worst case unchanged).

### E2 [minor] [improvement] [status: fixed]

- **Where**: `apps/desktop/src/components/TranscriptViewer.tsx:196-243` (`onPointerUp` on the `<ol>` only)
- **Spec ref**: FR-1; plan T5 ("on pointer-up / selection change over the transcript `<ol>`")
- **Expected**: A drag that starts over transcript text but is released slightly past the list (below the last turn, in the page margin) still offers the assign control — overshooting a drag is a common way to select to the end of a paragraph.
- **Actual**: The only trigger is `pointerup` bubbling through the `<ol>`; releasing the pointer outside it leaves the text highlighted with no control offered and no feedback. The plan's "selection change" half was not wired.
- **Suggested fix**: Also handle `pointerup` on the viewer container (or a document-level `pointerup` while a selection intersects the list), reusing the same `readSelection` path.
- **Round 2 verification**: Fixed. A document-level `pointerdown`/`pointerup` pair (`TranscriptViewer.tsx:126-149`) gated on a `draggingFromList` ref: only a drag that *began* over the `<ol>` and is released outside it triggers `readSelection` (releases inside the list are left to the list's own handler — no double read; presses on the popover's own buttons never set the flag, so a collapsed selection is not re-read). Tested by "offers the control for a drag released past the end of the list" (`TranscriptViewer.test.tsx:337-359`). Listeners removed on cleanup.

### E3 [minor] [improvement] [status: fixed]

- **Where**: `apps/desktop/src/components/SelectionSpeakerMenu.module.css:1-3` vs `TranscriptViewer.tsx`
- **Spec ref**: — (code/comment coherence)
- **Expected**: The CSS comment says "the viewer closes it on scroll-away rather than chasing it".
- **Actual**: No scroll handling exists anywhere in the viewer. With `position: fixed`, wheel-scrolling while the menu is open leaves the popover floating at its old viewport point, detached from the (scrolled-away) selection, until a click or Escape dismisses it. Not an FR-7 violation (all three specified dismissal gestures work), but the comment documents behavior that was never implemented.
- **Suggested fix**: Either add a scroll listener that dismisses the menu, or correct the comment.
- **Round 2 verification**: Fixed — both halves. A capture-phase document `scroll` listener dismisses the menu (`SelectionSpeakerMenu.tsx:60`, capture because pane scrolls do not bubble), owned by the same effect as the other dismissal listeners and removed on cleanup; `onDismiss` is a stable `useCallback` in the viewer, so the effect does not churn. The CSS comment now describes the implemented behavior. Tested by "dismisses when the transcript is scrolled out from under it" and the unmount-cleanup test now also fires a scroll (`SelectionSpeakerMenu.test.tsx:89-110`).

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 | `TranscriptViewer.tsx` (readSelection + document-level release), `SelectionSpeakerMenu.tsx` | `TranscriptViewer.test.tsx::offers the known speakers…`, `::offers nothing for a caret`, `::offers the control for a drag released past the end of the list`; `SelectionSpeakerMenu.test.tsx::offers every known name…` | ✓ |
| FR-2 | `lib/selection.ts::segmentIdsFromRange` (+ `coversText`), `lib/turns.ts::assignSpeakerToSegments` | `selection.test.ts` (exact ids, snap outward, both zero-coverage edges, whitespace-only), `turns.test.ts::labels exactly the ids…`, `TranscriptViewer.test.tsx::attributes exactly…`, `::snaps a selection…`, `::leaves out a sentence the drag only ran up to` | ✓ (E1 fixed, regression-tested) |
| FR-3 | existing `groupIntoTurns` rule 1 (unchanged) | `turns.test.ts::splits a labelled turn in three…`, `::leaves the flanks…`, `TranscriptViewer.test.tsx::re-groups around the reassigned sentence…` | ✓ |
| FR-4 | existing `persist` → `onSaveSpeakers`; no command/signature changes (diff touches no `api.ts`/Rust) | `onSaveSpeakers` payload assertions; live drive (disk `schema_version: 1`, reopen) | ✓ |
| FR-5 | zero diff under `services/transcription/` (verified: `git diff --stat` empty) | mechanical proof + live drive | ✓ |
| FR-6 | `persist` catch → `saveError` → existing `role="alert"` | `TranscriptViewer.test.tsx::keeps a sub-turn attribution on screen when its save fails` | ✓ |
| FR-7 | `SelectionSpeakerMenu` document keydown/pointerdown/scroll effect; `readSelection` collapsed-clear | `::closes … on Escape`, `::…clicks away`, `::…selection is cleared`, `::dismisses when the transcript is scrolled…`; menu unmount-cleanup test | ✓ |
| FR-8 | real segment ids on spans; filter untouched | `::assigns inside a filtered transcript without touching the hidden turns` | ✓ |
| FR-9 | document-order slice in `segmentIdsFromRange` | `selection.test.ts::returns ids from both turns…`, `TranscriptViewer.test.tsx::attributes a selection that spans two turns…` | ✓ |
| FR-10 | `assignSpeaker` delegates; `renameSpeaker` unchanged | `::renaming a speaker after a sub-turn assignment…`, `::still attributes a whole turn from its tag after a sub-turn split`; all pre-existing tests green | ✓ |
| NFR-1 | — | `git diff <base> --stat` + `git status`: nothing under `services/transcription/` | ✓ |
| NFR-2 | no layout reads in mapping (`coversText` is boundary-points only); `intersectsNode` screen; memoized `Map` | design + fixer measurement (~4ms on 1144 segments) | ✓ |
| NFR-3 | write path untouched (`write_speaker_labels`) | unchanged Rust tests | ✓ |
| NFR-4 | no `invoke`/`listen`/`fetch` in `TranscriptViewer`/`SelectionSpeakerMenu` (verified by read) | payload-only `onSaveSpeakers` assertions | ✓ |

QA evidence (round 2, re-run by evaluator): `npm run test` — 33 files, **314 tests, all pass**; `npm run lint`, `npm run type`, `npm run format:check` — clean.

## Positive notes

- `assignSpeakerToSegments` is exactly the planned generalization: `assignSpeaker` became a one-line delegate, trim/delete semantics preserved and newly tested (null/blank clears, no mutation, empty id list is a no-op). Do not "simplify" this back.
- The mapper reads structure only — no `getBoundingClientRect`, no layout — and the E1 fix kept this property: `coversText` is pure boundary-point arithmetic with a reused scratch range, still testable under jsdom and cheap on pointerup.
- The fixer's dispute of round 1's suggested E1 mechanism was correct on the DOM-spec merits (the backward `intersectsNode` scan would have re-found the rejected span) and the replacement is strictly better; the dispute was documented rather than silently deviated from.
- Capturing segment ids at pointer-up rather than at menu-confirm (the `PendingSelection` comment) correctly anticipates that pressing the popover collapses the browser selection — and the new document-level release handler reuses the same gate, so popover presses still cannot re-read a collapsed selection.
- The menu rendered *outside* the `<ol>` so its own clicks are not re-read as selections; document-level Escape/pointerdown/scroll listeners owned by a single effect with cleanup, and cleanup is explicitly tested for all three.
- Test fixtures mirror real post-`resegment` shape (leading-space Cyrillic sentence segments), and the filtered-view test asserts both the payload and the re-counted match status — thorough FR-8 coverage.
