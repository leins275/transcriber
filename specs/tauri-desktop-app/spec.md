---
slug: tauri-desktop-app
created: 2026-08-21
status: approved
---

# Spec: Tauri 2 desktop app with drag-and-drop processing

## Summary

A Windows desktop application built with Tauri 2 (Rust core) and a React frontend. Its single MVP capability: the user drags a meeting recording file onto the app window, the app files that recording into the meeting vault, kicks off transcription through the Python transcription service, shows live job status, and reports where the resulting `transcript.json` landed. The app also owns the user-visible setting for the meetings-root folder. This is the operator's day-to-day entry point to the whole system — everything else in the batch is machinery behind it.

## Problem & context

The operator works across several projects, records meetings, and today dumps the recordings into an unmanaged `Meetings` folder on Windows (`D:\Local\Git\transcriber\IDEA.md`, `# Проблема`). Nothing happens to them afterwards: getting a transcript means manually finding a file, manually invoking a tool, and manually deciding where the output goes. The friction is high enough that it does not happen.

This feature removes that friction down to one gesture — drop the file, walk away. It is deliberately the thinnest possible shell over the other three features:

- **F1** (`specs/meeting-vault-layout/`) owns the vault layout and the `<Project code> - <date> - <Title>.<ext>` naming convention, including the `unsorted/` fallback for badly named files. This app calls that logic; it does not reimplement it.
- **F2** (`specs/transcription-service/`) owns transcription. This app talks to it across a service seam and knows nothing about whisper, litellm, or sqlite logging.
- **F4** (`specs/windows-installer-build/`) owns packaging and sets the meetings-root at install time. This app reads and can change that setting.

Repository state at spec time: greenfield. `D:\Local\Git\transcriber\` contains only `IDEA.md`, `.gitignore`, `specs/`, and a gitignored read-only clone at `D:\Local\Git\transcriber\vexa\` which is research material for F2 and is **out of scope here**. There is no `Cargo.toml`, no `package.json`, no `Makefile`, and no Rust toolchain installed on the host — this feature creates the first Rust and first frontend code in the repo.

Host prerequisites verified on the operator's machine:

| Prerequisite | State |
|---|---|
| MSVC toolchain | present — `C:\Program Files\Microsoft Visual Studio\2022` |
| Windows SDK | present — `10.0.26100.0` |
| WebView2 runtime | present — `C:\Program Files (x86)\Microsoft\EdgeWebView\Application\151.0.4129.93` |
| Node / npm | present — Node v22.17.1, npm 11.5.1 |
| **rustup / cargo** | **absent** — must be installed before any work starts |

## Users

- **Operator (sole user)** — a single person on their own Windows machine, running a local-first personal tool. No accounts, no multi-tenancy, no network exposure beyond localhost. This is an internal-tool UI, not a customer-facing product surface: the UI should be dense, legible and functional rather than marketed.

## Profiles

The repository is greenfield, so **no profile matches by detection today** — every detection probe returns nothing because none of the files exist yet. The two profiles below match *by construction*: this feature's own deliverables are precisely the artifacts their detection rules look for, and downstream agents must treat them as active from the first task.

- `desktop` — matches once this feature creates `src-tauri/tauri.conf.json` and a `[dependencies] tauri` block in `src-tauri/Cargo.toml`. Probe today: no `Cargo.toml` anywhere in the repo outside the gitignored `vexa/`. The `tauri.conf.json` bundle block also satisfies the profile's "packaging config → distribution layer" rule, though F4 owns what goes in it.
- `web` — matches once this feature creates a root `package.json` with `react` and `vite` dependencies. Probe today: the only `package.json` in the tree is `D:\Local\Git\transcriber\vexa\package.json`, which belongs to the gitignored reference clone and is not this project's. Per the `desktop` profile, a Tauri app renders its UI with web technology and therefore takes the UI toolkits from `web` and the process/IPC rules from `desktop`.

`cli` does **not** match: its negative signal ("no `react` / `tauri` dependency anywhere") is exactly what this feature violates.

Internal-tool vs public-facing classification: **internal tool**. The `web` profile flags this as a legitimate open question, and it is recorded as one below, but the analyst's reading is unambiguous enough to draft against — a single-user, local-first, personal utility with no customer surface. This selects `frontend-toolkit:internal-ui` as a mandatory skill.

## Detected stack

Every row is a target state created by this feature; the Evidence column records what proves it today.

| Layer | Technology | Evidence |
|---|---|---|
| Privileged process | Rust + Tauri 2 | to be created — `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`; nothing exists today |
| Frontend | React + TypeScript + Vite | to be created — root `package.json`; only `vexa/package.json` (gitignored) exists today |
| Host toolchain (Rust) | **absent** | no `~/.cargo/`, no `C:\Program Files\Rust*`, `cargo`/`rustc`/`rustup` not on PATH |
| Host toolchain (Node) | Node v22.17.1 / npm 11.5.1 | `node --version`, `npm --version` on PATH; `bun` 1.3.14 also present, `pnpm` absent |
| Native build deps | MSVC 2022 + Windows SDK 10.0.26100 + WebView2 151 | directory probes listed in Problem & context |
| Config store | JSON settings file in the OS app-config dir | to be created; contract shared with F4 |
| Service seam | HTTP over localhost to the F2 service | to be created; F2's spec fixes the wire format |
| Testing | `cargo test` (Rust), Vitest + React Testing Library (UI) | to be created — no test deps or config in the repo |

Makefile QA targets present: **none**. There is no `Makefile` in the repository, and `make` is not installed on the host (`make -n format`, `make -n lint`, `make -n type`, `make -n test` all fail with `make: command not found`). FR-19 covers establishing them.

## Functional requirements

### App shell and toolchain

- **FR-1** (must): The repository contains a Tauri 2 application that builds and launches on Windows 11 x64 — a Rust privileged process under `src-tauri/` and a React + TypeScript + Vite frontend. `npm run tauri dev` opens a window; `npm run tauri build` produces a runnable binary. The frontend package manager is **npm** (present on the host; `pnpm` is not installed).
- **FR-2** (must): Setup documentation states the host prerequisites and how to satisfy the missing one — `rustup` with the `x86_64-pc-windows-msvc` toolchain. A contributor following it from a clean checkout reaches a running app.
- **FR-3** (must): Tauri capabilities are allowlisted, not blanket-enabled. `src-tauri/capabilities/` grants only the permissions the MVP actually uses. Any capability added is justified in a comment or in the plan.

### Drag and drop

- **FR-4** (must): Dropping one or more files onto the app window ingests them. The app uses Tauri 2's **window drag-drop event** (`onDragDropEvent` / `tauri://drag-drop`), which delivers absolute filesystem paths to the frontend. HTML5 drag-and-drop inside the webview does not yield real paths on Windows and must not be relied on; `dragDropEnabled` stays true in the window config.
- **FR-5** (must): The window shows an unmistakable drop target with three visual states — idle, drag-hovering, and dropped/working — so the user knows the gesture registered before any file IO completes.
- **FR-6** (must): Only recording files are accepted. The app validates the extension against an allowlist (`.mp4`, `.mkv`, `.mov`, `.webm`, `.m4a`, `.mp3`, `.wav`, `.flac`, `.ogg`, `.aac`) and rejects anything else — including directories — with a named, readable error that identifies the offending file. A rejected file in a multi-file drop does not abort the accepted ones.
- **FR-7** (should): A "Choose file…" button opens the native file dialog as an equivalent path to the same flow. This is the accessible fallback and the automation-friendly entry point for tests.
- **FR-8** (should): Dropping multiple files enqueues them and processes them sequentially. The UI shows each as its own job row.

### Vault ingest

- **FR-9** (must): Each accepted file is filed into the meeting vault using **F1's** naming and layout logic — valid names to `<meetings-root>/<PROJECT>/<date> - <Title>/source.<ext>`, invalid names to the `unsorted/` area keyed by date added. This app owns no copy of those rules; it calls the seam F1 exposes and renders the destination F1 returns. If the resolved destination already exists, the app does not silently overwrite: it reports the collision and F1's spec decides the resolution policy.
- **FR-10** (must): Ingest never blocks the UI thread. A multi-gigabyte recording being copied or moved leaves the window responsive, with a busy indication on that job row.
- **FR-11** (must): Every filesystem path derived from a dropped file is canonicalized and asserted to resolve **inside** the configured meetings-root before any write. A path that escapes the root is refused. A drag-drop payload is untrusted input, and a file dialog result is not validation.

### Transcription

- **FR-12** (must): After ingest, the app submits the meeting for transcription through an abstract **transcription service seam** — a single Rust trait/module with these operations, so the concrete transport can change when F2's spec lands without touching the UI:
  - `health()` — is the service reachable and ready?
  - `submit(meeting)` — start a transcription job for an ingested recording; returns a job id.
  - `status(job_id)` — current state, one of `queued | running | done | failed`, with optional progress and an error message.
  - The default binding is HTTP to `http://127.0.0.1:<port>` on loopback only, with the base URL taken from settings. The seam has a fake/in-memory implementation so the whole UI flow is testable without F2 existing.
- **FR-13** (must): The app surfaces service reachability. When the transcription service is not available, the app says so explicitly and tells the user what to do, rather than failing a drop with a generic error. Files already ingested into the vault stay ingested; only the transcription step is reported as unavailable.
- **FR-14** (must): Each job's status is visible and updates without user action — at minimum `queued → running → done | failed`, with the failure message from the service shown verbatim when it fails.
- **FR-15** (must): On success the job row shows the resolved location of `transcript.json` inside the meeting folder, with a control to reveal that folder in Windows Explorer. The reveal action validates that the target is under the meetings-root before invoking any OS handler — no UI-supplied string reaches a shell command unvalidated.

### Settings

- **FR-16** (must): The meetings-root folder is a persisted, user-visible setting. The app reads it at startup, shows it, and lets the user change it via a native folder picker. F4 writes the initial value at install time; the app must tolerate the setting being present, absent, or pointing at a folder that no longer exists.
- **FR-17** (must): Settings live in a single JSON file in the OS app-config directory (on Windows, under `%APPDATA%\<app-identifier>\`). The file contains at least `meetings_root` and the transcription service base URL. This path and schema are a **contract shared with F4** and must be stated in the plan so F4 can write it.
- **FR-18** (must): With no meetings-root configured, the app does not accept drops. It shows a first-run state that asks the user to pick the folder, and becomes functional as soon as they do — no crash, no silent write to an arbitrary default.

### Quality gates

- **FR-19** (must): The repository gains QA entry points covering this feature — `format` (`cargo fmt`, Prettier), `lint` (`cargo clippy -D warnings`, ESLint), `type` (`tsc --noEmit`), `test` (`cargo test`, `vitest run`). These are established as `make` targets to match the project convention; if the batch settles on npm scripts instead, the four names still exist and still run these commands. Note that `make` is not currently installed on the host, so the plan must state how the operator runs them.
- **FR-20** (should): An end-to-end smoke test drives the real app window. On Windows, Tauri 2 E2E uses `tauri-driver` with Microsoft Edge Driver and a WebDriver client — **Playwright cannot attach to a WebView2-hosted Tauri window**, so the `web` profile's Playwright guidance does not transfer here. If E2E is deferred as too costly for the MVP, FR-21 is not optional.
- **FR-21** (must): A written manual smoke checklist exists and is executed before the feature is called done: launch app → set meetings-root → drop a correctly named recording → observe ingest, job progress, and the reported `transcript.json` path → drop a badly named recording → observe it land in `unsorted/` → drop a `.txt` → observe rejection.

## Non-functional requirements

- **NFR-1**: The UI acknowledges a drop (job row appears, state changes) within **300 ms**, before any file copy or network call completes.
- **NFR-2**: The window stays responsive during ingest of a 2 GB file — no frozen or "Not Responding" window at any point.
- **NFR-3**: Cold start to interactive window is under **3 s** on the operator's machine.
- **NFR-4**: Job status shown in the UI is never more than **2 s** stale while a job is running.
- **NFR-5**: All network traffic from the app is to loopback (`127.0.0.1`). The app makes no outbound internet requests in the MVP.
- **NFR-6**: No Tauri `#[command]` handler panics on malformed input. Every command validates its arguments and returns a typed error the UI renders; `unwrap()`/`expect()` on UI-supplied data is a review failure.
- **NFR-7**: Platform scope is **Windows 11 x64 only**. Path handling must not hard-code separators or drive letters in a way that blocks a later macOS/Linux port, but no other platform is built or tested.
- **NFR-8**: The bundle identifier, product name and app-config directory name are fixed once in this feature and reused by F4 — changing them later breaks the installed settings file.

## Acceptance criteria

- **FR-1 / FR-2**:
  - [ ] From a clean checkout, following the setup doc yields a window on `npm run tauri dev`.
  - [ ] `npm run tauri build` produces a binary that launches on Windows 11 x64.
  - [ ] The setup doc names rustup + MSVC + WebView2 and is accurate against the operator's machine.
- **FR-3**:
  - [ ] `src-tauri/capabilities/` lists only permissions used by an implemented feature; removing any one of them breaks a demonstrable flow.
- **FR-4 / FR-5**:
  - [ ] Dragging a file over the window changes the drop target's appearance; dragging away restores it.
  - [ ] Dropping `C:\...\ELS - 260812 - Security issue.mp4` produces a job whose recorded source path is that exact absolute path.
  - [ ] No code path depends on the HTML5 `drop` event for file paths.
- **FR-6**:
  - [ ] Dropping a `.txt` file yields a visible rejection naming the file and its unsupported extension; no vault write occurs.
  - [ ] Dropping a folder is rejected without traversing it.
  - [ ] Dropping `[good.mp4, bad.txt]` together ingests `good.mp4` and rejects only `bad.txt`.
- **FR-7**:
  - [ ] The "Choose file…" button reaches an identical end state to dropping the same file.
- **FR-8**:
  - [ ] Dropping three files yields three job rows that complete one after another, none lost.
- **FR-9**:
  - [ ] A correctly named recording ends up at `<root>/ELS/260812 - Security issue/source.mp4`.
  - [ ] A badly named recording ends up under `unsorted/` per F1's rule.
  - [ ] Vault path construction lives entirely behind F1's seam — no naming regex or path-joining rule is duplicated in this feature's code.
  - [ ] A drop whose destination already exists reports the collision instead of overwriting.
- **FR-10**:
  - [ ] During ingest of a ≥2 GB file the window can be moved, resized and clicked throughout.
- **FR-11**:
  - [ ] A crafted path resolving outside the meetings-root is refused by the command handler, with a test that asserts it.
  - [ ] Path validation happens in the Rust process, not only in the frontend.
- **FR-12**:
  - [ ] The service seam is one Rust module; substituting the fake implementation runs the full drop→status→done flow with no service process running.
  - [ ] The base URL is read from settings, not hard-coded.
  - [ ] Swapping the transport requires no change to any React component.
- **FR-13**:
  - [ ] With the service down, the app shows a distinct "service unavailable" state naming the configured URL.
  - [ ] A file dropped while the service is down is still filed into the vault, and its job is marked as awaiting/failed transcription rather than reported as an ingest failure.
- **FR-14**:
  - [ ] A job visibly transitions `queued → running → done` without the user clicking anything.
  - [ ] A service-reported failure renders its message verbatim in the job row.
- **FR-15**:
  - [ ] A completed job displays the full path to `transcript.json` inside the meeting folder.
  - [ ] The reveal control opens the correct folder in Explorer.
  - [ ] A reveal request for a path outside the meetings-root is refused, with a test asserting it.
- **FR-16 / FR-17 / FR-18**:
  - [ ] The settings file path and schema are documented in the plan for F4 to consume.
  - [ ] Changing the meetings-root through the UI persists across an app restart.
  - [ ] Launching with a missing settings file shows the first-run picker and does not crash.
  - [ ] Launching with a meetings-root pointing at a deleted folder shows an actionable error, not a panic.
- **FR-19 / FR-20 / FR-21**:
  - [ ] `format`, `lint`, `type`, `test` all exist and all pass on a clean tree.
  - [ ] `lint` fails the build on a clippy warning (`-D warnings`).
  - [ ] The manual smoke checklist is committed and its execution recorded.

## Out of scope

- **Summary generation.** No `summary.md` is produced or displayed. Per the batch decision, it is a reserved filename in F1's layout only.
- **The vault naming and layout rules themselves** — F1 owns them; this feature only calls them.
- **The transcription implementation** — whisper, litellm, provider swapping, sqlite cost/time logging are all F2. This app never sees a model.
- **The Windows installer, whisper model download, and install-time folder selection** — F4.
- **The `vexa` clone** at `D:\Local\Git\transcriber\vexa\` — research material for F2, irrelevant here.
- **macOS and Linux builds.** Cross-platform is a stated direction, not MVP scope.
- **Auto-update**, code signing, and telemetry.
- **A vault browser** — browsing, searching, renaming or playing back past meetings. The MVP shows jobs from the current session only unless the open question below decides otherwise.
- **Transcript viewing or editing** in-app. The app reports where `transcript.json` is; it does not render its contents.
- **Speaker diarization, screen/audio recording capture, action items, RAG wiki, topic extraction** — all explicitly deferred at intake.
- **Any authentication, accounts, or remote access.** Single local user.

## Applicable toolkits

Union of `desktop` and `web`, filtered to the layers this feature actually builds. Rows the profiles offer that do **not** apply here: `django-toolkit:*` (no Django), `testing-toolkit:python-testing-patterns` (this feature contains no Python — F2 does), `devops-toolkit:docker-patterns` and `devops-toolkit:postgres-patterns` (no containers, no database).

- `frontend-toolkit:internal-ui` — the React UI. Signal: React + Vite, staff-facing (here: single-operator, local personal tool). **Not installed** in this environment.
- `frontend-toolkit:ui-ux-pro-max` — the React UI. Same signal. **Not installed.**
- `testing-toolkit:e2e-testing-patterns` — E2E driving the Tauri webview (FR-20). Signal: webview-rendered UI. **Not installed.** Note the profile's Playwright assumption does not hold for Tauri on Windows; the pattern guidance transfers, the tool does not (use `tauri-driver` + Edge Driver).
- `devops-toolkit:devops-rollout-plan` — the `tauri.conf.json` bundle block. Signal: packaging config. Applies only marginally here since **F4 owns packaging**; listed for completeness. **Not installed.**

Only `sdd` and `workflow-toolkit` are present in `C:\Users\feitr\.claude\plugins\cache\its-marketplace\`. Every toolkit above resolves to nothing today; downstream agents should degrade gracefully and the final report should surface the gap. The `desktop` profile has no ITS toolkit at all and carries its IPC/path-traversal/shell-execution rules inline — FR-3, FR-11, FR-15 and NFR-6 encode them directly.

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every UI task, carried unchanged from the `web` profile. It is the UI source of truth and overrides generic habits; an implementer that skips it produces plausible, wrong-looking components. Currently **not installed** — if it stays unavailable, the plan must say so explicitly rather than silently substituting generic UI judgment.

## Open questions

*All resolved at the spec gate (2026-08-21): Q1 → A (sidecar, from F2's lifecycle decision), Q2 → A (Rust crate, from F1's language decision), Q3 → A (polling, from F2's API decision), Q4 → A (drop zone + session job list). Retained below for the record.*

1. **Who runs the Python transcription service?** The app has to reach F2 somehow, and this decision shapes the Rust process model, F2's packaging, and F4's installer.
   - **(A) Tauri sidecar** — the app spawns the service as a bundled sidecar on launch and kills it on exit. Simplest UX (one thing to start), lifecycle is the app's problem, but the Python runtime must be bundled and F4's installer grows.
   - **(B) Expect it running** — the app only connects; the user starts the service themselves. Least app code, fastest to MVP, cleanest seam for development, but a second thing to launch and a worse day-one experience.
   - **(C) Windows service** — F4 installs the transcription service to auto-start. Best steady-state UX, most installer work, hardest to develop against.
   - **(D) Hybrid** — try to connect; if nothing answers, spawn a sidecar. Best UX of the four, but two lifecycle paths to get right and the most code.

2. **Where does F1's vault logic physically live?** The app must file the dropped recording, and only one side should own the rules.
   - **(A) Rust crate** — F1 ships as a Rust library the Tauri process links. The app files the file, then hands the service a path. Fast, offline-capable, no upload; means F1 is written in Rust.
   - **(B) Service-side** — F1 is Python inside F2. The app hands over the raw dropped path and the service files it. Keeps F1 and F2 in one language; the app becomes a thin client but cannot ingest at all when the service is down (conflicts with FR-13 as drafted).
   - **(C) Both** — Rust for the app, Python for the service, one shared spec. Maximum flexibility, duplicated rules, guaranteed drift.

3. **How does the app learn a job's progress?** Determines F2's API surface as much as this app's.
   - **(A) Poll `status(job_id)`** every 1–2 s. Dead simple, works with any HTTP server, slightly stale, satisfies NFR-4.
   - **(B) Server-sent events / WebSocket** — real progress, smoother UI, more moving parts on both sides.
   - **(C) Completion only** — submit and check when it finishes. Least code, but a 40-minute recording shows a spinner with no feedback, which reads as a hang.

4. **How much UI is in the MVP?** Directly sets the build size.
   - **(A) Drop zone + current-session job list** — jobs disappear on restart. The lean reading of "--fast". Recommended.
   - **(B) Drop zone only, one job at a time** — absolute minimum; conflicts with FR-8 (multi-file) and makes failures easy to miss.
   - **(C) Drop zone + persistent job history** — survives restarts, needs local persistence (a small sqlite or JSON store) the MVP does not otherwise require.

## Decisions log

- 2026-08-21 — What does the user drag onto the app? → The meeting **recording** file (mp4/audio). "файл транскрипта" in `IDEA.md` was a misnomer. (operator, split gate)
- 2026-08-21 — Which platforms does the MVP target? → **Windows only**; cross-platform is a later direction. (operator, split gate)
- 2026-08-21 — Does the MVP generate summaries? → **No.** `summary.md` is a reserved filename in F1's layout only. (operator, split gate)
- 2026-08-21 — Internal-tool UI or public-facing UI? → **Internal tool** — single-user, local-first personal utility; selects `frontend-toolkit:internal-ui` as mandatory. (analyst determination; flip if the app is ever aimed at outside users)
- 2026-08-21 — Q1 service lifecycle → **A: Tauri sidecar** — the app spawns the F2 service on launch, reads its stdout ready line (`{"event":"listening","port":…,"token":…}`) for the base URL/token, kills it on exit. FR-12's "expects it running" states are still required for the sidecar-crashed case. (Operator, F2 spec gate; propagated.)
- 2026-08-21 — Q2 vault logic location → **A: Rust crate** — the app links F1's vault library directly. (Operator, F1 spec gate; propagated.)
- 2026-08-21 — Q3 progress mechanism → **A: poll `status(job_id)` every 1–2 s** against F2's async job API. (Operator, F2 spec gate; propagated.)
- 2026-08-21 — Q4 UI surface → **A: drop zone + current-session job list**; no persistent job history in MVP. (Operator, spec gate.)
