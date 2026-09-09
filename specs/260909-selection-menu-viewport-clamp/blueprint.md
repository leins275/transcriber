---
slug: 260909-selection-menu-viewport-clamp
created: 2026-09-09
status: approved
base_ref: <git sha, recorded at blueprint approval>
---

# Blueprint: Keep the selection speaker popover inside the window

Requirements and task plan for feature F4 of the 2026-09-09 batch. The
evaluator judges against the FRs below; the scheduler parses the task blocks.

## Summary

Selecting transcript text with the mouse opens `SelectionSpeakerMenu`, a
`position: fixed` popover placed at the pointer-release point
(`left: anchor.x; top: anchor.y`, centred and nudged down by a CSS
`transform`). It never measures itself against the window, so a selection
ending near the right or bottom edge pushes the speaker buttons and the
name box off-screen. The fix measures the popover and places it inside the
viewport — flipping above the anchor when there is no room below, otherwise
sliding it in — and re-places it when the anchor or the window size
changes. Pure UI, no new dependencies; the placement maths is a standalone
function under `src/lib/` so it is unit-testable without a browser, and the
component change is confined to positioning so the parallel F5/F6 branches
(which rewrite the picker list in the same component) merge cleanly.

## Profiles

- `desktop` — Tauri: `apps/desktop/src-tauri/tauri.conf.json`; UI toolkits taken from `web` per the profile's own rule.
- `web` — browser UI: `apps/desktop/package.json` names `react` and `vite`. Internal-tool UI (single-user desktop app, not a customer surface).
- `cli` — `[project.scripts]` in `services/transcription/pyproject.toml` (`transcription-service`, `transcriber-mcp`). Matched but untouched by this feature: no task carries a CLI skill.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 (Rust) | `apps/desktop/src-tauri/tauri.conf.json`, `@tauri-apps/cli` in `apps/desktop/package.json` |
| UI | React 18 + Vite 5, TypeScript 5, CSS modules | `apps/desktop/package.json`, `apps/desktop/src/components/*.module.css` |
| UI tests | vitest 2 + jsdom + Testing Library (+ jest-dom, user-event) | `apps/desktop/vite.config.ts` (`test.environment: "jsdom"`), `apps/desktop/src/test/setup.ts` |
| UI lint/format | eslint 9 (typescript-eslint, react-hooks) / prettier 3 | `apps/desktop/eslint.config.js`, `apps/desktop/package.json` scripts |
| Service | Python FastAPI sidecar (`uv`) | `services/transcription/pyproject.toml` — not touched |
| Vault rules | Rust crate | `crates/vault/` — not touched |

Makefile QA targets present: format, lint, type, test (no aggregate `qa`).
Only the `apps/desktop` legs are exercised by this feature: `npm --prefix apps/desktop run format|lint|type|test`.

## Requirements

- **FR-1** (must): The popover's placement is computed by a pure function `clampToViewport(anchor, size, viewport, options?)` in `apps/desktop/src/lib/viewportPosition.ts` that keeps a box of `size` inside `viewport` with a margin.
  - [ ] With room on every side it returns the box centred horizontally on `anchor.x` and `gap` px below `anchor.y` (`left = anchor.x - width/2`, `top = anchor.y + gap`) — the placement the CSS transform gave before.
  - [ ] A box that would cross the right edge is slid left so its right side sits `margin` px inside `viewport.width`; one that would cross the left edge is slid right to `left = margin`.
  - [ ] A box that would cross the bottom edge is flipped above the anchor (`top = anchor.y - gap - height`) when that fits below the top margin; when neither side fits it is slid up so its bottom sits `margin` px inside `viewport.height`; `top` is never below `margin`.
  - [ ] A box wider than the viewport minus margins is pinned to `left = margin` (never a negative or NaN coordinate).
  - [ ] A zero `size` (not yet measured) yields `{ left: anchor.x, top: anchor.y + gap }` clamped as above, so the first paint is never off-screen.
  - [ ] `options.margin` and `options.gap` default to 8 px each.
- **FR-2** (must): `SelectionSpeakerMenu` positions itself with that function, using its own rendered size and the window's inner size.
  - [ ] Rendered with a stubbed 200x40 px box in a 1000x800 window at anchor `(950, 300)`, the popover element (`role="group"`) has inline `left: 792px; top: 308px`.
  - [ ] Same box at anchor `(500, 780)` renders `top: 732px` (flipped above).
  - [ ] Same box at anchor `(500, 300)` renders `left: 400px; top: 308px` (centred, i.e. no double offset from a leftover CSS transform).
  - [ ] The measured placement is applied before the first paint (layout effect) — no frame at the raw anchor.
- **FR-3** (must): The popover re-places itself when its anchor or the window changes.
  - [ ] Re-rendering with a new `anchor` moves the popover to the clamped position for that anchor.
  - [ ] A `resize` event on `window` after the window shrinks recomputes the position against the new `innerWidth`/`innerHeight`.
  - [ ] The resize listener is removed on unmount.
- **FR-4** (must): Everything else about the popover is unchanged — buttons, new-name input, datalist suggestions, Escape / click-away / scroll dismissal — and the existing `SelectionSpeakerMenu.test.tsx` and `TranscriptViewer.test.tsx` suites pass untouched.
  - [ ] `npm --prefix apps/desktop run test` is green with no edits to existing test files.

**Non-functional**:

- **NFR-1**: No new npm dependency; `apps/desktop/package.json` and `package-lock.json` are unchanged.
- **NFR-2**: Exactly one layout read (`getBoundingClientRect`) per anchor change and per resize event — no per-frame measurement, no `ResizeObserver` polling.

## Out of scope

- Following the selection on scroll (the popover still dismisses on scroll — see the comment in `SelectionSpeakerMenu.tsx`).
- Anchoring to the selection's bounding rectangle instead of the pointer-release point (`TranscriptViewer.tsx` `readSelection` deliberately reads no geometry).
- Any change to the picker's contents or behaviour (F5 `per-turn-speaker-reassign`, F6 `project-speaker-roster` own those).
- `SpeakerTag`'s own popover, which is absolutely positioned inside its trigger and not reported as clipping.
- Clamping any other fixed-position surface (`RecordingPage.module.css`).

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task; **mandatory** (desktop, web, cli profiles).
- `frontend-toolkit:internal-ui` — UI (internal) layer; **mandatory** per the `web` profile on every internal-tool UI task. Not installed on this machine — listed so agents that have it apply it; others proceed on the component's existing conventions.
- `frontend-toolkit:ui-ux-pro-max` — UI (internal) layer (web profile row; signal React/Vite present). Not installed; same degradation.
- `testing-toolkit:python-testing-patterns` — Tests row (pytest present in `services/transcription`); no task in this feature touches Python, so unassigned.
- `devops-toolkit:devops-rollout-plan` — Packaging/Release rows (`tauri.conf.json` bundle, console scripts); unassigned, nothing here ships differently.

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`

## Architecture

Three files change or appear, all under `apps/desktop/src/`:

- **`lib/viewportPosition.ts`** (new) — pure placement maths, no DOM, no React, following the `lib/` convention (`selection.ts`, `turns.ts`: pure modules with a sibling `.test.ts`).
  ```ts
  export type Point = { x: number; y: number };
  export type Size = { width: number; height: number };
  export type Placement = { left: number; top: number };
  export function clampToViewport(
    anchor: Point,
    size: Size,
    viewport: Size,
    options?: { margin?: number; gap?: number },
  ): Placement;
  ```
  Order of operations: preferred = centred below; flip above when the bottom overflows and above fits; then clamp `left` to `[margin, viewport.width - margin - width]` and `top` to `[margin, viewport.height - margin - height]` with the lower bound winning when the range is empty.
- **`state/useClampedPosition.ts`** (new) — `useClampedPosition(anchor: Point, ref: RefObject<HTMLElement>): Placement`. Lives in `state/` beside the app's other `use*` hooks. Initial state is `clampToViewport(anchor, {0,0}, windowSize())`; a `useLayoutEffect` keyed on `anchor.x`/`anchor.y` measures `ref.current.getBoundingClientRect()` and sets the placement before paint; a `useEffect` subscribes to `window` `resize` and re-measures, unsubscribing on cleanup.
- **`components/SelectionSpeakerMenu.tsx`** — replaces `style={{ left: anchor.x, top: anchor.y }}` with the hook's `{ left, top }` on the existing `menuRef`. No other line changes (F5/F6 merge hygiene).
- **`components/SelectionSpeakerMenu.module.css`** — the `.menu` rule drops `transform: translate(-50%, var(--space-2))` (centring and the gap now come from the hook; leaving it would double-shift) and its comment says so. Nothing else in the file moves.

Data flow: `TranscriptViewer` still passes `anchor = { clientX, clientY }` of the pointer release (unchanged) → hook measures the popover → `clampToViewport` → inline `left/top`.

**Risks**:
- jsdom returns a zero rect from `getBoundingClientRect` and has no layout; T2's tests stub the rect on `HTMLElement.prototype` and set `window.innerWidth/innerHeight` directly (both writable in jsdom). Mitigated by specifying those stubs in T2's test contract.
- Removing the CSS transform without the hook (or vice versa) mis-positions the popover; both live in T2 so they land together.
- Merge overlap with F5/F6: the component edit is a single line plus one import, the CSS edit a single rule. Kept minimal on purpose.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1 |
| 2 | T2 |

## Tasks

### [ ] T1: Pure viewport placement function  [deps: —]

- **Files**: `apps/desktop/src/lib/viewportPosition.ts`
- **Test first**: `apps/desktop/src/lib/viewportPosition.test.ts` — plain vitest (`describe/it/expect`, no DOM) like `lib/selection.test.ts`. Fixtures: box 200x40, viewport 1000x800, `{ gap: 8, margin: 8 }` unless stated. Cases: (1) FR-1 centred below — anchor (500,300) → `{ left: 400, top: 308 }`; (2) FR-1 right edge — anchor (950,300) → `left: 792`, `top: 308`; (3) FR-1 left edge — anchor (50,300) → `left: 8`; (4) FR-1 flip above — anchor (500,780) → `top: 732`; (5) FR-1 no room either side — viewport 1000x60, anchor (500,50) → `top: 12`; (6) FR-1 top never below margin — viewport 1000x60, anchor (500,4), box 200x40 → `top: 12` (would-be 12 anyway; assert `>= 8` and equal to 12); (7) FR-1 wider than viewport — box 1200x40 → `left: 8`; (8) FR-1 zero size — box 0x0, anchor (500,300) → `{ left: 500, top: 308 }`; (9) FR-1 defaults — call without `options` for case (1) → same result. Hardcode every expected number; do not recompute them in the test.
- **Implement**: Export the types and `clampToViewport` as specified in Architecture. Preferred placement, optional flip, then clamp each axis with `Math.max(margin, Math.min(value, max))`. Doc comment states the order of operations and why flipping beats sliding (sliding up covers the very text just selected).
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: `viewportPosition.test.ts` green; `npm --prefix apps/desktop run lint`, `run type`, `run format:check` clean for the new file.

### [ ] T2: Hook + wire the popover to the clamped position  [deps: T1]

- **Files**: `apps/desktop/src/state/useClampedPosition.ts`, `apps/desktop/src/components/SelectionSpeakerMenu.tsx`, `apps/desktop/src/components/SelectionSpeakerMenu.module.css`
- **Test first**: `apps/desktop/src/components/SelectionSpeakerMenu.position.test.tsx` — a new file (the existing `SelectionSpeakerMenu.test.tsx` stays untouched). Render `SelectionSpeakerMenu` via Testing Library as the sibling test does; in `beforeEach` set `window.innerWidth = 1000; window.innerHeight = 800` and `vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 200, height: 40, ... } as DOMRect)`; restore in `afterEach`. Locate the popover with `screen.getByRole("group", { name: /attribute the selected text/i })` and assert `element.style.left` / `.top` strings. Cases: (1) FR-2 right edge — anchor (950,300) → `left "792px"`, `top "308px"`; (2) FR-2 bottom flip — anchor (500,780) → `top "732px"`; (3) FR-2 centred — anchor (500,300) → `left "400px"`; (4) FR-3 anchor change — `rerender` with anchor (950,300) after (500,300) → `left "792px"`; (5) FR-3 resize — after render at (500,300) set `window.innerWidth = 600` then `fireEvent(window, new Event("resize"))` → `left "392px"`; (6) FR-3 unmount — `unmount()`, then resize: no error thrown and no further `getBoundingClientRect` call (spy call count unchanged). Assert only the rendered style, never hook internals.
- **Implement**: `useClampedPosition` per Architecture (`useState` + `useLayoutEffect` on `[anchor.x, anchor.y]` + `useEffect` resize subscription; one `measure()` closure shared by both; `windowSize()` reads `innerWidth/innerHeight`). In `SelectionSpeakerMenu.tsx` import the hook, call `const placement = useClampedPosition(anchor, menuRef)`, and set `style={placement}`; touch nothing else. In the CSS `.menu` rule delete the `transform` line and rewrite its comment: centring and the below-anchor gap are computed by `useClampedPosition` so the popover can be kept inside the window.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `SelectionSpeakerMenu.position.test.tsx` green and the pre-existing `SelectionSpeakerMenu.test.tsx` / `TranscriptViewer.test.tsx` unchanged and green; `make format`, `make lint`, `make type`, `make test` pass; manual check in `cd apps/desktop && npm run tauri dev`: open a diarized meeting, select text whose drag ends within ~50 px of the right edge and of the bottom edge of the window — the popover is fully visible in both cases and still dismisses on Escape/click-away/scroll.

## QA expectations

No aggregate `make qa`. The gate runs `make format`, `make lint`, `make type`, `make test` — each fans out over cargo, npm and uv; only the npm legs (`prettier`, `eslint`, `tsc --noEmit`, `vitest run`) can be affected by this feature. `make` exists only after `scripts/bootstrap.ps1` has run; the direct npm equivalents are `npm --prefix apps/desktop run format|lint|type|test`. Nothing known-flaky in the vitest suite. `make test` also runs the Python suite (model-free, <30 s) and cargo tests — unaffected but part of the gate.

## Assumptions & decisions

- 2026-09-09 — (AUTO: codebase) Where does the pure helper live? → `apps/desktop/src/lib/viewportPosition.ts`, matching every other pure module in `lib/` with a sibling `.test.ts`; the hook goes to `state/` beside `useJobs`/`useChat`/`useUpdate`, the only `use*` directory.
- 2026-09-09 — (AUTO: codebase) Flip or slide when the bottom overflows? → Flip above the anchor when it fits, else slide. Sliding up alone would cover the selected text the operator is reading (the component comment stresses the highlight is the only cue of what is about to be attributed).
- 2026-09-09 — (AUTO: codebase) Keep the CSS `transform: translate(-50%, var(--space-2))`? → No; the hook computes centring and the gap in JS so the measured rect and the applied coordinates are in one coordinate system. Gap default 8 px replaces the 9.2 px `--space-2` — a sub-pixel difference nobody will see.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test the hook in isolation with `renderHook`? → No; test observable behaviour through the component's rendered `left/top`, plus the pure function exhaustively. Stubbing `getBoundingClientRect` and `innerWidth` is stubbing the (absent) layout engine, not an in-process collaborator.
- 2026-09-09 — (AUTO: intake) Merge hygiene with F5/F6 → the component changes one `style` prop and one import; the CSS change is one line of the `.menu` rule; all logic lives in new files.
- 2026-09-09 — (ASSUMPTION) Anchor stays the pointer-release point rather than the selection's rectangle → kept as is; `readSelection` reads no geometry by design and the request is only about staying on-screen. Flipping above the release point may cover the last selected line in the rare bottom-edge case; accepted.
- 2026-09-09 — (ASSUMPTION) Margin 8 px on every side → a viewport margin close to the app's `--space-2`; no operator preference exists to consult.
