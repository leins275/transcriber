---
slug: 260909-project-speaker-roster
created: 2026-09-09
status: approved
base_ref: <git sha, recorded at blueprint approval>
---

# Blueprint: Project speaker roster

## Summary

Naming a voice in a transcript today offers a free-text box with the project's sibling-meeting names as a `datalist` hint, so every typo or variant spelling quietly becomes a new speaker. This feature adds a maintained, per-project **roster** — a plain list of allowed speaker names, with a mode switch: **open** (today's behaviour, for a young project whose cast is still forming) or **roster** (the name controls in `SpeakerTag` and `SelectionSpeakerMenu` become a pick-list over the roster, no free text). The roster is a *name list only*, stored as `<vault root>/<PROJECT>/roster.json`; it holds no voice data, so it does not resurrect the deliberately rejected per-project voice store (`<PROJECT>/speakers.json`) — voice memory stays the on-demand sibling scan and the service is untouched.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists and `apps/desktop/src-tauri/Cargo.toml` depends on `tauri` (Tauri 2); bundle config present (NSIS).
- `web` — `apps/desktop/package.json` names `react` and `vite` (browser-rendered UI inside the webview). Internal-tool UI: single-user, operator-facing desktop app, no public surface.
- `cli` — `services/transcription/pyproject.toml` has `[project.scripts]` (`transcription-service`, `transcriber-mcp`) and `argparse`-style entry points. Matched, but no task in this feature touches the Python payload.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2, Rust (`transcriber_desktop_lib`) | `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/Cargo.toml` (`[lib] name = "transcriber_desktop_lib"`) |
| UI | React 18 + Vite 5 + TypeScript, CSS modules | `apps/desktop/package.json`, `apps/desktop/src/components/*.module.css` |
| UI tests | vitest 2 + jsdom + Testing Library + `@tauri-apps/api/mocks` | `apps/desktop/vite.config.ts` (`test.environment: "jsdom"`, `setupFiles: src/test/setup.ts`), `apps/desktop/src/App.test.tsx` (`mockIPC`), `apps/desktop/src/state/useChat.test.ts` (`vi.mock("../api")`) |
| Rust tests | in-module `#[cfg(test)]` plus integration tests with a shared harness | `apps/desktop/src-tauri/tests/common/mod.rs` (`build_state`, `new_tempdir`, `run`), `apps/desktop/src-tauri/tests/e2e_flow.rs` |
| Vault rules | Rust library crate `vault` | `crates/vault/src/paths.rs` (`CHATS_DIR_NAME`, `RESERVED_PROJECT_DIR_NAMES`), `crates/vault/src/list.rs` |
| Service | Python 3 / FastAPI, `uv` | `services/transcription/pyproject.toml` — not touched here |

Makefile QA targets present: `format`, `lint`, `type`, `test` (no aggregate `qa`; `make -n` confirms each of the four fans out across cargo / npm / uv).

## Requirements

- **FR-1** (must): A project can carry a speaker roster — an ordered list of allowed names plus a mode, `open` or `roster` — persisted in the vault as `<vault root>/<PROJECT>/roster.json` (`{"schema_version": 1, "mode": "open" | "roster", "names": [...]}`), readable and writable through two Tauri commands.
  - [ ] `read_project_roster(project)` on a project with no `roster.json` returns `{ mode: "open", names: [] }` — absence is the normal state, not an error.
  - [ ] `save_project_roster(project, roster)` writes `<root>/<PROJECT>/roster.json` atomically (temp file + rename, the `chats.rs` pattern) and a subsequent read returns the same mode and names.
  - [ ] A malformed or oversized `roster.json` reads as `{ mode: "open", names: [] }` rather than failing the call (degradation over failure).
  - [ ] `roster.json` is a file at project level, so `vault::list_meetings`, the Python indexer and the MCP server keep skipping it (they only consider directories); no listing exclusion is added and the file holds no embeddings.
  - [ ] The file name is a single constant, `vault::ROSTER_FILE_NAME == "roster.json"`, exported from `crates/vault` and documented in its layout doc comment.
- **FR-2** (must): Roster names are normalized identically on both sides of the IPC boundary — trimmed, blanks dropped, deduplicated case-insensitively keeping the first spelling, original order preserved.
  - [ ] Saving `["  Anna ", "anna", "", "Maxim", "Maxim"]` stores and returns `["Anna", "Maxim"]`.
  - [ ] A name longer than 200 characters, or more than 500 names, is rejected with `invalid_argument`; nothing is written.
  - [ ] The TypeScript `lib/roster.ts` helpers produce the same result for the same input, so the editor never shows a list the backend would change on save.
- **FR-3** (must): The project argument of both commands is validated exactly like `commands/chats.rs` validates it: one plain directory name that is an existing project under the meetings root.
  - [ ] `unsorted`, any name in `vault::RESERVED_PROJECT_DIR_NAMES`, an empty string, `.`, `..`, or anything containing `/`, `\` or `:` is rejected with `invalid_argument`.
  - [ ] A project directory that does not exist is rejected; saving never creates a project.
  - [ ] With no meetings root configured the commands fail with `not_configured`.
- **FR-4** (must): In **roster** mode, the speaker name controls offer only the roster: the free-text input in `SpeakerTag` and `SelectionSpeakerMenu` is replaced by a select whose options are the roster names.
  - [ ] `SpeakerTag` on an unattributed turn, given `roster: ["Anna", "Maxim"]`, shows a select labelled "Name this speaker" with a placeholder option plus exactly those names; choosing "Anna" calls `onAssign("Anna")` and leaves edit mode.
  - [ ] `SpeakerTag` on a turn attributed to "Maxim", given the same roster, shows a select labelled "Rename Maxim" preselected to "Maxim"; choosing "Anna" calls `onRename("Maxim", "Anna")` (the assign-vs-rename distinction is unchanged from today — a roster only changes where the name comes from).
  - [ ] When the current speaker's name is not in the roster (a label from before the roster existed, or a service pre-name), the select still lists it as the selected option so the control never displays the wrong name.
  - [ ] Escape in the select cancels without calling either callback; the in-transcript `known` buttons ("Attribute this turn to X") keep working unchanged.
  - [ ] `SelectionSpeakerMenu` given a roster shows a select labelled "Attribute selection to a speaker" with the roster names; choosing one calls `onAssign(name)`. No free-text input is rendered.
  - [ ] A roster-mode project with an empty roster renders the select with a single disabled option reading that the roster is empty, and no callback can fire from it.
- **FR-5** (must): In **open** mode (or for an `unsorted` meeting, which has no project), the name controls behave exactly as today: a free-text input with the sibling-scan names as a `datalist`.
  - [ ] With `roster` undefined, the existing `SelectionSpeakerMenu.test.tsx` and `TranscriptViewer.test.tsx` suites pass unchanged.
  - [ ] `TranscriptViewer` given `roster: { mode: "open", names: [...] }` passes `suggestedSpeakers` to the datalist and no select appears.
- **FR-6** (must): The recording page offers a roster editor for the open meeting's project.
  - [ ] A "Project speakers" button sits beside the project pill in the recording page's breadcrumb, only when `entry.project` is not `null`; it toggles an inline panel (the same `panel` mechanism as Rename / Delete).
  - [ ] The panel shows a radio group "Who can be named in this project" with "Anyone — type any name" (`open`) and "Only the roster below" (`roster`), the roster names each with a "Remove <name>" button, an "Add a name" input (Enter or an Add button appends, normalized per FR-2), an "Add names already used in this project" button that merges the sibling-scan names not yet present (disabled when there is nothing to add), and Save / Cancel.
  - [ ] Save calls `onSaveRoster(project, draft)`; on success the panel closes and the name controls in the transcript immediately reflect the new mode and names. A rejected save keeps the panel open and shows the error message in a `role="alert"`.
  - [ ] Cancel discards the draft; reopening shows the persisted roster.
- **FR-7** (must): `App.tsx` loads the roster of the open recording's project and passes it down.
  - [ ] Opening a recording in project P calls `read_project_roster` with `P` once and hands the result to `RecordingPage`; a failed read degrades to `{ mode: "open", names: [] }`.
  - [ ] Opening an `unsorted` recording never calls `read_project_roster`.
  - [ ] After a successful save the in-memory roster is replaced by the normalized view the command returned.
- **FR-8** (should): Docs and the codebase guidance record the new artifact.
  - [ ] `CLAUDE.md`'s speaker-identification paragraph states that `<PROJECT>/roster.json` is a name-only roster (mode + names, no embeddings) and that the "no `<PROJECT>/speakers.json`" decision stands.
  - [ ] `docs/setup.md` gains a short "Project speaker roster" paragraph after the Speakers steps.
  - [ ] `crates/vault/src/lib.rs`'s on-disk layout block lists `roster.json` at project level.

**Non-functional**:

- **NFR-1**: Reading a roster is one small file read; the existing `list_project_speaker_names` sibling scan stays the only directory walk and is not duplicated.
- **NFR-2**: No change to the Python service, its API, `speakers.json`, `transcript.json` or the search index; the default `make test` stays model-free and network-free.

## Out of scope

- A per-project voice store (embeddings, `<PROJECT>/speakers.json`) — explicitly still rejected; recognition remains the sibling scan in `speaker_matching.py`.
- Making the service or the MCP server roster-aware (e.g. filtering pre-names by roster). Pre-names may be off-roster; the operator reassigns them from the picker.
- Retro-validating or rewriting existing `speakers.json` labels when a project switches to roster mode.
- A "soft" roster with an "Other…" free-text escape (open question 1 — see Assumptions; implemented only if the operator picks it).
- A roster-management surface anywhere other than the recording page (Settings, the Chat tab, a project page — there are no project pages).
- Renaming a roster entry and propagating that rename across meetings.
- Changes to `SpeakerTag` / `SelectionSpeakerMenu` positioning or to the assign-vs-rename semantics (owned by sibling features F4 and F5).

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task, all three profiles; **mandatory**.
- `testing-toolkit:python-testing-patterns` — pytest signal present (`services/transcription`), no Python task in this feature.
- `frontend-toolkit:internal-ui` — internal-tool React UI (`desktop` → `web` UI rows); **mandatory** per the `web` profile. Not installed on this machine: implementers follow the repo's own component conventions (CSS modules, `btn btn-ghost` / `pill` classes, action-named `aria-label`s, inline `role="alert"` errors) as the fallback.
- `frontend-toolkit:ui-ux-pro-max` — same rows; not installed, same fallback.
- `devops-toolkit:devops-rollout-plan` — packaging signal present (`tauri.conf.json` bundle); no packaging task here.

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui` (unavailable — degrade as above)

## Architecture

**Storage — `<vault root>/<PROJECT>/roster.json`.** A plain JSON file at project level, beside the meeting folders and the reserved `chats/` directory. Chosen over `<PROJECT>/.transcriber/roster.json` (a dot *directory* at project level would surface as a bogus meeting in `vault::list_meetings` and in the Python indexer unless both grew an exclusion) and over app config keyed by project (the roster is project data; it should travel with the vault like `chats/` do). Because it is a file, every existing project-level walker — `crates/vault/src/list.rs` (`if !is_dir continue`), `services/transcription/src/transcription/search/indexer.py:241`, `mcp_server.py:175` — already ignores it. `crates/vault/src/paths.rs` gains `pub const ROSTER_FILE_NAME: &str = "roster.json";` (re-exported from `lib.rs`, layout doc updated); it is not added to `RESERVED_PROJECT_DIR_NAMES` (that list is directories).

Shape: `{"schema_version": 1, "mode": "open" | "roster", "names": ["Anna", "Maxim"]}`. Names only — no ids, no embeddings. This is a different artifact from the rejected `<PROJECT>/speakers.json` (which would have been a *voice* store); the CLAUDE.md decision is amended, not reversed.

**Rust — `apps/desktop/src-tauri/src/commands/roster.rs` (new).** `ProjectRosterView { mode: RosterMode, names: Vec<String> }` (`RosterMode` serialized as `"open"`/`"roster"`), `read_project_roster_handler(state, project)` and `save_project_roster_handler(state, project, roster: ProjectRosterInput)` following `chats.rs`: project validation, `paths::ensure_inside` containment, capped read (64 KiB) that degrades to the empty open roster, atomic temp+rename write, normalization (FR-2) before writing, returning the normalized view. Thin `#[tauri::command]` wrappers `read_project_roster` / `save_project_roster` live in the same file (the `speakers.rs` layout) and are registered in `apps/desktop/src-tauri/src/lib.rs`. Project validation is shared, not copied: `chats.rs`'s private `chats_dir` is split into `pub(super) async fn project_dir(state, project) -> Result<PathBuf, AppError>` (the validation + containment + existence part) and a two-line `chats_dir` that joins `CHATS_DIR_NAME`; `roster.rs` calls `super::chats::project_dir`. `commands.rs` declares `pub mod roster;`.

**TypeScript.**
- `apps/desktop/src/types.ts`: `RosterMode`, `ProjectRosterView`. `apps/desktop/src/api.ts`: `readProjectRoster(project)`, `saveProjectRoster(project, roster)`.
- `apps/desktop/src/lib/roster.ts` (new, pure): `EMPTY_ROSTER`, `normalizeRosterNames(names)`, `addRosterName(roster, name)`, `removeRosterName(roster, name)`, `mergeRosterNames(roster, seed)`, and `pickerSource(roster | undefined, siblingNames) -> { roster: string[] | undefined; suggestions: string[] }` — the single place that turns a mode into "select over roster" vs "datalist over the sibling scan".
- `apps/desktop/src/components/SpeakerNameField.tsx` (new) + `SpeakerNameField.module.css`: the shared name control. Props: `value`, `onChange`, `onCommit(value)`, `onCancel?`, `commitOnBlur?`, `ariaLabel`, `className`, `suggestions`, `roster?`, `autoFocus?`. `roster` undefined → today's `<input list=…>` + `<datalist>` (moved verbatim, keyboard handling included); `roster` present → `<select>` with a placeholder option, the roster names, and the current value appended when it is not in the roster; `change` commits immediately, Escape cancels. Both `SpeakerTag.tsx` and `SelectionSpeakerMenu.tsx` replace their input+datalist block with this one element and accept a new optional `roster?: string[]` prop — the smallest possible diff in the two files F4 and F5 also edit, and all roster logic stays outside them.
- `apps/desktop/src/components/TranscriptViewer.tsx`: new optional prop `roster?: ProjectRosterView`; `pickerSource(roster, suggestedSpeakers)` feeds `roster` / `suggestions` to both controls. Existing `suggestedSpeakers` prop untouched.
- `apps/desktop/src/components/ProjectRosterPanel.tsx` (new) + `.module.css`: the editor (FR-6), presentational, draft state local, `onSave` / `onClose`.
- `apps/desktop/src/state/useProjectRoster.ts` (new): `useProjectRoster(project: string | null) -> { roster, save }`; loads on project change (best-effort), `save` calls the API and stores the returned normalized view.
- `apps/desktop/src/components/RecordingPage.tsx`: props `projectRoster: ProjectRosterView` and `onSaveRoster(project, roster)`; `Panel` gains `"roster"`; the breadcrumb button; renders `ProjectRosterPanel` with `siblingNames={projectSpeakers}`; passes `roster={entry.project === null ? undefined : projectRoster}` to `TranscriptViewer`.
- `apps/desktop/src/App.tsx`: `const { roster, save: saveRoster } = useProjectRoster(openEntry?.project ?? null)` next to the existing `projectSpeakers` effect; wired into `RecordingPage`.

**Data flow.** Open recording → `App` reads roster for `entry.project` → `RecordingPage` → `TranscriptViewer` → `pickerSource` → `SpeakerTag` / `SelectionSpeakerMenu` → `SpeakerNameField` renders select or input. Editor save → `save_project_roster` → normalized view back into the hook → the same chain re-renders. `speakers.json` writes are unchanged (`set_speaker_labels`).

**Risks**:
- Merge conflicts with F4 (`SelectionSpeakerMenu.tsx`) and F5 (`SpeakerTag.tsx`): mitigated by T3–T5 — one new component absorbs the input, the two files change by one prop and one element each, and their new test files are named `*.roster.test.tsx` so no sibling edits the same test file.
- A select in edit mode changes keyboard flow for a roster-mode project: mitigated by T3's cases (Escape cancels, change commits) and T11's manual pass.
- Rust normalization and TS normalization drifting: mitigated by FR-2 fixtures shared verbatim between `tests/roster.rs` and `lib/roster.test.ts`.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3 |
| 2 | T4, T5, T7, T8 |
| 3 | T6 |
| 4 | T9 |
| 5 | T10 |
| 6 | T11 |

## Tasks

### [ ] T1: Roster file contract and the two Tauri commands  [deps: —]

- **Files**: `crates/vault/src/paths.rs`, `crates/vault/src/lib.rs`, `apps/desktop/src-tauri/src/commands/roster.rs`, `apps/desktop/src-tauri/src/commands/chats.rs`, `apps/desktop/src-tauri/src/commands.rs`, `apps/desktop/src-tauri/src/lib.rs`
- **Test first**: `apps/desktop/src-tauri/tests/roster.rs` (uses `tests/common/mod.rs`'s `build_state`, `new_tempdir`, `run`; a project is a directory created under the temp root; handlers reached as `transcriber_desktop_lib::commands::roster::{read_project_roster_handler, save_project_roster_handler}`) — cases: reading a project with no file returns open mode and no names (FR-1); save then read round-trips mode `roster` and names, and `<root>/ACME/roster.json` exists with `schema_version` 1 (FR-1); a garbage `roster.json` and a 65 KiB one read as open/empty (FR-1); saving `["  Anna ", "anna", "", "Maxim", "Maxim"]` returns and stores `["Anna", "Maxim"]` (FR-2); a 201-char name and a 501-name list are refused with `invalid_argument` and no file appears (FR-2); `unsorted`, `chats`, `""`, `..`, `A/B`, `A:B` and an unknown project are refused with `invalid_argument` and nothing is created (FR-3); with `meetings_root: None` the read fails `not_configured` (FR-3); `list_chats` on the same project still works after the `chats_dir` split (FR-3, regression guard).
- **Implement**: Add `ROSTER_FILE_NAME` to `crates/vault/src/paths.rs`, export it from `lib.rs` and add `roster.json` to the layout doc block. Split `chats.rs`'s `chats_dir` into `pub(super) project_dir` + `chats_dir`. Write `roster.rs` with the view/input structs, the two handlers (capped read degrading to open/empty; normalization; atomic write via `.roster.json.tmp` + rename) and the two `#[tauri::command]` wrappers; declare `pub mod roster;` in `commands.rs` and register both commands in `lib.rs`'s `generate_handler!`.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: `cargo test -p transcriber-desktop --test roster` and `cargo test -p vault` pass; `make lint` and `make type` pass (clippy `-D warnings`).

### [ ] T2: TS roster types, API wrappers and pure roster helpers  [deps: —]

- **Files**: `apps/desktop/src/types.ts`, `apps/desktop/src/api.ts`, `apps/desktop/src/lib/roster.ts`
- **Test first**: `apps/desktop/src/lib/roster.test.ts` — cases: `normalizeRosterNames(["  Anna ", "anna", "", "Maxim", "Maxim"])` is `["Anna", "Maxim"]` — the same fixture as T1 (FR-2); `addRosterName` appends a trimmed new name, ignores a blank one and a case-insensitive duplicate, and never mutates its input (FR-2, FR-6); `removeRosterName` drops exactly that name (FR-6); `mergeRosterNames(roster, ["maxim", "Olga"])` appends only `"Olga"` (FR-6); `pickerSource(undefined, sib)` and `pickerSource({mode:"open"}, sib)` give `{ roster: undefined, suggestions: sib }` (FR-5); `pickerSource({mode:"roster", names}, sib)` gives `{ roster: names, suggestions: [] }` (FR-4); `EMPTY_ROSTER` is open with no names (FR-1).
- **Implement**: Add `RosterMode = "open" | "roster"` and `ProjectRosterView { mode; names }` to `types.ts`; `readProjectRoster` / `saveProjectRoster` `call(...)` wrappers in `api.ts` next to the chat wrappers; the pure helpers in `lib/roster.ts` with the normalization rules mirrored from T1.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: `npm --prefix apps/desktop run test -- src/lib/roster.test.ts` passes; `make type` and `make lint` pass.

### [ ] T3: `SpeakerNameField` — the shared free-text-or-select name control  [deps: —]

- **Files**: `apps/desktop/src/components/SpeakerNameField.tsx`, `apps/desktop/src/components/SpeakerNameField.module.css`
- **Test first**: `apps/desktop/src/components/SpeakerNameField.test.tsx` — cases: without `roster`, renders a textbox with the given accessible name whose `list` points at a datalist holding every suggestion, and no datalist when suggestions are empty (FR-5); typing then Enter calls `onCommit` with the typed value, Escape calls `onCancel`, blur commits only when `commitOnBlur` (FR-5); with `roster: ["Anna", "Maxim"]`, renders a combobox with the same accessible name, a placeholder option and exactly those names, and no textbox (FR-4); selecting "Anna" calls `onCommit("Anna")` once (FR-4); with `value: "Olga"` not in the roster, "Olga" is the selected option and the roster names are still offered (FR-4); Escape on the select calls `onCancel` and never `onCommit` (FR-4); with `roster: []` the select has one disabled option and changing it never calls `onCommit` (FR-4); `autoFocus` focuses the rendered control in either mode.
- **Implement**: A presentational component (no invoke/listen/fetch) that renders `<input list=…>` + `<datalist>` when `roster` is undefined — the block currently in `SpeakerTag.tsx` lines 68–96, keyboard handling included — and a `<select>` otherwise; `useId` for the datalist, `className` passed through so the parents keep their own `.input` styling; the select gets its own small style in the module CSS.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/SpeakerNameField.test.tsx` passes; `make type`, `make lint`, `make format` pass.

### [ ] T4: `SpeakerTag` takes a roster  [deps: T3]

- **Files**: `apps/desktop/src/components/SpeakerTag.tsx`
- **Test first**: `apps/desktop/src/components/SpeakerTag.roster.test.tsx` — cases: with no `roster`, an unattributed turn opens a textbox "Name this speaker" with the suggestions datalist, typing "Olga" + Enter calls `onAssign("Olga")` (FR-5, guards today's flow); with `roster: ["Anna", "Maxim"]`, an unattributed turn opens a combobox "Name this speaker", choosing "Anna" calls `onAssign("Anna")` and the tag returns to its button state (FR-4); with `speaker: "Maxim"` and the same roster, the combobox is "Rename Maxim" preselected to "Maxim", choosing "Anna" calls `onRename("Maxim", "Anna")` and never `onAssign` (FR-4); with `speaker: "Olga"` (off-roster) the combobox shows "Olga" selected (FR-4); Escape closes the editor with neither callback called (FR-4); the `known` "Attribute this turn to …" buttons still call `onAssign(name)` in roster mode (FR-4); roster mode hint reads that names come from the project roster.
- **Implement**: Add `roster?: string[]` to `SpeakerTagProps`; replace the input+datalist block with `<SpeakerNameField … commitOnBlur onCommit={commit} onCancel={cancel} roster={roster} />`; `commit(value)` uses the value handed in instead of reading `draft`. Nothing else moves — the assign-vs-rename branches and the `known` buttons stay exactly where F5 expects them.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/SpeakerTag.roster.test.tsx src/components/TranscriptViewer.test.tsx` passes (the existing viewer suite is the open-mode regression guard); `make type`, `make lint` pass.

### [ ] T5: `SelectionSpeakerMenu` takes a roster  [deps: T3]

- **Files**: `apps/desktop/src/components/SelectionSpeakerMenu.tsx`
- **Test first**: `apps/desktop/src/components/SelectionSpeakerMenu.roster.test.tsx` — cases: with `roster: ["Anna", "Maxim"]` the menu renders the `known` buttons plus a combobox "Attribute selection to a speaker" listing the roster, and no textbox (FR-4); choosing "Maxim" calls `onAssign("Maxim")` (FR-4); with `roster: []` the combobox has one disabled option and nothing can fire (FR-4); with no `roster` the textbox "Attribute selection to a new speaker" and its datalist render as before (FR-5).
- **Implement**: Add `roster?: string[]` to the props; replace the input+datalist with `<SpeakerNameField … onCommit={onAssign} roster={roster} />` (no blur commit, no Escape — the document listener already owns Escape). Positioning and dismissal code untouched (F4's territory).
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/SelectionSpeakerMenu.roster.test.tsx src/components/SelectionSpeakerMenu.test.tsx` passes; `make type`, `make lint` pass.

### [ ] T6: `TranscriptViewer` threads the roster to both controls  [deps: T2, T4, T5]

- **Files**: `apps/desktop/src/components/TranscriptViewer.tsx`
- **Test first**: `apps/desktop/src/components/TranscriptViewer.roster.test.tsx` — cases: with `roster: { mode: "roster", names: ["Anna", "Maxim"] }`, clicking an unattributed turn's "Add speaker" opens a combobox and choosing "Anna" calls `onSaveSpeakers` with that turn's segment ids mapped to "Anna" (FR-4); with the same roster, selecting text and choosing "Maxim" from the selection menu's combobox saves exactly the selected ids (FR-4, selection built as in `TranscriptViewer.test.tsx`); with `roster: { mode: "open", names: [...] }` and `suggestedSpeakers: ["Olga"]`, the textbox with an "Olga" datalist renders and no combobox exists (FR-5); with `roster` undefined the behaviour is identical to open mode (FR-5).
- **Implement**: Add `roster?: ProjectRosterView` to the props; `const picker = useMemo(() => pickerSource(roster, suggestedSpeakers), …)`; pass `roster={picker.roster}` and `suggestions={picker.suggestions}` at both render sites (lines ~252 and ~284).
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/TranscriptViewer.roster.test.tsx src/components/TranscriptViewer.test.tsx` passes; `make type`, `make lint` pass.

### [ ] T7: `ProjectRosterPanel` — the roster editor  [deps: T2]

- **Files**: `apps/desktop/src/components/ProjectRosterPanel.tsx`, `apps/desktop/src/components/ProjectRosterPanel.module.css`
- **Test first**: `apps/desktop/src/components/ProjectRosterPanel.test.tsx` — cases: renders the radio group "Who can be named in this project" with the persisted mode checked, one "Remove <name>" button per name, the "Add a name" textbox, the seed button and Save/Cancel (FR-6); typing "  Olga " + Enter adds "Olga" to the list once, typing "olga" again adds nothing (FR-6, FR-2); "Remove Anna" drops Anna (FR-6); "Add names already used in this project" with `siblingNames: ["Maxim", "Olga"]` and roster `["Maxim"]` appends only "Olga", and the button is disabled when every sibling name is already listed (FR-6); switching the radio to "Only the roster below" and pressing Save calls `onSave` with `{ mode: "roster", names }` and then `onClose` (FR-6); when `onSave` rejects with `{ message: "boom" }`, an alert shows "boom" and `onClose` is not called (FR-6); Cancel calls `onClose` without `onSave` (FR-6).
- **Implement**: Presentational form in the `MeetingEditor` style (`onSave: (roster) => Promise<void>`, `onCancel`), draft state local, helpers from `lib/roster.ts`, saving state disables the buttons while pending; a short lead sentence explains what roster mode does.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/ProjectRosterPanel.test.tsx` passes; `make type`, `make lint`, `make format` pass.

### [ ] T8: `useProjectRoster` hook  [deps: T2]

- **Files**: `apps/desktop/src/state/useProjectRoster.ts`
- **Test first**: `apps/desktop/src/state/useProjectRoster.test.ts` (`vi.mock("../api")` exactly as `useChat.test.ts` does; `renderHook`) — cases: with project `"ACME"` the hook calls `readProjectRoster("ACME")` once and exposes the returned roster (FR-7); with `null` it never calls the API and exposes `EMPTY_ROSTER` (FR-7); when the read rejects, the roster is `EMPTY_ROSTER` and nothing throws (FR-7); changing the project re-reads and a stale late response for the previous project is ignored (FR-7); `save(roster)` calls `saveProjectRoster("ACME", roster)` and stores the normalized view it returns; a rejected save propagates to the caller and leaves the roster unchanged (FR-6, FR-7).
- **Implement**: `useState` + a cancellable `useEffect` on `project` (the same shape as `App.tsx`'s `projectSpeakers` effect), `save` via `useCallback`.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: `npm --prefix apps/desktop run test -- src/state/useProjectRoster.test.ts` passes; `make type`, `make lint` pass.

### [ ] T9: `RecordingPage` — trigger, panel and roster pass-through  [deps: T6, T7]

- **Files**: `apps/desktop/src/components/RecordingPage.tsx`, `apps/desktop/src/components/RecordingPage.module.css`
- **Test first**: `apps/desktop/src/components/RecordingPage.roster.test.tsx` (the `renderPage` harness from `RecordingPage.test.tsx`, plus `projectRoster` and `onSaveRoster` defaults) — cases: a "Project speakers" button renders beside the project pill for a project entry and not for `project: null` (FR-6); clicking it shows the roster panel, clicking again hides it (FR-6); saving from the panel calls `onSaveRoster("RDDM", roster)` and the panel closes (FR-6); with `projectRoster: { mode: "roster", names: ["Anna"] }` and a transcript, "Add speaker" on a turn opens a combobox listing "Anna" (FR-4 end to end through the page); with `project: null` and the same roster, the textbox renders instead (FR-5); the panel's seed button receives `projectSpeakers` as sibling names (FR-6).
- **Implement**: Add the two props, extend `Panel` with `"roster"`, the breadcrumb button (`btn btn-ghost`, `aria-pressed` on the toggle), render `ProjectRosterPanel` in the panel slot after the edit/delete panels, and pass `roster` to `TranscriptViewer` only when the entry has a project. The kebab menu is left alone.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/RecordingPage.roster.test.tsx src/components/RecordingPage.test.tsx` passes; `make type`, `make lint`, `make format` pass.

### [ ] T10: `App` wiring and documentation  [deps: T1, T8, T9]

- **Files**: `apps/desktop/src/App.tsx`, `CLAUDE.md`, `docs/setup.md`
- **Test first**: `apps/desktop/src/App.roster.test.tsx` (the `mockIPC` harness from `App.test.tsx`: `get_settings` with a meetings root, `service_status` ready, `list_vault` returning one `RDDM` entry and one `unsorted` entry, `read_transcript` returning a one-segment transcript) — cases: opening the `RDDM` recording invokes `read_project_roster` with `{ project: "RDDM" }` exactly once, and when it answers `{ mode: "roster", names: ["Anna"] }` the transcript's "Add speaker" opens a combobox listing "Anna" (FR-7, FR-4); opening the `unsorted` recording never invokes `read_project_roster` and "Add speaker" opens a textbox (FR-7, FR-5); saving from the roster panel invokes `save_project_roster` with `{ project: "RDDM", roster }` and the combobox then lists the names the command returned (FR-7); when `read_project_roster` throws, the page still opens in open mode (FR-7).
- **Implement**: `useProjectRoster(openEntry?.project ?? null)` in `App.tsx`, pass `projectRoster` and `onSaveRoster` to `RecordingPage`. Amend the CLAUDE.md speaker paragraph (one sentence: `roster.json` is a name-only roster with a mode, `commands/roster.rs`, still no `<PROJECT>/speakers.json`) and the hybrid-search paragraph's mention that `speakers.json` names feed suggestions (add "in open mode"). Add a "Project speaker roster" paragraph to `docs/setup.md` after the per-meeting "Identify speakers" note.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`
- **Done when**: `npm --prefix apps/desktop run test -- src/App.roster.test.tsx src/App.test.tsx` passes; `make format`, `make lint`, `make type`, `make test` all pass.

### [ ] T11: Verification — drive the flow in the running app  [deps: T10]

- **Files**: —
- **Test first**: not applicable (verification task); evidence is the checks below, mapped to FR-1, FR-4, FR-5, FR-6 and the `desktop` profile's Verification section.
- **Implement**: `cd apps/desktop && npm run tauri dev` against a scratch vault containing a project with two meetings (one with `speakers.json` labels). Checks: open a meeting in the project → "Project speakers" beside the pill → panel opens with "Anyone" checked and no names; "Add names already used in this project" pulls the sibling names; switch to "Only the roster below", Save → `<vault>/<PROJECT>/roster.json` exists with `mode: "roster"`; "Add speaker" on a turn now shows a select over the roster, choosing a name labels the turn and `speakers.json` updates; select text → the selection popover shows the select, assignment works; rename a labelled turn via the select → every segment of that speaker renames; switch back to "Anyone" → free text with datalist returns; open an `unsorted` meeting → no button, free text. Confirm the library listing and the Chat tab's project list are unaffected by the new file. Windows is the platform in scope; paths are produced by the Rust side only.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: every check above observed in the running app; `make format`, `make lint`, `make type`, `make test` green.

## QA expectations

No aggregate `make qa`; the gate runs `make format`, `make lint`, `make type`, `make test`. `make lint` includes `cargo clippy --workspace --all-targets -- -D warnings` (integration tests under `tests/` are linted too), `sync_version --check`, `verify_locks --check` and `gen_diarization_runtime --check` — none of which this feature touches. `make test` runs `cargo test --workspace`, vitest, the Python suite (model-free, unaffected) and `scripts/tests`. `make` exists only after `scripts/bootstrap.ps1`; the direct equivalents are listed in the Makefile. PowerShell 5.1 has no `&&`. Nothing known-flaky in the touched areas; `App.test.tsx`-style tests must `await settle()` before teardown as that file documents.

## Assumptions & decisions
- 2026-09-09 — (OPERATOR) Roster strictness → strict: select over the roster only, new names via the editor. Soft variants rejected.- 2026-09-09 — (OPERATOR) Roster seeding → manual only: an "Add names already used in this project" button in the editor; no auto-seed on mode switch.

- 2026-09-09 — (AUTO: codebase — `crates/vault/src/list.rs`, `search/indexer.py`, `mcp_server.py`) Where does the roster live? → `<vault root>/<PROJECT>/roster.json`, a project-level *file*: every existing walker already skips non-directories, so no listing exclusion, no Python change. Not `<PROJECT>/.transcriber/` (a dot directory would list as a bogus meeting) and not app config (project data travels with the vault, like `chats/`).
- 2026-09-09 — (AUTO: CLAUDE.md speaker paragraph) Does this reopen the "no `<PROJECT>/speakers.json`" decision? → No. The roster is names + mode with no embeddings and no segment ids; voice memory stays the sibling scan. CLAUDE.md is amended to say so (T10).
- 2026-09-09 — (AUTO: `commands/chats.rs`) Project validation → reuse `chats.rs`'s rules by extracting `project_dir`; roster commands never create a project.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test shape → Rust handlers are controllers: one integration test file over the real filesystem via the existing `tests/common` harness, no mocks. TS normalization is domain logic: unit-tested in `lib/roster.test.ts` with the same fixtures as the Rust file. UI components tested through their accessible roles, never through internals. The hook mocks only the IPC boundary (`vi.mock("../api")`), as `useChat.test.ts` does.
- 2026-09-09 — (AUTO: sibling features F4/F5) How to keep merges clean → one new `SpeakerNameField` absorbs the input/select; `SpeakerTag.tsx` and `SelectionSpeakerMenu.tsx` change by one prop and one element; new tests are `*.roster.test.tsx`, never the files F4/F5 will edit.
- 2026-09-09 — (ASSUMPTION) Strictness in roster mode → **strict**: the control is a select over the roster only; adding a name means opening the roster editor (one click away). Open question 1 offers the soft variants.
- 2026-09-09 — (ASSUMPTION) Seeding → **manual**: an "Add names already used in this project" button in the editor; no automatic seeding on switching modes. Open question 2 offers auto-seed.
- 2026-09-09 — (ASSUMPTION) Where the editor lives → the recording page, a "Project speakers" toggle beside the project pill, reusing the page's inline-panel mechanism; there are no project pages and Settings is global.
- 2026-09-09 — (ASSUMPTION) Off-roster names already in a transcript (pre-roster labels, service pre-names) are left as they are, shown as the selected option, and never rewritten; roster mode governs what the picker offers, not what the service writes.
- 2026-09-09 — (ASSUMPTION) `frontend-toolkit:internal-ui` is not installed → implementers follow the repository's existing component conventions as the UI standard.
