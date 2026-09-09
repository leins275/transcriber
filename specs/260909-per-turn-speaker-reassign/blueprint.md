---
slug: 260909-per-turn-speaker-reassign
created: 2026-09-09
status: approved
base_ref: 0cdd13df2dc6ac7a4d1254bbd35e632feac954f3
---

# Blueprint: Per-turn speaker reassignment vs. rename-everywhere, made explicit

The single planning artifact for feature F5 of the 2026-09-09 batch. Requirements
(FRs) are what the evaluator judges against; the task blocks are what the scheduler
parses. Downstream agents receive this path as their `<blueprint>`.

## Summary

Today the speaker label above a transcript turn (`SpeakerTag`) resolves "type a
different name over an existing one" as **rename this speaker everywhere**; the only
way to give *this turn alone* to someone else is the hover-revealed row of
already-known names, and giving it to a *new* name is impossible from the tag. The
operator keeps renaming every turn by accident when they meant one. This feature makes
the two intents explicit at the moment they diverge: after editing an existing name,
the tag asks **"Only this turn"** or **"All N turns of <old name>"** before anything
is written, and nothing is ever written silently on focus loss. The data model
(`speakers.json` = flat `segment id -> name` map, saved wholesale) already supports
per-turn assignment to any name, so the change is UI-only — no Rust, no service, no
schema.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists and `apps/desktop/src-tauri/Cargo.toml` depends on `tauri` (Tauri 2).
- `web` — `apps/desktop/package.json` depends on `react` and `vite` (webview UI of the Tauri app; per the desktop profile, UI toolkits come from here). The UI is operator-facing, single-user: **internal-tool UI**.
- `cli` — `services/transcription/pyproject.toml` has `[project.scripts]` (`transcription-service`, `transcriber-mcp`). Matched and confirmed, but this feature touches no Python.

Unmatched: no `playwright`/`cypress` in `apps/desktop/package.json`, no `docker-compose*`, no Django.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 (Rust), commands in `src-tauri/src/commands/` | `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/src/commands/meetings.rs` (`set_speaker_labels`) |
| UI | React 18 + Vite 5, CSS modules | `apps/desktop/package.json`, `apps/desktop/src/components/SpeakerTag.module.css` |
| UI tests | vitest 2 + jsdom + Testing Library (`user-event`) | `apps/desktop/package.json`, `apps/desktop/src/components/TranscriptViewer.test.tsx` |
| Transcript domain logic (TS) | pure functions in `apps/desktop/src/lib/turns.ts` | `groupIntoTurns`, `assignSpeaker`, `renameSpeaker` |
| Service | Python FastAPI + CLI via `uv` | `services/transcription/pyproject.toml` — untouched here |
| Vault rules | Rust crate | `crates/vault/` — untouched here |

Makefile QA targets present: `format`, `lint`, `type`, `test` (no aggregate `qa`).

## Requirements

- **FR-1** (must): Editing the name on a turn that already has a speaker never writes on its own. Confirming a changed name (Enter) offers an explicit scope choice — **"Only this turn"** vs. **"All N turns of <old name>"**, where N is the number of turns currently attributed to the old name in this transcript (this one included) — and nothing is persisted until one is picked.
  - [ ] With `Speaker 2` on ≥2 turns, clicking the tag, replacing the name with `Anna` and pressing Enter shows two buttons whose accessible names are `Only this turn` and `All N turns of Speaker 2` (N the real count), and `onSaveSpeakers` has not been called yet.
  - [ ] Picking `Only this turn` saves a map where only that turn's segment ids carry `Anna`; every other `Speaker 2` segment keeps `Speaker 2`.
  - [ ] Picking `All N turns of Speaker 2` saves a map where every segment that held `Speaker 2` now holds `Anna` (existing `renameSpeaker` semantics, merge-on-collision included).
  - [ ] The typed name is trimmed before being written; a name identical to the current one commits nothing and closes the editor.
- **FR-2** (must): Per-turn reassignment to a **new** name (not yet present in the transcript) is reachable from the tag: "Only this turn" with a fresh name creates that speaker on exactly this turn's segments.
  - [ ] Starting from `{0: Maxim, 1: Maxim, 2: Maxim}` grouped as one turn, and a second Maxim turn elsewhere: choosing `Only this turn` with `Anna` on the first turn yields `{0: Anna, 1: Anna, 2: Anna, …rest unchanged}` and the list re-groups showing an `Anna` turn.
  - [ ] Typing an already-known name (e.g. `Anna` exists) and choosing `Only this turn` attributes the turn to that existing name (no chooser variant needed — same path).
- **FR-3** (must): The choice can be abandoned without side effects.
  - [ ] Escape while the input or the scope chooser has focus closes the editor, restores the original label and calls `onSaveSpeakers` never.
  - [ ] Focus leaving the tag entirely (click elsewhere, Tab out) while an unconfirmed edit or the chooser is open discards the edit — no save, original label back. Focus moving *within* the tag (input → chooser buttons) does not discard.
- **FR-4** (must): The scope chooser is keyboard-operable and states its consequences.
  - [ ] When the chooser opens, focus lands on `Only this turn`; Tab reaches `All N turns of …`; Enter/Space on either activates it.
  - [ ] While editing an existing name the hint no longer claims "renames every segment"; it tells the operator the scope is chosen next (e.g. `Enter, then choose the scope`).
- **FR-5** (must): Turns that would make the two choices identical skip the chooser.
  - [ ] When the old name is held by exactly one turn (N ≤ 1), Enter commits the new name to this turn directly (`onAssign`) — the resulting map is the same either way — and the hint while editing says `this turn only · Enter to save`.
- **FR-6** (must): Existing tag behaviour that is not about this ambiguity is unchanged.
  - [ ] An unattributed turn (`Add speaker`) still names itself on Enter or blur via `onAssign`, with the hint `Enter to name`.
  - [ ] Clearing the box to empty on any turn still unattributes just this turn (`onAssign(null)`), no chooser.
  - [ ] The hover-revealed `Attribute this turn to <name>` buttons still assign this turn only, unchanged in markup and labels (F6 rewrites that list; F5 must not touch it).
  - [ ] `SelectionSpeakerMenu` and the selection flow are untouched (F4 owns that file).
- **FR-7** (should): The count is computed once per transcript in domain code, not per tag.
  - [ ] `speakerTurnCounts(turns)` in `lib/turns.ts` returns `{ name -> number of turns }` for every attributed name, ignoring `null` turns, and is pure.

**Non-functional**:

- **NFR-1**: `npm --prefix apps/desktop run test` stays model-free and completes with no new timers or network; new tests use Testing Library queries by role/label only.
- **NFR-2**: No new dependencies in `apps/desktop/package.json`.

## Out of scope

- Any change to `SelectionSpeakerMenu.tsx` (F4: viewport clamp) or to how the known-name list / project suggestions are rendered in either component (F6: project speaker roster).
- Rust/Tauri changes: `set_speaker_labels` already replaces the map wholesale; `speakers.rs` (diarization/backfill) is unrelated.
- Undo/history of speaker edits, multi-select of turns, drag-reorder.
- Renaming a speaker across *other meetings* of the project (project-level rename).
- Persisting anything about the chooser (no "remember my choice"); the chooser appears every time N ≥ 2.
- Localising the UI strings (the app's UI is English throughout).

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task; **mandatory** (desktop, web and cli profiles all name it).
- `testing-toolkit:python-testing-patterns` — Python service tests (pytest signal present in `services/transcription`); no task in this feature touches Python, so no task lists it.
- `frontend-toolkit:internal-ui` — internal-tool React UI; **mandatory** per the web profile. **Not installed on this machine** — listed so agents that have it apply it; others follow the existing component conventions in `SpeakerTag.tsx` / `SelectionSpeakerMenu.tsx` (presentational, role/label-named controls, CSS-module tokens) as the house style.
- `frontend-toolkit:ui-ux-pro-max` — internal-tool React UI (same row); not installed, same degradation.

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`

## Architecture

All changes live in the React layer under `apps/desktop/src/`. Data flow is unchanged:
`TranscriptViewer` holds the `segment id -> name` map in state, derives `turns` with
`groupIntoTurns`, renders one `SpeakerTag` per turn, and persists the whole map via
`onSaveSpeakers` → `RecordingPage` → Tauri `set_speaker_labels` → `speakers.json`
(`commands/meetings.rs`, atomic write). `assignSpeaker`/`renameSpeaker` in
`lib/turns.ts` already produce both target maps.

**`apps/desktop/src/lib/turns.ts`** — add `speakerTurnCounts(turns: Turn[]): Record<string, number>` next to `speakerNames`. Pure; one pass.

**`apps/desktop/src/components/SpeakerTag.tsx`** — new prop `turnsHeld?: number` (how many turns `speaker` currently holds across the transcript, this one included; default 1). Editing state machine becomes three states: `idle` → `editing` → (`choosing` | commit | cancel):

- `speaker === null`: unchanged — Enter/blur → `onAssign(trimmed)` or `onAssign(null)` when empty.
- `speaker !== null`, trimmed empty → `onAssign(null)` (unchanged).
- `speaker !== null`, trimmed === speaker → close, no callback (unchanged).
- `speaker !== null`, trimmed differs, `turnsHeld <= 1` → `onAssign(trimmed)` on Enter (FR-5).
- `speaker !== null`, trimmed differs, `turnsHeld >= 2` → Enter enters `choosing`: the input stays visible (read-only or disabled is fine; keep the draft on screen), the hint is replaced by two buttons — `Only this turn` → `onAssign(trimmed)`; `All {turnsHeld} turns of {speaker}` → `onRename(speaker, trimmed)`. First button auto-focused.
- Blur handling moves from the input to the wrapping `<span className={styles.tag}>` via `onBlur` with a `relatedTarget` containment check: focus leaving the tag while `editing` (existing speaker) or `choosing` → cancel; while `editing` an unattributed turn → commit as today. Escape in either state → cancel.
- `aria-label` of the input becomes `Edit speaker for this turn` for an existing speaker (the old `Rename <name>` label is now wrong; the unattributed label `Name this speaker` stays).
- The doc comment at the top of the file is rewritten to describe the new resolution (it currently documents the rename-everywhere rule as the design).

**`apps/desktop/src/components/SpeakerTag.module.css`** — one `.scope` container and a `.scopeButton` style for the chooser, reusing existing tokens (`--accent`, `--divider`, `--radius-sm`, 10–11 px uppercase label style like `.other`). No layout change to `.tag`.

**`apps/desktop/src/components/TranscriptViewer.tsx`** — `const turnCounts = useMemo(() => speakerTurnCounts(turns), [turns])`; pass `turnsHeld={turn.speaker === null ? 0 : (turnCounts[turn.speaker] ?? 0)}` to each `SpeakerTag`. Everything else (selection flow, `assignSelection`, `SelectionSpeakerMenu` call) untouched.

**Sibling merge hygiene**: F4 edits only `SelectionSpeakerMenu.tsx` — no overlap. F6 edits the `known.filter(...).map(...)` block and the datalist/suggestions in both components; F5 confines its `SpeakerTag.tsx` edits to the props type, the doc comment, the `commit`/`cancel`/state logic and the `editing` render branch, and leaves the idle-branch offer list byte-identical. `TranscriptViewer.tsx` gets one `useMemo` and one prop on the `<SpeakerTag>` call.

**Risks**:

- *jsdom focus semantics for the leave-the-tag rule (FR-3).* jsdom fires `focusout` with `relatedTarget` when `element.focus()` moves focus, and `user-event` clicks focus focusable targets; the test file should drive focus loss by clicking/tabbing to another focusable control (e.g. the Find box) or `fireEvent.blur(el, { relatedTarget: null })`, never by asserting on internal state. Mitigated by T2's test-case list.
- *Regression of the existing rename test* (`TranscriptViewer.test.tsx :: "renaming a speaker renames every segment they hold"` queries `/rename speaker 2/i` and expects an immediate save). T3's test file rewrites that case to go through the chooser. The `renaming after a sub-turn assignment` case likewise.
- *F6 merge conflicts in `SpeakerTag.tsx`.* Kept to disjoint regions (see hygiene above); T2 must not reformat or reorder the idle-branch JSX.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2 |
| 2 | T3 |

## Tasks

### [x] T1: `speakerTurnCounts` in the turns library  [deps: —]

- **Files**: `apps/desktop/src/lib/turns.ts`
- **Test first**: `apps/desktop/src/lib/turns.test.ts` — add a `describe("speakerTurnCounts")` block (existing cases stay): counts one entry per attributed name equal to the number of *turns* (not segments) holding it — e.g. Maxim on turns of 3 and 2 segments → `{ Maxim: 2 }` (FR-7); unattributed (`null`) turns contribute nothing and appear under no key (FR-7); returns `{}` for no turns and for only-unattributed turns (FR-7); does not mutate the turns array (FR-7). Build turns through `groupIntoTurns` with the existing `seg` helper so the test states behaviour, not the `Turn` shape.
- **Implement**: One-pass fold over `turns` next to `speakerNames`, doc-commented like its neighbours ("turns, not segments — the count is what the tag reads back to the operator"). Export it.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: new cases pass via `npm --prefix apps/desktop run test -- src/lib/turns.test.ts`; `make lint`, `make type`, `make test` green.

### [x] T2: Scope chooser in `SpeakerTag`  [deps: —]

- **Files**: `apps/desktop/src/components/SpeakerTag.tsx`, `apps/desktop/src/components/SpeakerTag.module.css`
- **Test first**: `apps/desktop/src/components/SpeakerTag.test.tsx` (new file; render `SpeakerTag` directly with `vi.fn()` callbacks, `known`, `turnsHeld`) — cases:
  - editing `Speaker 2` with `turnsHeld=3`, typing `Anna{Enter}` shows buttons named `Only this turn` and `All 3 turns of Speaker 2`, and neither `onAssign` nor `onRename` has been called (FR-1);
  - clicking `Only this turn` calls `onAssign("Anna")` once and `onRename` never; the editor closes (FR-1, FR-2);
  - clicking `All 3 turns of Speaker 2` calls `onRename("Speaker 2", "Anna")` once and `onAssign` never (FR-1);
  - the typed name is trimmed (`"  Anna "` → `Anna`) (FR-1);
  - retyping the same name and pressing Enter closes the editor with no callback and no chooser (FR-1);
  - when the chooser opens, `Only this turn` has focus; `{Tab}` moves focus to the `All … turns` button; `{Enter}` there fires `onRename` (FR-4);
  - hint while editing an existing name with `turnsHeld=3` does not contain "every segment" and mentions choosing the scope (FR-4);
  - `turnsHeld=1`: `Anna{Enter}` calls `onAssign("Anna")` directly, no chooser rendered, and the hint reads `this turn only` (FR-5);
  - Escape in the input, and Escape while the chooser is open, restore the `Speaker 2` label with no callback (FR-3);
  - focus leaving the tag while the chooser is open (render a sibling `<button>Elsewhere</button>` in the test and `user.click` it) discards: label restored, no callback (FR-3); focus leaving while the input holds an unconfirmed changed name discards likewise (FR-3);
  - `speaker=null`: `Maxim{Enter}` calls `onAssign("Maxim")`; blur also commits; hint `Enter to name` (FR-6);
  - clearing the box on an existing speaker and pressing Enter calls `onAssign(null)`, no chooser (FR-6);
  - the `Attribute this turn to <name>` buttons still call `onAssign(name)` (FR-6).
- **Implement**: Per the Architecture section: `turnsHeld` prop, `editing | choosing` state, `commit()` branching on `speaker`, `trimmed` and `turnsHeld`; chooser rendered in place of the hint with `autoFocus`/`ref` focus on the first button; tag-level `onBlur` with `event.currentTarget.contains(event.relatedTarget as Node | null)`; Escape handled on the tag wrapper's `onKeyDown` so it works from the chooser too; input `aria-label` → `Edit speaker for this turn`; rewrite the component doc comment. Add `.scope`/`.scopeButton` CSS using existing tokens. Do not touch the idle-branch offer list or the datalist markup (F6 territory).
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/SpeakerTag.test.tsx` green; `make format`, `make lint`, `make type` green; `git diff` of the idle-branch JSX in `SpeakerTag.tsx` is empty apart from the doc comment.

### [x] T3: Wire the count through `TranscriptViewer` and update its speaker-flow tests  [deps: T1, T2]

- **Files**: `apps/desktop/src/components/TranscriptViewer.tsx`
- **Test first**: `apps/desktop/src/components/TranscriptViewer.test.tsx` — modify the two existing rename cases and add end-to-end (viewer + tag + turns) cases; all selection cases stay untouched:
  - rewrite `renaming a speaker renames every segment they hold`: with `buildTranscript({ speakers: {0: Speaker 2, 1: Speaker 2} })` (segments 0 and 1 are 122 s apart, so two turns) open the first tag (input labelled `Edit speaker for this turn`), type `Anna{Enter}`, click `All 2 turns of Speaker 2` → `onSaveSpeakers({0: Anna, 1: Anna})` (FR-1);
  - new: same setup, click `Only this turn` → `onSaveSpeakers({0: Anna, 1: Speaker 2})` and the list now shows buttons `Anna` and `Speaker 2` (FR-1, FR-2);
  - new: per-turn reassignment to a brand-new name on a multi-segment turn — segments 0 and 1 adjacent, segment 2 two minutes later, all `Maxim` (two turns): first tag, `Anna{Enter}`, `Only this turn` → `onSaveSpeakers({0: Anna, 1: Anna, 2: Maxim})` (FR-2);
  - new: the chooser count reflects turns, not segments — three Maxim segments in two turns → button named `All 2 turns of Maxim` (FR-1, FR-7);
  - new: a speaker held by one turn commits directly — `{0: Maxim, 1: Anna}`, edit Maxim → `Max{Enter}` → `onSaveSpeakers({0: Max, 1: Anna})` with no chooser (FR-5);
  - new: Escape from the chooser saves nothing and keeps the labels (FR-3);
  - new: clicking the Find box while the chooser is open saves nothing (FR-3);
  - rewrite `renaming a speaker after a sub-turn assignment renames the new segments too` to go through `All 2 turns of Anna` (FR-1, FR-6);
  - keep `names an unattributed speaker …`, `surfaces a failed save …`, `still attributes a whole turn from its tag …` as they are (FR-6 — they must remain green unmodified).
- **Implement**: `import { speakerTurnCounts }`; `const turnCounts = useMemo(() => speakerTurnCounts(turns), [turns])`; pass `turnsHeld` on the `<SpeakerTag>` call. Nothing else in the file changes.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/TranscriptViewer.test.tsx` green with every pre-existing selection case unmodified; `make format lint type test` green; desktop-profile smoke: `cd apps/desktop && npm run tauri dev`, open a labelled transcript, edit a name held by several turns, confirm the chooser appears with the right count and that `Only this turn` changes one turn in `speakers.json` while `All N turns` changes all — and that clicking away writes nothing.

## QA expectations

No aggregate `make qa`; the gate runs `make format`, `make lint`, `make type`, `make test` — all four exist and fan out over cargo, npm and uv. This feature only changes files under `apps/desktop/src/`, so the relevant slices are `npm --prefix apps/desktop run lint|type|test` (eslint, `tsc --noEmit`, vitest), but the full targets must pass. `make lint` also runs `sync_version --check`, `verify_locks --check` and `gen_diarization_runtime --check` — none affected. Nothing known-flaky in the vitest suite; the Python suite is model-free and network-free (<30 s). The opt-in `pytest -m gpu` test self-skips.

## Assumptions & decisions
- 2026-09-09 — (OPERATOR) Shape of the two intents → A, scope chooser after Enter ("Only this turn" / "All N turns of <name>"); separate rename icon and confirm-on-rename rejected.

- 2026-09-09 — (AUTO: codebase) Is a data-model change needed for per-turn reassignment to a new name? → No. `speakers.json` is a flat `segment id -> free-form name` map written wholesale by `set_speaker_labels` (`commands/meetings.rs`); `assignSpeaker(speakers, turn, "NewName")` already produces the right map. UI-only feature; no Rust/Python tasks.
- 2026-09-09 — (AUTO: web profile) Internal-tool or public UI? → Internal: single-user local desktop app, operator-facing. `frontend-toolkit:internal-ui` applies (and is a strict skill) but is not installed here; agents without it follow the existing component conventions.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test style → observable behaviour through Testing Library roles/labels and the `onAssign`/`onRename`/`onSaveSpeakers` boundary callbacks; no mocking of in-process collaborators (`lib/turns` runs for real in the viewer tests); one behaviour per test; names in plain language matching the existing suites.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) `SpeakerTag` gets its own test file rather than piling every state onto `TranscriptViewer.test.tsx` — the tag's state machine is the domain-significant logic of this feature and is cheapest to exercise directly; the viewer file keeps the integration paths.
- 2026-09-09 — (ASSUMPTION) Interaction shape → a scope chooser *after* the edit ("Only this turn" / "All N turns of <name>") rather than two separate entry points on the tag. Chosen because it is one control, states the blast radius as a number at the moment of decision, and covers reassignment to a new name without a second icon. See the open question — the plan is written for this shape.
- 2026-09-09 — (ASSUMPTION) Default focus in the chooser → `Only this turn` (the narrower, non-destructive option) so a reflexive second Enter never renames everything.
- 2026-09-09 — (ASSUMPTION) Focus leaving the tag with an unconfirmed edit on an *existing* speaker discards the edit (no silent write); the unattributed-turn path keeps committing on blur as it does today.
- 2026-09-09 — (ASSUMPTION) N ≤ 1 skips the chooser and commits as a this-turn assignment — both choices produce the identical map, and asking would be noise.
- 2026-09-09 — (ASSUMPTION) The count in the chooser is in *turns* (the operator's word, "реплики"), not segments.
- 2026-09-09 — (ASSUMPTION) UI strings stay English, matching the rest of the app.
