---
slug: tauri-desktop-app
status: approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Tauri 2 desktop app with drag-and-drop processing

## Architecture overview

This feature creates the repo's first Rust workspace manifest, the first frontend package, and the Tauri app that ties F1 (vault crate) and F2 (transcription service) together. The worktree's base **already contains** `crates/vault/` (F1) and `services/transcription/` (F2) — implementers must read that real code rather than guessing its API.

### Repository layout after this feature

```
Cargo.toml                      # NEW — workspace root: members = ["crates/vault", "apps/desktop/src-tauri"]
crates/vault/                   # F1, already merged — linked as a path dependency, never modified here
services/transcription/         # F2, already merged — spawned as a sidecar, never modified here
apps/desktop/                   # NEW — this feature
  package.json  vite.config.ts  tsconfig.json  index.html  eslint.config.js  .prettierrc.json
  src/                          # React + TypeScript
    main.tsx  App.tsx  api.ts  types.ts  styles.css
    components/{DropZone,JobList,JobRow,SettingsBar,ServiceBanner,FirstRun}.tsx (+ *.module.css, *.test.tsx)
    state/useJobs.ts
    test/setup.ts
  src-tauri/                    # Rust privileged process
    Cargo.toml  build.rs  tauri.conf.json  capabilities/default.json  icons/
    src/main.rs                 # thin — calls lib::run()
    src/lib.rs                  # app builder, plugin + state wiring, sidecar lifecycle hooks
    src/error.rs                # AppError taxonomy, serialized to the UI
    src/config.rs               # config.json load/save/validate (FR-16..18)
    src/paths.rs                # canonicalization + containment (FR-11, FR-15)
    src/ingest.rs               # wrapper over F1's vault crate, off the UI thread (FR-9, FR-10)
    src/service/mod.rs          # TranscriptionService trait + job model (FR-12)
    src/service/http.rs         # HTTP binding to F2
    src/service/fake.rs         # in-memory fake for tests
    src/sidecar.rs              # spawn F2, parse ready line, kill on exit
    src/jobs.rs                 # job registry, sequential pipeline, 1.5 s poll loop (FR-8, FR-14, NFR-4)
    src/commands.rs             # #[tauri::command] handlers — the only IPC surface
    tests/                      # integration tests over the fake service
docs/                           # NEW — setup guide + manual smoke checklist
```

`installer/`, `scripts/` and the root `Makefile` are **F4's** and are not created here.

### Data flow (one dropped file)

```
webview  onDragDropEvent(drop, paths[])            [Tauri window event — real FS paths]
   |                                               HTML5 drop is never used for paths
   v
invoke("enqueue_paths", {paths})  --------------->  commands.rs
   |   returns JobSnapshot[] immediately (<300 ms, NFR-1)
   |                                                jobs.rs: create job rows (state=pending)
   |                                                         sequential worker, one job at a time
   |                                                paths.rs: canonicalize, reject dirs / bad ext
   |                                                ingest.rs (spawn_blocking): vault crate does
   |                                                         parse -> classify -> copy -> verify
   |                                                         -> returns meeting_dir + source path
   |                                                service::submit(meeting_dir, source) -> job_id
   |                                                poll service::status(job_id) every 1.5 s
   v
listen("jobs://updated")  <-----------------------  emit JobSnapshot on every transition
listen("service://status") <----------------------  sidecar/health state changes
```

On success the job row shows `<meeting_dir>\transcript.json` (written by F2 into the `output_dir` we pass) and a **Reveal** control that calls `reveal_job(job_id)` — the Rust side looks the path up **by job id**, re-validates containment under the meetings-root, and only then runs `explorer.exe /select,<path>`. No UI-supplied string ever reaches a process spawn.

### Service seam (FR-12)

`src/service/mod.rs` defines exactly one abstraction:

```rust
#[async_trait]
pub trait TranscriptionService: Send + Sync {
    async fn health(&self) -> Result<ServiceHealth, ServiceError>;
    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError>;   // -> job_id
    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError>;
}
pub enum JobState { Queued, Running, Done, Failed }   // FR-12's four states
```

- `SubmitRequest { audio_path, output_dir, language: Option<String> }` maps to F2's `POST /v1/jobs` body `{ audio_path, output_dir, language? }`.
- `status()` maps F2's `GET /v1/jobs/{id}` → `{status, progress, error_kind?, error_message?}`.
  **State mapping (authoritative here):** F2 `queued→Queued`, `running→Running`, `succeeded→Done`, `failed→Failed`, `cancelled→Failed` with message `"cancelled"`. F2 has five states, FR-12 has four; this collapse is the seam's job and must be unit-tested.
- Auth: F2 issues a bearer token on its ready line; `http.rs` sends `Authorization: Bearer <token>` when a token is known.
- `reqwest` is declared `default-features = false, features = ["json"]` — no TLS compiled in, so NFR-5 (loopback only, no internet) is enforced structurally as well as by a host check that rejects any base URL whose host is not `127.0.0.1`/`localhost`.
- `fake.rs` walks `Queued → Running(progress) → Done|Failed` on a timer with no process and no socket, so the whole UI flow is testable without F2 running.

### Sidecar lifecycle (Q1 → A)

On startup `lib.rs` asks `sidecar.rs` to spawn F2 **in the background** (never blocking window creation — NFR-3). Dev command, kept configurable in one struct:

```
C:\Users\<user>\.local\bin\uv.exe run --directory services/transcription transcription-service serve --port 0
```

with environment derived from `config.json` (`TRANSCRIBER_CONFIG`, `TRANSCRIBER_APP_DIR`, `TRANSCRIBER_ALLOWED_ROOTS=<meetings_root>`, `TRANSCRIBER_MODEL_PATH`, `TRANSCRIBER_MODEL_ID`). The exact flag/env spellings **must be read out of `services/transcription/` at implementation time** — F2's config module is the source of truth, and F4 later swaps the program for the baked-env launcher by changing only this struct.

The supervisor reads one JSON line from the child's stdout — `{"event":"listening","port":N,"token":"…","pid":P}` (F2 FR-14) — with a bounded timeout, derives `http://127.0.0.1:<port>`, and emits `service://status`. Child stderr is drained to the app log. On app exit the child is killed. If `config.json` has a non-null `service.base_url`, that URL wins and no sidecar is spawned (the "expect it running" development mode FR-12/FR-13 still require). If the sidecar dies or never becomes ready, the app enters the **service-unavailable** state naming the configured/derived URL — ingest keeps working and affected jobs are marked awaiting transcription, never as ingest failures (FR-13).

Because F2 validates `audio_path`/`output_dir` against its own allowed-roots list, changing the meetings-root at runtime **restarts the sidecar** with the new root. That restart is part of `set_meetings_root`.

### Settings contract (FR-17, shared with F4 — authoritative)

**Path:** `%APPDATA%\com.transcriber.desktop\config.json` — i.e. Tauri's `app_config_dir()`, which is `%APPDATA%\<bundle identifier>`.

**Fixed identity (NFR-8, changing any of these breaks installed settings):**
- bundle identifier: `com.transcriber.desktop`
- productName / window title: `Transcriber`
- app-config directory name: `com.transcriber.desktop`

**Schema v1:**

```json
{
  "schema_version": 1,
  "meetings_root": "D:\\Meetings",
  "service": { "base_url": null },
  "model": { "id": "large-v3", "path": "C:\\Users\\<user>\\AppData\\Local\\Programs\\Transcriber\\models" }
}
```

- `meetings_root` — absolute path; `null`/absent means first-run (FR-18).
- `service.base_url` — `null` (default) means "app-managed sidecar"; a string like `http://127.0.0.1:8756` means "connect to this, do not spawn".
- `model.id` / `model.path` — passed through to the sidecar; the app never loads a model.
- **Unknown top-level and nested keys are preserved verbatim on save** (`#[serde(flatten)] extra`), so keys F4's installer writes are never destroyed by an app-side settings change. Missing keys fall back to defaults; a malformed file surfaces an actionable error, never a panic.
- The app writes atomically (temp file in the same directory + rename) and creates the directory if absent.

*(F4's spec FR-11 describes this file as living in the application folder, discovered via an env var the app sets when spawning the service. The batch decision puts the file in the app-config dir; the env-var handshake is honoured — the app exports `TRANSCRIBER_CONFIG` and `TRANSCRIBER_APP_DIR` to the sidecar. F4 must write to the app-config path above.)*

### IPC contract (frozen here so frontend and Rust tasks can proceed in parallel)

Commands — all async, all returning `Result<T, AppError>`; every one validates its arguments (NFR-6: no `unwrap`/`expect` on UI-supplied data):

| Command | Args | Returns |
|---|---|---|
| `get_settings` | — | `SettingsView` |
| `set_meetings_root` | `{ path: string }` | `SettingsView` |
| `enqueue_paths` | `{ paths: string[] }` | `JobSnapshot[]` (immediately, before any IO) |
| `list_jobs` | — | `JobSnapshot[]` |
| `service_status` | — | `ServiceStatusView` |
| `reveal_job` | `{ jobId: string }` | `()` |

```ts
type SettingsView = {
  meetings_root: string | null;
  meetings_root_exists: boolean;      // false => actionable error state, not a panic
  service_base_url: string | null;
  supported_extensions: string[];     // single source of truth, from the Rust side
};
type JobState = "pending" | "ingesting" | "queued" | "running" | "done" | "failed" | "rejected";
type JobSnapshot = {
  id: string; source_path: string; file_name: string; state: JobState;
  classification: "sorted" | "unsorted" | null;
  meeting_dir: string | null; source_dest: string | null; transcript_path: string | null;
  progress: number | null; message: string | null; error_kind: string | null; created_at: string;
};
type ServiceStatusView = { state: "starting" | "ready" | "unavailable"; base_url: string | null; detail: string | null };
type AppError = { kind: ErrorKind; message: string };
type ErrorKind = "not_configured" | "invalid_argument" | "outside_root" | "unsupported_extension"
               | "not_a_file" | "vault" | "collision" | "service_unavailable" | "service"
               | "config" | "io" | "internal";
```

Events: `jobs://updated` (payload: one `JobSnapshot`, upsert by id) and `service://status` (payload: `ServiceStatusView`).

### UI shape (Q4 → A)

One window, three regions: a settings bar (meetings-root path + Change…), the drop zone with idle / hovering / working states plus a "Choose file…" button, and the current-session job list. No persistence of job history. First-run replaces the drop zone with a folder picker prompt and refuses drops.

**Mandatory-skill gap:** `frontend-toolkit:internal-ui` is declared mandatory by the spec but is **not installed** in this environment (only `sdd` and `workflow-toolkit` exist in the marketplace cache). It cannot be consulted. Every UI task lists it anyway and must degrade explicitly to these written conventions instead of generic product-UI habits: dense and legible over decorative; no marketing chrome, hero sections, gradients or illustrations; system font stack; paths rendered in a monospace face and always selectable; service/error text quoted verbatim from the backend; every control reachable by keyboard; state changes announced through the job list rather than transient toasts. Implementers must note the unavailable skill in their report.

### Capabilities (FR-3)

`src-tauri/capabilities/default.json`, `windows: ["main"]`, with a `description` field carrying the justification (JSON has no comments). Blanket `core:default` is **not acceptable**. Candidate minimal set, to be pruned in T11 until removing any one breaks a demonstrable flow:

- `core:event:default` — the frontend listens to `jobs://updated` / `service://status`.
- `core:window:default`, `core:webview:default` — window drag-drop events (FR-4) and window control.
- `core:app:default` — version/about surface.
- `dialog:allow-open` — the folder picker (FR-16) and "Choose file…" (FR-7).

No `fs`, `shell`, `opener`, `http` or `process` plugin permission is granted: all filesystem work, the Explorer reveal and all HTTP happen in the privileged process behind validated commands.

## Risks

- **F1/F2 API drift against their specs.** The specs are the design, the merged code is the truth. T7/T8/T9 explicitly require reading `crates/vault/src/lib.rs` and `services/transcription/` before writing code, and forbid re-deriving vault rules or F2 request shapes from the spec text alone.
- **Extension allowlist divergence.** This spec's FR-6 lists `.aac` (F1 does not accept it) and omits `.avi` (F1 does). Mitigation: the Rust side has exactly one allowlist, taken from the vault crate's public constant if it exposes one, and the UI receives it via `supported_extensions` instead of hard-coding a list. Flagged at the plan gate — see the summary.
- **Sidecar cold start in dev.** `uv run --directory services/transcription …` may resolve an environment on first launch, far exceeding NFR-3 if awaited. Mitigation: spawn is fire-and-forget, the window paints immediately, and the UI shows an explicit "service starting" state that degrades into FR-13's unavailable state on timeout.
- **Runtime meetings-root change vs F2's allowed-roots.** Handled by restarting the sidecar inside `set_meetings_root`; tested in T8/T11.
- **Windows canonicalization.** `std::fs::canonicalize` yields `\\?\C:\…` verbatim paths. Containment checks must canonicalize *both* sides and comparison/display must strip the verbatim prefix. `paths.rs` owns one helper for this; every other module uses it.
- **2 GB ingest freezing the UI (NFR-2).** All vault work runs under `spawn_blocking`; no command handler performs synchronous IO on the async runtime. T13 asserts responsiveness manually against a large file.
- **E2E tooling.** `tauri-driver` + `msedgedriver` are unproven on this host and Playwright cannot attach to WebView2. T15 is **parked**; the committed manual smoke checklist (T14) is the required evidence path per FR-21.
- **All four listed toolkits are uninstalled.** Every task that names one must degrade to the written conventions in this plan and surface the gap in its report.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2 |
| 2 | T3, T4, T5, T6 |
| 3 | T7, T8, T9, T12 |
| 4 | T10 |
| 5 | T11 |
| 6 | T13, T14 |
| 7 | T15 (parked) |

## Tasks

### [x] T1: Rust workspace, Tauri 2 skeleton, error taxonomy, module stubs  [deps: —]

- **Files**: `Cargo.toml`, `.gitignore`, `apps/desktop/src-tauri/Cargo.toml`, `apps/desktop/src-tauri/build.rs`, `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/capabilities/default.json`, `apps/desktop/src-tauri/icons/**`, `apps/desktop/src-tauri/.gitignore`, `apps/desktop/src-tauri/src/{main.rs,lib.rs,error.rs,config.rs,paths.rs,ingest.rs,jobs.rs,sidecar.rs,commands.rs}`, `apps/desktop/src-tauri/src/service/{mod.rs,fake.rs,http.rs}`
- **Test first**: unit tests inside `apps/desktop/src-tauri/src/error.rs` — cases: every `AppError` variant serializes to exactly `{"kind": "...", "message": "..."}` with the `ErrorKind` strings frozen in the plan's IPC contract (FR-12, NFR-6); `AppError::from(std::io::Error)` maps to `kind: "io"` without panicking.
- **Implement**: Root `Cargo.toml` workspace with members `crates/vault` and `apps/desktop/src-tauri` (resolver 2). Tauri 2 app crate named `transcriber-desktop`: deps `tauri` (features `["protocol-asset"]` only if needed — start bare), `tauri-plugin-dialog`, `serde`/`serde_json`, `thiserror`, `uuid` (v4), `tokio` (`rt-multi-thread`, `process`, `time`, `io-util`, `sync`), `reqwest { default-features = false, features = ["json"] }`, `async-trait`, and `vault = { path = "../../../crates/vault" }` (read `crates/vault/Cargo.toml` for the real package name); dev-deps `tempfile`, `wiremock`. `tauri.conf.json` fixes identifier `com.transcriber.desktop`, productName `Transcriber`, `frontendDist: "../dist"`, `devUrl: "http://localhost:1420"`, `beforeDevCommand: "npm run dev"`, `beforeBuildCommand: "npm run build"`, one `main` window ~1100x760 with `dragDropEnabled: true`. `lib.rs` declares **all** modules up front and every stub compiles empty, so wave-2 tasks never touch a shared file; `error.rs` ships the complete `AppError`/`ErrorKind` taxonomy from the IPC contract (including `Internal { message }`) — later tasks consume variants and must not edit this file. Icons: generate placeholders with `npx @tauri-apps/cli icon` from a plain square PNG, or copy the Tauri template set. Root `.gitignore` gains `target/`, `node_modules/`, `dist/`.
- **Skills**: `devops-toolkit:devops-rollout-plan` (bundle block only, marginal — **not installed**, note the gap; F4 owns packaging).
- **Done when**: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings` and `cargo test --workspace` all pass from the repo root, F1's crate builds as a workspace member, and `cargo build -p transcriber-desktop` succeeds. No `capabilities` entry beyond the plan's candidate list.

### [x] T2: Frontend scaffold — Vite + React + TypeScript + Vitest + lint/format scripts  [deps: —]

- **Files**: `apps/desktop/package.json`, `apps/desktop/package-lock.json`, `apps/desktop/index.html`, `apps/desktop/vite.config.ts`, `apps/desktop/tsconfig.json`, `apps/desktop/tsconfig.node.json`, `apps/desktop/eslint.config.js`, `apps/desktop/.prettierrc.json`, `apps/desktop/.prettierignore`, `apps/desktop/.gitignore`, `apps/desktop/src/{main.tsx,App.tsx,styles.css,vite-env.d.ts}`, `apps/desktop/src/test/setup.ts`, `apps/desktop/src/App.test.tsx`
- **Test first**: `apps/desktop/src/App.test.tsx` — cases: the app shell renders its product name and the three region landmarks (settings bar, drop zone, job list) as accessible regions; `vitest run` exits 0 under jsdom with no Tauri runtime present (FR-1, FR-19).
- **Implement**: npm (not pnpm/bun). Deps `react`, `react-dom`, `@tauri-apps/api@^2`, `@tauri-apps/plugin-dialog@^2`; dev-deps `@tauri-apps/cli@^2`, `vite`, `@vitejs/plugin-react`, `typescript`, `vitest`, `jsdom`, `@testing-library/{react,jest-dom,user-event}`, `eslint` + `typescript-eslint` + `eslint-plugin-react-hooks`, `prettier`. Vite on port 1420, `strictPort: true`, `clearScreen: false`, and a `test` block (jsdom, `setupFiles: src/test/setup.ts`). Scripts: `dev`, `build` (`tsc --noEmit && vite build`), `preview`, `tauri`, and the FR-19 four — `format` (`prettier --write .`), `format:check`, `lint` (`eslint .`), `type` (`tsc --noEmit`), `test` (`vitest run`). `App.tsx` is a static placeholder shell only (regions + headings, no logic); T12 replaces it.
- **Skills**: `frontend-toolkit:internal-ui` (mandatory — **not installed**; degrade to the plan's written UI conventions and report the gap), `frontend-toolkit:ui-ux-pro-max` (**not installed**).
- **Done when**: from `apps/desktop/`, `npm run format:check`, `npm run lint`, `npm run type` and `npm run test` all pass on a clean tree, and `npm run build` emits `apps/desktop/dist/`.

### [x] T3: Settings module — config.json load/save/validate  [deps: T1]

- **Files**: `apps/desktop/src-tauri/src/config.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/config.rs` (`tempfile` for the config dir) — cases: absent file → first-run defaults, `meetings_root: None`, no file written (FR-18); file with only `{"schema_version":1,"meetings_root":"…"}` loads with defaults for the rest (FR-16); unknown top-level and unknown nested `service.*` keys survive a load→modify→save round-trip byte-for-byte in value (F4 contract); `meetings_root` pointing at a deleted folder loads with `meetings_root_exists = false` and no panic (FR-16); malformed JSON returns `AppError { kind: "config" }` naming the file (NFR-6); `set_meetings_root` rejects a relative path, an empty string and a path that cannot be created, and accepts an existing writable directory, persisting atomically; save→load round-trip preserves the value across a simulated restart (FR-16).
- **Implement**: `Settings` struct mirroring the plan's schema v1 with `#[serde(flatten)] extra: serde_json::Map` at each level; `load(dir)`, `save(dir)` (temp file + `fs::rename` in the same directory), `settings_view()` producing `SettingsView`. The config directory is injected as a parameter so tests never touch `%APPDATA%`; the Tauri `app_config_dir()` lookup lives in the caller (T11).
- **Skills**: — (no toolkit in the matched profiles covers Rust; the `desktop` profile's inline IPC/validation rules apply).
- **Done when**: `cargo test -p transcriber-desktop config::`, `cargo fmt --check` and `cargo clippy --workspace --all-targets -- -D warnings` pass; the file contains no `unwrap()`/`expect()` outside `#[cfg(test)]`.

### [x] T4: Path canonicalization and containment  [deps: T1]

- **Files**: `apps/desktop/src-tauri/src/paths.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/paths.rs` (`tempfile` roots) — cases: a path inside the root is accepted and returned canonical (FR-11); `<root>\..\evil.mp4`, an absolute path on another drive, a UNC path and a `\\?\` device path are all refused with `kind: "outside_root"` **before** any write (FR-11); a path whose prefix merely *string-matches* the root (`C:\Meetings-old\x.mp4` vs root `C:\Meetings`) is refused — component-wise comparison, not `starts_with` on strings; verbatim `\\?\` prefixes are stripped for display and both sides are canonicalized before comparison; a non-existent file under the root reports `not_a_file` rather than panicking; a directory input is rejected with `not_a_file` without traversal (FR-6); extension checking is case-insensitive against the single allowlist and unknown extensions yield `unsupported_extension` naming the file and its extension (FR-6); reveal-target validation refuses a path outside the root (FR-15).
- **Implement**: `canonicalize_existing`, `strip_verbatim`, `ensure_inside(root, candidate)`, `classify_drop(path, allowlist)`, `supported_extensions()`. The allowlist is derived from the vault crate's public constant when one exists (read `crates/vault/src/lib.rs`); otherwise one `const` here, documented as mirroring F1's accepted set — never duplicated anywhere else in this feature. No separator or drive letter is hard-coded (NFR-7).
- **Skills**: — (`desktop` profile inline rule: "a file dialog result is not validation").
- **Done when**: `cargo test -p transcriber-desktop paths::` passes, plus workspace `fmt`/`clippy -D warnings`; a test asserts the containment check runs before any filesystem mutation.

### [x] T5: Transcription service seam — trait, types, in-memory fake  [deps: T1]

- **Files**: `apps/desktop/src-tauri/src/service/mod.rs`, `apps/desktop/src-tauri/src/service/fake.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/service/fake.rs` — cases: `health()` on a healthy fake returns ready; `submit()` returns a job id and `status()` then walks `Queued → Running (progress non-decreasing) → Done` (FR-12, FR-14); a fake configured to fail returns `Failed` carrying the exact provider message verbatim (FR-14); a fake configured as down returns `ServiceError::Unavailable` from `health()` and `submit()` while previously submitted jobs still report status (FR-13); the mapping table (`succeeded→Done`, `cancelled→Failed("cancelled")`) is exercised through the shared `JobState::from_wire` helper.
- **Implement**: The trait, `SubmitRequest`, `JobStatus`, `JobState`, `ServiceHealth`, `ServiceError` (unavailable / http status / decode / auth), and `JobState::from_wire(&str)` implementing F2's five-to-four collapse. `fake.rs` is a `Arc<Mutex<…>>` scripted state machine with configurable step timing, usable from both unit and integration tests. `mod.rs` declares `pub mod fake; pub mod http;` so T7 never edits it.
- **Skills**: —
- **Done when**: `cargo test -p transcriber-desktop service::` passes; workspace `fmt`/`clippy -D warnings` clean; no `reqwest` or transport type appears in `mod.rs`.

### [x] T6: UI components — drop zone, job list, settings bar, service banner, first-run  [deps: T2]

- **Files**: `apps/desktop/src/types.ts`, `apps/desktop/src/components/{DropZone,JobRow,JobList,SettingsBar,ServiceBanner,FirstRun}.tsx`, `apps/desktop/src/components/*.module.css`, `apps/desktop/src/components/*.test.tsx`
- **Test first**: co-located `*.test.tsx` with React Testing Library — cases: `DropZone` renders distinct idle / hovering / working states and reverts from hovering to idle (FR-5); when disabled by first-run it renders the "choose a meetings folder" prompt and exposes no drop affordance (FR-18); `JobRow` renders each `JobState` with the file name, and for `done` shows the full `transcript_path` plus an enabled Reveal control (FR-15); for `failed` renders the backend `message` verbatim (FR-14); for `rejected` names the file and its unsupported extension (FR-6); for `ingesting` shows a busy indication (FR-10); `JobList` renders three jobs in submission order and keys them by id with none lost (FR-8); `ServiceBanner` in `unavailable` state names the configured base URL and states what to do (FR-13); `SettingsBar` shows the meetings-root path, a Change… button, and an actionable warning when `meetings_root_exists` is false (FR-16); all controls are reachable by keyboard.
- **Implement**: Purely presentational components — props in, callbacks out, **no `invoke`, no `listen`, no fetch**. `types.ts` transcribes the plan's IPC contract types exactly (`JobSnapshot`, `SettingsView`, `ServiceStatusView`, `AppError`, `JobState`, `ErrorKind`). Styling via CSS modules following the plan's UI conventions: dense rows, monospace selectable paths, no decorative chrome.
- **Skills**: `frontend-toolkit:internal-ui` (mandatory — **not installed**; degrade to the plan's UI conventions and report the gap), `frontend-toolkit:ui-ux-pro-max` (**not installed**).
- **Done when**: `npm run test`, `npm run lint`, `npm run type`, `npm run format:check` pass from `apps/desktop/`; grep confirms no `@tauri-apps/api` import under `src/components/`.

### [x] T7: HTTP binding to the F2 service  [deps: T5]

- **Files**: `apps/desktop/src-tauri/src/service/http.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/service/http.rs` using `wiremock` — cases: `submit()` POSTs to `/v1/jobs` with F2's exact body keys and returns the `job_id` from a `202` (FR-12); the `Authorization: Bearer <token>` header is sent when a token is configured and omitted otherwise; `status()` maps each of F2's five statuses to the seam's four and passes `progress`, `error_kind`, `error_message` through unchanged (FR-14); `health()` maps a `GET /health` 200 to ready and a connection refusal to `ServiceError::Unavailable` naming the base URL (FR-13); a 401 maps to an auth error, a 5xx to a distinct error, a malformed body to a decode error — none of them panic (NFR-6); constructing a client with a non-loopback base URL (`http://10.0.0.5:8000`, `https://example.com`) is rejected (NFR-5); a `status()` call completes well under the poll interval and uses a bounded request timeout (NFR-4).
- **Implement**: `HttpTranscriptionService { base_url, token, client }` implementing the T5 trait. **Read `services/transcription/` in the base tree first** and match the real route paths, request/response field names and status strings; the spec text is a design sketch, the merged code is the contract. Reuse one `reqwest::Client` with a short connect/request timeout.
- **Skills**: —
- **Done when**: `cargo test -p transcriber-desktop service::http` passes; workspace `fmt`/`clippy -D warnings` clean; no HTTPS or non-loopback host can be constructed.

### [x] T8: Sidecar supervisor — spawn F2, parse ready line, terminate on exit  [deps: T3]

- **Files**: `apps/desktop/src-tauri/src/sidecar.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/sidecar.rs` — cases: `parse_ready_line` accepts `{"event":"listening","port":51234,"token":"abc","pid":9001}` and yields base URL `http://127.0.0.1:51234` plus the token (F2 FR-14); a non-JSON line, a JSON line with a different `event`, and a line missing `port` are ignored/rejected without panicking (NFR-6); waiting on a stream that never emits a ready line resolves to a timeout error naming the command (FR-13); `SidecarSpawnConfig` renders the documented dev command (`uv run --directory services/transcription transcription-service serve --port 0`) with `TRANSCRIBER_CONFIG`, `TRANSCRIBER_APP_DIR`, `TRANSCRIBER_ALLOWED_ROOTS`, `TRANSCRIBER_MODEL_PATH`, `TRANSCRIBER_MODEL_ID` derived from a `Settings` value; a config with `service.base_url` set produces "do not spawn, use this URL" (FR-12); `restart()` terminates the previous child before spawning (used when the meetings-root changes).
- **Implement**: One `SidecarSpawnConfig` struct is the only place the program, arguments and environment are decided, so F4 can repoint it at the baked environment. Spawn with `tokio::process::Command`, stdout/stderr piped, ready-line reading behind a timeout on a background task; stderr drained to the app log. Ready-line parsing and command construction are pure functions over injected readers/values so the tests need no real F2. Kill the child on drop and on the app's exit hook. **Read `services/transcription/`'s config/CLI module for the real flag and env-var names before finalizing.**
- **Skills**: —
- **Done when**: `cargo test -p transcriber-desktop sidecar::` passes; workspace `fmt`/`clippy -D warnings` clean; no `shell = true`-equivalent, no string-concatenated command line, and no UI-supplied value reaches the argument vector.

### [x] T9: Vault ingest wrapper over F1's crate  [deps: T3, T4]

- **Files**: `apps/desktop/src-tauri/src/ingest.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/ingest.rs` against a real `tempfile` vault root — cases: a correctly named recording lands at `<root>/ELS/260812 - Security issue/source.mp4` and the returned `meeting_dir` and `source_dest` are absolute and exist (FR-9); a badly named recording lands under `<root>/unsorted/…` per F1's rule with `classification: "unsorted"` and F1's rejection reason surfaced as the job message (FR-9); a `.txt` file and a directory are refused before any vault call (FR-6); a destination collision is reported as `kind: "collision"` (or F1's duplicate/suffix outcome) and never overwrites the existing `source.*` (FR-9); a path outside the meetings-root is refused by `paths::ensure_inside` before F1 is called (FR-11); no naming regex, date parsing or path-joining rule appears in this file (FR-9 acceptance).
- **Implement**: A thin `ingest(root, source_path) -> IngestOutcome` calling F1's public API — **read `crates/vault/src/lib.rs` and use its real function and result types**; translate F1's typed rejection variants into `AppError`/job messages with a `match` that has no catch-all silently swallowing a variant. The blocking call is wrapped so callers run it under `tokio::task::spawn_blocking` (FR-10, NFR-2); this module exposes an async fn that does that wrapping itself.
- **Skills**: —
- **Done when**: `cargo test -p transcriber-desktop ingest::` passes; workspace `fmt`/`clippy -D warnings` clean; a large-file smoke is deferred to T13.

### [x] T12: Frontend IPC layer, job state hook, app wiring, drag-drop listener  [deps: T6]

- **Files**: `apps/desktop/src/api.ts`, `apps/desktop/src/api.test.ts`, `apps/desktop/src/state/useJobs.ts`, `apps/desktop/src/state/useJobs.test.ts`, `apps/desktop/src/App.tsx`, `apps/desktop/src/App.test.tsx`, `apps/desktop/src/styles.css`
- **Test first**: `api.test.ts` / `useJobs.test.ts` / `App.test.tsx` using `mockIPC` + `clearMocks` from `@tauri-apps/api/mocks` — cases: `api.enqueuePaths` invokes `enqueue_paths` with `{ paths }` and returns snapshots; a rejected promise carrying `{kind, message}` is surfaced as a typed `AppError`, never as an unhandled rejection (NFR-6); `useJobs` upserts by id from `jobs://updated` events so a job transitions `queued → running → done` in the rendered list with no user action (FR-14); events for an unknown id append rather than drop (FR-8); `App` renders the first-run picker when `meetings_root` is null and refuses drops in that state (FR-18); a simulated `tauri://drag-drop` `over` event puts the drop zone into hovering, `leave` restores idle, and `drop` with `["C:\\x\\ELS - 260812 - Security issue.mp4"]` invokes `enqueue_paths` with that exact absolute path (FR-4, FR-5); a mixed drop `[good.mp4, bad.txt]` renders one accepted and one rejected row (FR-6); "Choose file…" reaches the same `enqueue_paths` call as a drop of the same path (FR-7); Reveal invokes `reveal_job` with the job id, never with a path string (FR-15); Change… persists via `set_meetings_root` and re-renders the new root (FR-16).
- **Implement**: `api.ts` is the **only** module importing `@tauri-apps/api` — typed wrappers over the six commands, `listen` helpers for the two events, and the dialog-plugin calls for the folder/file pickers. Drag-drop uses `getCurrentWebview().onDragDropEvent`; no HTML5 `drop`/`dataTransfer` code path exists anywhere (grep-asserted). `useJobs` holds the session list and subscribes to events; `App` composes the T6 components and owns no formatting logic of its own. `styles.css` gains only global reset/layout tokens.
- **Skills**: `frontend-toolkit:internal-ui` (mandatory — **not installed**; degrade as above), `frontend-toolkit:ui-ux-pro-max` (**not installed**).
- **Done when**: `npm run test`, `npm run lint`, `npm run type`, `npm run format:check` pass; grep shows `@tauri-apps/api` imported only in `src/api.ts`; grep shows no `ondrop`/`dataTransfer` usage.

### [x] T10: Job registry, sequential pipeline and 1.5 s poll loop  [deps: T5, T7, T9]

- **Files**: `apps/desktop/src-tauri/src/jobs.rs`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/jobs.rs` driving the fake service and a `tempfile` vault, with an injected event sink instead of a Tauri handle — cases: `enqueue` returns snapshots synchronously in under 300 ms for three files while ingest is still pending (NFR-1); three enqueued files are ingested and submitted **strictly one at a time** in order and all three reach a terminal state (FR-8); a running job emits an update at least every 2 s while polling (NFR-4) with the interval set to 1.5 s; a job transitions `pending → ingesting → queued → running → done` emitting one snapshot per transition (FR-14); on `Done` the snapshot's `transcript_path` is `<meeting_dir>\transcript.json` (FR-15); a service-reported failure sets `failed` with the message verbatim (FR-14); with the fake service down, the file is still ingested (vault write asserted on disk) and the job ends in a distinct awaiting/failed-transcription state, never an ingest failure (FR-13); a rejected file in a mixed batch does not abort the accepted ones (FR-6); ingest work happens off the async runtime (assert the runtime stays responsive by racing a short timer against a slow ingest — NFR-2).
- **Implement**: `JobRegistry` behind `Arc<RwLock<…>>` holding `JobSnapshot`s; one background worker task consuming an mpsc queue so ingest+submit are serial; per-job poll task with a 1.5 s interval and a bounded consecutive-error tolerance that flips the service banner to unavailable. Events go through a small `EventSink` trait (Tauri `AppHandle` in production, a recording vec in tests) — this is what keeps `jobs.rs` unit-testable.
- **Skills**: —
- **Done when**: `cargo test -p transcriber-desktop jobs::` passes; workspace `fmt`/`clippy -D warnings` clean; the poll interval and its NFR-4 justification are a named constant.

### [x] T11: Tauri command handlers, app state wiring, capability pruning  [deps: T3, T4, T7, T8, T9, T10, T12]

- **Files**: `apps/desktop/src-tauri/src/commands.rs`, `apps/desktop/src-tauri/src/lib.rs`, `apps/desktop/src-tauri/src/main.rs`, `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/capabilities/default.json`, `apps/desktop/src-tauri/Cargo.toml`
- **Test first**: unit tests in `apps/desktop/src-tauri/src/commands.rs` calling the handler bodies through injectable state (no Tauri runtime) — cases: `enqueue_paths` with `[]`, with a 32k-character string, with a relative path, and with a path outside the meetings-root each return a typed `AppError` and never panic (NFR-6, FR-11); `enqueue_paths` before a meetings-root is configured returns `kind: "not_configured"` and writes nothing (FR-18); `reveal_job` with an unknown id returns `invalid_argument`, and with a job whose path was tampered to sit outside the root returns `outside_root` — asserting the containment check is in Rust, not the frontend (FR-15); `reveal_job` on a valid job builds the `explorer.exe /select,<canonical path>` argument vector from the registry, not from any caller string, and tolerates Explorer's nonzero exit code; `set_meetings_root` persists and triggers a sidecar restart with the new allowed root; `service_status` reports `unavailable` naming the URL when the seam is down (FR-13).
- **Implement**: Six `#[tauri::command]` handlers, each validating arguments first and returning `Result<T, AppError>`. `lib.rs` builds the app: resolve `app_config_dir()`, load settings (T3), pick the service implementation (sidecar-derived HTTP, or configured base URL, or the fake behind a `--fake-service`/`TRANSCRIBER_FAKE_SERVICE` dev switch), start the sidecar in the background so the window paints first (NFR-3), register the dialog plugin, manage state, and kill the sidecar on exit. Prune `capabilities/default.json` to the minimum that still works — remove each candidate permission, confirm a flow breaks, restore it, and record the justification in the file's `description`. Confirm `dragDropEnabled: true` and the final window config.
- **Skills**: —
- **Done when**: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace` pass; `npm run tauri dev` from `apps/desktop/` opens the window and the full drop→ingest→status→done flow works against the fake service with **no F2 process running**; `capabilities/default.json` contains no `core:default` and no unused permission.

### [x] T13: Integration verification — end-to-end flow, real app run, security assertions  [deps: T11]

- **Files**: `apps/desktop/src-tauri/tests/e2e_flow.rs`, `apps/desktop/src-tauri/tests/common/mod.rs`
- **Test first**: `apps/desktop/src-tauri/tests/e2e_flow.rs` — cases: with the fake service and a temp vault, enqueuing `ELS - 260812 - Security issue.mp4` drives the whole pipeline to `done`, leaves exactly `<root>/ELS/260812 - Security issue/source.mp4` on disk, and reports `transcript.json` under that folder (FR-9, FR-15); a badly named recording lands under `unsorted/` (FR-9); a `.txt` in the same batch is rejected while the media file completes (FR-6); with the fake service down the recording is still filed and the job is marked awaiting/failed transcription (FR-13); a crafted `..`-escaping path is refused with `outside_root` (FR-11); re-dropping the identical file applies F1's collision policy without overwriting (FR-9).
- **Implement**: Integration tests against the crate's public surface (`commands` + `jobs` + `ingest` wired as in `lib.rs`, event sink recorded). Then perform the profile-mandated real-app verification: `npm run tauri dev`, drive drop / choose-file / change-root / reveal by hand, and confirm NFR-2 by ingesting a ≥2 GB file while moving, resizing and clicking the window. Record the observed results in the task report (the written checklist itself is T14).
- **Skills**: —
- **Done when**: `cargo test --workspace` passes including the new integration tests; the app was actually launched and the flows driven; the 2 GB responsiveness observation and cold-start timing (NFR-3) are reported.

### [x] T14: Setup documentation, manual smoke checklist, QA/contract docs  [deps: T11]

- **Files**: `docs/setup.md`, `docs/manual-smoke-checklist.md`, `docs/config-contract.md`, `apps/desktop/README.md`
- **Test first**: `docs/manual-smoke-checklist.md` is itself the executable artifact — it must enumerate FR-21's steps as checkboxes with expected results: launch app → set meetings-root → drop `ELS - 260812 - Security issue.mp4` → observe ingest, `queued→running→done`, and the reported `transcript.json` path → drop `random meeting.mp4` → observe `unsorted/` placement → drop `notes.txt` → observe named rejection with no vault write → Reveal opens the meeting folder → restart the app and confirm the meetings-root persisted. Execute it against the real app and commit the recorded run (date, result per step) in the same file.
- **Implement**: `docs/setup.md` states the verified host prerequisites (MSVC 2022, Windows SDK 10.0.26100, WebView2 151, Node v22.17.1/npm 11.5.1, `uv` at `C:\Users\<user>\.local\bin\uv.exe`) and the one missing piece — `rustup` with `x86_64-pc-windows-msvc` — plus the exact clean-checkout sequence to a running window (FR-2). `apps/desktop/README.md` documents the QA commands and their FR-19 names (`cargo fmt --check` / `cargo clippy -- -D warnings` / `cargo test` at the repo root; `npm run format|lint|type|test` in `apps/desktop/`), and states plainly that `make` is **not installed** on this host and that the root `Makefile` wrapping these is F4's deliverable. `docs/config-contract.md` reproduces this plan's settings contract verbatim (path, identifier, schema v1, unknown-key preservation, sidecar env handshake) for F4 to consume (FR-17).
- **Skills**: —
- **Done when**: the checklist is committed **with its execution recorded**; a reader following `docs/setup.md` from a clean checkout reaches a running window; all four QA command groups pass on a clean tree and the doc's commands are copy-pasteable as written.

### [!] T15: OPTIONAL — tauri-driver E2E harness  [deps: T11]

- **Files**: `apps/desktop/e2e/tauri-driver.spec.ts`, `apps/desktop/e2e/README.md`, `apps/desktop/package.json`
- **Test first**: `apps/desktop/e2e/tauri-driver.spec.ts` — cases: the built app launches under `tauri-driver` + `msedgedriver`, the window title is `Transcriber`, the first-run state is visible with no config, and setting a meetings-root then invoking "Choose file…" with a fixture recording reaches a `done` job row (FR-20).
- **Implement**: Parked by default — FR-20 is a *should* and FR-21 (T14) is the required evidence path. Unpark only on operator request. Playwright cannot attach to a WebView2-hosted Tauri window; use `tauri-driver` with Microsoft Edge Driver matching the installed WebView2 (151.x) and a WebDriver client (`webdriverio`). Time-box it: if `msedgedriver` 151 or `tauri-driver` cannot be made to attach, stop, document the failure in `e2e/README.md`, and leave the task parked.
- **Skills**: `testing-toolkit:e2e-testing-patterns` (**not installed**; the profile's Playwright guidance does not transfer to Tauri on Windows — patterns only).
- **Done when**: the spec runs green locally against a built binary, **or** the task stays parked with the blocking reason documented.

## QA expectations

There is **no `Makefile` and no `make` on this host**, and the root `Makefile` is F4's deliverable — do not create one here. FR-19's four names exist as npm scripts plus their cargo equivalents:

| FR-19 name | Rust (repo root) | Frontend (`apps/desktop/`) |
|---|---|---|
| `format` | `cargo fmt` (`cargo fmt --check` in CI/gates) | `npm run format` / `npm run format:check` |
| `lint` | `cargo clippy --workspace --all-targets -- -D warnings` | `npm run lint` |
| `type` | — (compiler) | `npm run type` (`tsc --noEmit`) |
| `test` | `cargo test --workspace` | `npm run test` (`vitest run`) |

Every task's **Done when** requires the relevant group to pass on a clean tree. `clippy -D warnings` is the gate: a warning fails the build.

Known constraints and flakiness sources:
- `cargo` is only available after F1's rustup install; the first workspace build is slow (Tauri's dependency tree), so budget for it in T1.
- Rust unit tests must never touch `%APPDATA%` or a real vault — inject directories, use `tempfile`.
- No test may spawn the real F2 sidecar or require a whisper model; the fake service and `wiremock` cover the seam. Only T13's manual pass and T14's checklist involve real processes.
- `npm run tauri dev` compiles the Rust side on first run; a cold `tauri dev` is minutes, not seconds — NFR-3's 3 s budget is about the *built* app's cold start.
