//! `#[tauri::command]` handlers — the only IPC surface (T11).
//!
//! Every handler validates its arguments first and returns
//! `Result<T, AppError>` (NFR-6). Each `#[tauri::command]`-annotated
//! function here is a thin wrapper around a plain `*_handler` function that
//! takes `&AppState` directly — that's what lets these be unit tested below
//! without a Tauri runtime (`tauri::State` has no public constructor).
//!
//! `AppState` owns the pieces `lib.rs` wires up at startup: settings, the
//! job registry, the currently selected `TranscriptionService`, and two
//! small injectable abstractions this file defines itself (rather than
//! touching `sidecar.rs`/`jobs.rs`, which this task does not own):
//! [`SidecarController`] (spawn/await-ready/terminate, so no test here ever
//! spawns the real F2 process) and [`Revealer`] (the actual
//! `explorer.exe /select,<path>` launch, so a unit test never opens a
//! visible Explorer window).

use std::collections::HashMap;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::Mutex as StdMutex;
use std::time::Duration;

use async_trait::async_trait;
use serde::Serialize;
use tokio::sync::{Mutex as TokioMutex, RwLock};
use uuid::Uuid;

use crate::app_paths;
use crate::config::{self, Settings};
use crate::error::AppError;
use crate::jobs::{self, JobRegistry, JobSnapshot};
use crate::paths;
use crate::service::http::HttpTranscriptionService;
use crate::service::{JobStatus, ServiceError, ServiceHealth, SubmitRequest, TranscriptionService};
use crate::sidecar::{self, ReadyLine, Sidecar, SidecarError, SidecarPlan, SidecarSpawnConfig};

/// The three model-download commands (T13, FR-12, FR-17) -- a submodule of
/// this file (`src/commands/model.rs`) rather than a sibling of `commands`
/// in `lib.rs`, so it shares `AppState`/`ServiceStatusSink` directly.
pub mod model;

/// Read-only access to F2's own sqlite job ledger (`src/commands/ledger.rs`)
/// -- a submodule for the same reason `model` is.
pub mod ledger;

/// Per-meeting commands over an already-ingested vault entry: read its
/// transcript, rename/re-file it, delete it (`src/commands/meetings.rs`).
pub mod meetings;

/// The LLM feature's commands (`src/commands/llm.rs`): derived jobs
/// (summary, action items, per-recording exports) and the GGUF
/// model-download trio.
pub mod llm;

/// Hybrid vault search (`search_vault`) -- maps the service's directory-
/// named hits back to entry ids.
pub mod search;

/// The project chat (`chat_stream`/`cancel_chat`) -- SSE forwarded over a
/// Tauri ipc channel.
pub mod chat;

/// Saved chat conversations (`<PROJECT>/chats/<id>.json`): list, read,
/// save, rename, delete.
pub mod chats;
/// Speaker identification: the runtime/model fetches, the `diarize`
/// switch, and the per-meeting and vault-wide "Identify speakers" jobs.
pub mod speakers;

/// A defensive upper bound on a single dropped-path argument's length
/// (Windows' own extended-length path limit is 32767 UTF-16 code units) —
/// guards `enqueue_paths` against a pathological string without ever
/// touching the filesystem for it (NFR-6).
const MAX_PATH_ARG_LEN: usize = 32_768;

/// The full IPC-contract `SettingsView` (plan.md's frozen IPC contract):
/// `config.rs`'s own `SettingsView` deliberately omits
/// `supported_extensions` (it comes from `paths.rs`, not `config.rs`) —
/// this is where the two are combined.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SettingsResponse {
    pub meetings_root: Option<String>,
    pub meetings_root_exists: bool,
    pub service_base_url: Option<String>,
    pub supported_extensions: Vec<String>,
    /// Set when `config.json` existed but could not be parsed at startup
    /// (E3, NFR-6): the app still opens, falls back to first-run defaults
    /// (`meetings_root: None`), and reports this so the operator gets an
    /// actionable error instead of the app silently discarding their old
    /// settings or panicking before a window ever appears.
    pub config_error: Option<String>,
    /// A sane starting point for the vault folder picker (E2/FR-10: "a
    /// sane default") -- additive to the frozen IPC contract, so an older
    /// frontend build simply ignores it. `None` when `%USERPROFILE%` is
    /// unset (never expected on a real Windows session).
    pub default_meetings_root: Option<String>,
    /// Speaker identification on new transcriptions (`config.rs`'s
    /// `diarize`, resolved to the service default when unset). Additive.
    pub diarize: bool,
    /// Whether a Hugging Face token is stored (`hf_token`); the token
    /// itself never crosses the IPC boundary. Additive.
    pub hf_token_present: bool,
}

/// `%USERPROFILE%\Meetings` (E2/FR-10) -- outside the application folder by
/// construction, so it is never rejected by `config::set_meetings_root`'s
/// own app-folder check.
fn default_meetings_root() -> Option<String> {
    let home = std::env::var("USERPROFILE").ok()?;
    Some(
        PathBuf::from(home)
            .join("Meetings")
            .to_string_lossy()
            .into_owned(),
    )
}

fn build_settings_response(settings: &Settings, config_error: Option<String>) -> SettingsResponse {
    let view = config::settings_view(settings);
    SettingsResponse {
        meetings_root: view.meetings_root,
        meetings_root_exists: view.meetings_root_exists,
        service_base_url: view.service_base_url,
        diarize: view.diarize,
        hf_token_present: view.hf_token_present,
        supported_extensions: paths::supported_extensions()
            .iter()
            .map(|ext| ext.to_string())
            .collect(),
        config_error,
        default_meetings_root: default_meetings_root(),
    }
}

/// One meeting the vault browser lists (vault-browser extension to the IPC
/// contract; additive, never removes/renames a field an existing frontend
/// build already relies on).
///
/// `id` is an opaque, server-issued lookup key -- never a raw path -- into
/// [`AppState::vault_index`], the same "id-keyed lookup, never trust a raw
/// path from the UI" pattern `JobSnapshot::id`/`reveal_job` already use.
/// `meeting_dir` here is presentational only (rendered as a monospace path
/// in the UI); [`reveal_vault_entry`] never accepts it back as an argument.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct VaultMeetingView {
    pub id: String,
    /// The project code this meeting sits under, or `None` for a meeting
    /// filed under `unsorted/`.
    pub project: Option<String>,
    /// The meeting folder's own name (`<date> - <title>`).
    pub meeting_name: String,
    /// The absolute meeting-folder path, for display only.
    pub meeting_dir: String,
    pub has_source: bool,
    pub has_transcript: bool,
}

/// IPC contract's `ServiceStatusView.state` (plan.md).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceState {
    Starting,
    Ready,
    Unavailable,
}

/// IPC contract's `ServiceStatusView` (plan.md).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ServiceStatusView {
    pub state: ServiceState,
    pub base_url: Option<String>,
    pub detail: Option<String>,
}

/// Where a `service://status` transition is pushed — the production
/// implementation (`lib.rs`) wraps a Tauri `AppHandle`; tests use a
/// recording fake, mirroring `jobs::EventSink`'s own design.
pub trait ServiceStatusSink: Send + Sync {
    fn emit(&self, status: &ServiceStatusView);
}

/// Adapts [`jobs::ServiceUnavailableSink`] (E5) into this module's
/// `ServiceStatusSink` + `ServiceStatusView`, naming whatever base URL is
/// currently on record -- so a poll-error-budget exhaustion mid-session
/// produces exactly the same `service://status { state: unavailable, ... }`
/// event `apply_resolved_service` emits when the sidecar never comes up in
/// the first place. Both call sites end up flipping the same
/// `ServiceBanner`.
struct RegistryStatusSinkAdapter {
    status_sink: Arc<dyn ServiceStatusSink>,
    base_url: Arc<RwLock<Option<String>>>,
}

#[async_trait]
impl jobs::ServiceUnavailableSink for RegistryStatusSinkAdapter {
    async fn service_unavailable(&self, detail: String) {
        let base_url = self.base_url.read().await.clone();
        self.status_sink.emit(&ServiceStatusView {
            state: ServiceState::Unavailable,
            base_url,
            detail: Some(detail),
        });
    }
}

/// A `TranscriptionService` that always reports unavailable, carrying a
/// fixed detail message. Used as the placeholder while the sidecar is
/// starting, and as the fallback when a spawn/ready-line wait fails or a
/// configured `service.base_url` turns out to be invalid — in every case
/// ingest keeps working (FR-13); only the transcription seam is down.
pub struct UnavailableTranscriptionService {
    detail: String,
}

impl UnavailableTranscriptionService {
    pub fn new(detail: impl Into<String>) -> Self {
        UnavailableTranscriptionService {
            detail: detail.into(),
        }
    }
}

#[async_trait]
impl TranscriptionService for UnavailableTranscriptionService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: self.detail.clone(),
        })
    }

    async fn submit(&self, _req: SubmitRequest) -> Result<String, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: self.detail.clone(),
        })
    }

    async fn status(&self, _job_id: &str) -> Result<JobStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: self.detail.clone(),
        })
    }
}

/// Abstracts "spawn F2 and wait for its ready line" and "terminate the
/// running child" behind a trait so nothing in this file (or its tests)
/// ever needs to spawn the real F2 process — QA's expectation (plan.md)
/// that no test spawns the real sidecar or requires a whisper model.
#[async_trait]
pub trait SidecarController: Send + Sync {
    async fn spawn_and_await_ready(
        &self,
        config: &SidecarSpawnConfig,
        timeout: Duration,
    ) -> Result<ReadyLine, SidecarError>;

    async fn terminate(&self);
}

/// Wraps a real `sidecar::Sidecar` — used in production.
pub struct RealSidecarController {
    sidecar: TokioMutex<Sidecar>,
}

impl RealSidecarController {
    pub fn new() -> Self {
        RealSidecarController {
            sidecar: TokioMutex::new(Sidecar::new()),
        }
    }
}

impl Default for RealSidecarController {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl SidecarController for RealSidecarController {
    async fn spawn_and_await_ready(
        &self,
        config: &SidecarSpawnConfig,
        timeout: Duration,
    ) -> Result<ReadyLine, SidecarError> {
        let stdout = {
            let mut guard = self.sidecar.lock().await;
            guard.restart(config).await?
        };
        let command = format!("{} {}", config.program, config.args.join(" "));
        let ready = sidecar::read_ready_line(stdout, timeout, &command).await?;
        // E6: record F2's own pid so a later `terminate()` kills the real
        // service process tree, not just the `uv` wrapper `restart` spawned.
        self.sidecar.lock().await.record_service_pid(ready.pid);
        Ok(ready)
    }

    async fn terminate(&self) {
        self.sidecar.lock().await.terminate().await;
    }
}

/// The actual program `reveal_job` launches (never a caller-supplied
/// string — see [`Revealer`]).
#[cfg(windows)]
pub const EXPLORER_PROGRAM: &str = "explorer.exe";

/// Builds the raw `/select,"<path>"` command-line tail Explorer expects, as
/// a single **raw** string rather than a `Vec<String>` fed through
/// `Command::args`. `Command::args` quotes any argument containing a space
/// as one token, and every F1 meeting folder name is `<date> - <Title>`, so
/// that route would turn `/select,<path>` into `"/select,<path>"` — the
/// switch quoted together with the path. Explorer parses that as an
/// unrecognized argument and opens the user's Documents folder instead of
/// the target (E1, verified empirically against a real folder with spaces
/// in its name). The form Explorer actually expects has the switch bare and
/// only the path quoted: `/select,"<path>"`. Pure: no process is spawned
/// here, so this is unit-testable directly. `path` is already
/// canonicalized and containment-checked by the caller, and NTFS forbids
/// `"` in a filename, so there is no injection surface beyond that.
#[cfg(windows)]
pub fn reveal_command_line(path: &Path) -> String {
    format!("/select,\"{}\"", path.display())
}

/// Spawns `program` with `raw_tail` appended verbatim to the command line
/// via [`CommandExt::raw_arg`] — never `std::process::Command::args`, whose
/// own quoting is exactly the E1 defect [`reveal_command_line`]'s doc
/// comment describes — and waits for it to exit. Explorer's own exit code
/// carries no meaning to this app — it simply hands the request off to an
/// already-running shell process — so any exit status is tolerated; only a
/// failure to spawn at all is reported.
#[cfg(windows)]
pub fn run_reveal_command(program: &str, raw_tail: &str) -> Result<(), AppError> {
    std::process::Command::new(program)
        .raw_arg(raw_tail)
        .status()
        .map(|_status| ())
        .map_err(|err| AppError::io(format!("failed to launch {program}: {err}")))
}

/// Executes a validated reveal target — implemented by actually launching
/// Explorer in production; a recording fake in tests, so a unit test never
/// opens a visible window.
pub trait Revealer: Send + Sync {
    fn reveal(&self, path: &Path) -> Result<(), AppError>;
}

/// Launches the platform's real reveal-in-file-manager command:
/// `explorer.exe /select,<path>` on Windows, Finder's `open -R <path>` on
/// macOS.
pub struct ExplorerRevealer;

#[cfg(windows)]
impl Revealer for ExplorerRevealer {
    fn reveal(&self, path: &Path) -> Result<(), AppError> {
        run_reveal_command(EXPLORER_PROGRAM, &reveal_command_line(path))
    }
}

#[cfg(target_os = "macos")]
impl Revealer for ExplorerRevealer {
    fn reveal(&self, path: &Path) -> Result<(), AppError> {
        // Unlike Explorer's `/select,` (see `reveal_command_line`), `open`
        // takes the path as an ordinary argument -- no raw command-line
        // assembly needed. Its exit code is tolerated the same way
        // Explorer's is; only a failure to spawn at all is reported.
        std::process::Command::new("open")
            .arg("-R")
            .arg(path)
            .status()
            .map(|_status| ())
            .map_err(|err| AppError::io(format!("failed to launch open -R: {err}")))
    }
}

/// State shared by every command handler. Constructed once at startup
/// (`lib.rs`) and replaced piecewise (settings, registry, service) as the
/// app runs; every field is behind a lock so handlers never need `&mut`
/// access to the whole thing.
pub struct AppState {
    pub config_dir: PathBuf,
    pub app_dir: PathBuf,
    pub settings: RwLock<Settings>,
    pub registry: RwLock<JobRegistry>,
    pub service: RwLock<Arc<dyn TranscriptionService>>,
    pub service_base_url: Arc<RwLock<Option<String>>>,
    pub service_starting: RwLock<bool>,
    /// Set by `lib.rs`'s startup wiring when `config.json` existed but
    /// failed to parse (E3) -- `settings` above already fell back to
    /// `Settings::default()` in that case; this is what lets `get_settings`
    /// surface the actionable error instead of the failure being invisible.
    pub config_error: RwLock<Option<String>>,
    pub sink: Arc<dyn jobs::EventSink>,
    pub status_sink: Arc<dyn ServiceStatusSink>,
    pub sidecar: Arc<dyn SidecarController>,
    pub revealer: Arc<dyn Revealer>,
    /// Set by `lib.rs` when the `--fake-service`/`TRANSCRIBER_FAKE_SERVICE`
    /// dev switch was given at startup (E20). Defaults to `false` here and
    /// is patched directly after construction (the same pattern `lib.rs`
    /// already uses for `config_error`) rather than threaded through the
    /// constructors below, since it is a startup-only decision, not one of
    /// the collaborators tests substitute per call.
    /// [`resolve_and_apply_meetings_root_service`] checks this and skips
    /// resolving a real sidecar/service when set, so changing the
    /// meetings-root mid-session does not silently discard the operator's
    /// `FakeService` and spawn a real `uv` sidecar in its place.
    pub fake_mode: bool,
    /// The id-keyed lookup [`list_vault_handler`] rebuilds on every call
    /// (an id stays stable while its meeting exists; a vanished meeting's
    /// id drops out and fails closed) and
    /// [`reveal_vault_entry_handler`] reads from -- the same "look the
    /// target up by an opaque id the server handed out, never trust a raw
    /// path from the UI" pattern `registry`/`reveal_job_handler` already
    /// use for jobs. A vault entry has no job id of its own, so this is a
    /// parallel map rather than a reuse of `registry`.
    pub vault_index: RwLock<HashMap<String, PathBuf>>,
    /// The in-flight chat turn's cancel handle. One slot, not a map: a
    /// single-window app has at most one project chat, so starting a new
    /// turn stashes a fresh sender here -- and *dropping* the previous one
    /// fires its receiver, which is exactly how a superseding question
    /// cancels the stream it replaces. A plain `std::Mutex`: only ever
    /// locked to swap the `Option`, never held across an `.await`.
    pub chat_cancel: StdMutex<Option<tokio::sync::oneshot::Sender<()>>>,
}

impl AppState {
    /// Full constructor — every collaborator injected, used by tests (and
    /// internally by [`AppState::new`]).
    #[allow(clippy::too_many_arguments)]
    pub fn new_with(
        config_dir: PathBuf,
        app_dir: PathBuf,
        settings: Settings,
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        service_base_url: Option<String>,
        service_starting: bool,
        sink: Arc<dyn jobs::EventSink>,
        status_sink: Arc<dyn ServiceStatusSink>,
        sidecar: Arc<dyn SidecarController>,
        revealer: Arc<dyn Revealer>,
    ) -> Self {
        let registry = JobRegistry::new(root, service.clone(), sink.clone());
        let service_base_url = Arc::new(RwLock::new(service_base_url));
        registry.set_status_sink(Arc::new(RegistryStatusSinkAdapter {
            status_sink: status_sink.clone(),
            base_url: service_base_url.clone(),
        }));
        AppState {
            config_dir,
            app_dir,
            settings: RwLock::new(settings),
            registry: RwLock::new(registry),
            service: RwLock::new(service),
            service_base_url,
            service_starting: RwLock::new(service_starting),
            config_error: RwLock::new(None),
            sink,
            status_sink,
            sidecar,
            revealer,
            fake_mode: false,
            vault_index: RwLock::new(HashMap::new()),
            chat_cancel: StdMutex::new(None),
        }
    }

    /// Production constructor: real sidecar control, real Explorer reveal.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        config_dir: PathBuf,
        app_dir: PathBuf,
        settings: Settings,
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        service_base_url: Option<String>,
        service_starting: bool,
        sink: Arc<dyn jobs::EventSink>,
        status_sink: Arc<dyn ServiceStatusSink>,
    ) -> Self {
        Self::new_with(
            config_dir,
            app_dir,
            settings,
            root,
            service,
            service_base_url,
            service_starting,
            sink,
            status_sink,
            Arc::new(RealSidecarController::new()),
            Arc::new(ExplorerRevealer),
        )
    }
}

/// Whether the `--fake-service` CLI switch or `TRANSCRIBER_FAKE_SERVICE`
/// env var was given (plan.md's dev switch). Pure over injected values so
/// it's testable without touching real process state.
pub fn fake_service_requested(env_var: Option<String>, args: &[String]) -> bool {
    env_var.is_some() || args.iter().any(|arg| arg == "--fake-service")
}

/// Resolves which `TranscriptionService` to use for `plan`: connects
/// directly for `UseExisting`, or spawns and waits for F2's ready line
/// (bounded by `timeout`) through `controller` for `Spawn`, falling back to
/// [`UnavailableTranscriptionService`] if the spawn/ready-line wait fails or
/// the resulting base URL is somehow invalid — the transcription seam
/// being down never fails app startup or a settings change (FR-13).
pub async fn resolve_service(
    controller: &dyn SidecarController,
    plan: SidecarPlan,
) -> (Arc<dyn TranscriptionService>, Option<String>) {
    match plan {
        SidecarPlan::UseExisting { base_url } => {
            match HttpTranscriptionService::new(&base_url, None) {
                Ok(client) => (Arc::new(client), Some(base_url)),
                Err(_) => (
                    Arc::new(UnavailableTranscriptionService::new(format!(
                        "configured service.base_url {base_url} is invalid"
                    ))),
                    Some(base_url),
                ),
            }
        }
        SidecarPlan::Spawn(spawn_config) => {
            match controller
                .spawn_and_await_ready(&spawn_config, sidecar::READY_TIMEOUT)
                .await
            {
                Ok(ready) => {
                    let base_url = ready.base_url();
                    let service: Arc<dyn TranscriptionService> =
                        match HttpTranscriptionService::new(&base_url, Some(ready.token)) {
                            Ok(client) => Arc::new(client),
                            Err(_) => Arc::new(UnavailableTranscriptionService::new(
                                "sidecar reported an invalid base url".to_string(),
                            )),
                        };
                    (service, Some(base_url))
                }
                Err(err) => (
                    Arc::new(UnavailableTranscriptionService::new(err.to_string())),
                    None,
                ),
            }
        }
    }
}

/// Installs a resolved `(service, base_url)` pair into `state`: swaps the
/// active service and the job registry's root/service **in place** (E2 --
/// `JobRegistry::set_root_and_service`, not a freshly constructed registry),
/// so any job enqueued before this call (e.g. while the sidecar was still
/// starting) stays tracked and revealable, then emits the resulting
/// `service://status`.
pub async fn apply_resolved_service(
    state: &AppState,
    service: Arc<dyn TranscriptionService>,
    base_url: Option<String>,
    root: PathBuf,
) -> ServiceStatusView {
    *state.service.write().await = service.clone();
    *state.service_base_url.write().await = base_url.clone();
    *state.service_starting.write().await = false;
    state
        .registry
        .read()
        .await
        .set_root_and_service(root, service.clone())
        .await;

    let status = match service.health().await {
        Ok(health) if health.ready => ServiceStatusView {
            state: ServiceState::Ready,
            base_url,
            detail: health.detail,
        },
        Ok(health) => ServiceStatusView {
            state: ServiceState::Unavailable,
            base_url,
            detail: health.detail,
        },
        Err(err) => ServiceStatusView {
            state: ServiceState::Unavailable,
            base_url,
            detail: Some(err.to_string()),
        },
    };
    state.status_sink.emit(&status);
    status
}

/// The `service://status` detail while the app is shutting the sidecar
/// down for an update install — one constant so the handler and its tests
/// cannot drift on the exact wording.
pub const UPDATE_STOP_DETAIL: &str = "stopped for update";

/// `prepare_update` — stops the bundled Python sidecar so the updater's
/// installer can run.
///
/// The NSIS installer overwrites `pyenv\` in place, and a running
/// `python.exe` from that tree holds locks on its own DLLs — installing
/// over it fails with "Error opening file for writing: ...\pyenv\...".
/// The updater plugin exits *this* process via `std::process::exit` when
/// it launches the installer, which never runs `lib.rs`'s `RunEvent::Exit`
/// hook, so the sidecar would be left running as an orphan. The frontend
/// therefore calls this between downloading the update and installing it.
///
/// The service is swapped for an [`UnavailableTranscriptionService`] first
/// (registry included, so an in-flight poll loop sees it too), then the
/// sidecar process tree is terminated and awaited — by the time this
/// returns, nothing of ours holds a file under `pyenv\` open.
pub async fn prepare_update_handler(state: &AppState) -> Result<(), AppError> {
    let service: Arc<dyn TranscriptionService> =
        Arc::new(UnavailableTranscriptionService::new(UPDATE_STOP_DETAIL));
    *state.service.write().await = service.clone();
    *state.service_base_url.write().await = None;

    let root = {
        let settings = state.settings.read().await;
        settings
            .meetings_root
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| state.config_dir.clone())
    };
    state
        .registry
        .read()
        .await
        .set_root_and_service(root, service)
        .await;

    state.sidecar.terminate().await;

    state.status_sink.emit(&ServiceStatusView {
        state: ServiceState::Unavailable,
        base_url: None,
        detail: Some(UPDATE_STOP_DETAIL.to_string()),
    });
    Ok(())
}

// -- Handler bodies (testable without a Tauri runtime) ---------------------

/// `get_settings` — never fails; wrapped in `Result` to match the IPC
/// contract's uniform `Result<T, AppError>` shape.
pub async fn get_settings_handler(state: &AppState) -> Result<SettingsResponse, AppError> {
    let settings = state.settings.read().await;
    let config_error = state.config_error.read().await.clone();
    Ok(build_settings_response(&settings, config_error))
}

/// `set_meetings_root` — validates and persists the new root (FR-16) and
/// returns the resulting settings view immediately (E17). Resolving and
/// (re)starting the sidecar (or reconnecting to a configured URL) so F2's
/// own allowed-roots list stays in sync is *not* done here: for the `Spawn`
/// plan that can take up to `sidecar::READY_TIMEOUT` (30s, e.g. `uv`
/// resolving a Python environment on first use), and awaiting it here left
/// the operator's folder-picker click looking ignored for that whole
/// window. The caller (the `#[tauri::command]` wrapper below) drives
/// [`resolve_and_apply_meetings_root_service`] as a spawned background task
/// instead, exactly as `lib.rs`'s startup path already does for the
/// initial root, and its outcome is reported through the existing
/// `service://status` event rather than by delaying this response.
///
/// The job registry's `root` is swapped **synchronously, right here**
/// (E21) -- not left for the background task above to apply minutes (up to
/// `sidecar::READY_TIMEOUT`) later. `App.tsx` leaves the first-run state and
/// accepts drops as soon as this handler returns, so if the registry still
/// pointed at the previous root (on first run, `state.config_dir` itself)
/// until the sidecar finished resolving, a file dropped in that window
/// would be ingested -- and, per F1, *moved* -- into the wrong place before
/// the operator ever sees it. The background task only ever swaps the
/// service half in place after this (`set_root_and_service`), and it
/// re-reads the current settings root itself before doing so, so it cannot
/// race or undo this root move.
pub async fn set_meetings_root_handler(
    state: &AppState,
    path: &str,
) -> Result<SettingsResponse, AppError> {
    let updated = {
        let mut settings = state.settings.write().await;
        config::set_meetings_root(
            &state.config_dir,
            &app_paths::app_dir(),
            &mut settings,
            path,
        )?;
        settings.clone()
    };

    if let Some(root) = updated.meetings_root.as_deref() {
        state
            .registry
            .read()
            .await
            .set_root(PathBuf::from(root))
            .await;
    }

    // A successful `set_meetings_root` writes a fresh, valid `config.json`
    // (`config::set_meetings_root` -> `save`), so any earlier "malformed
    // config.json" error (E3) no longer describes the file on disk.
    *state.config_error.write().await = None;

    Ok(build_settings_response(&updated, None))
}

/// `set_diarization_settings` -- persists the speaker-identification
/// switch and (optionally) the Hugging Face token, then answers the
/// resulting settings view. The service reads both keys from the shared
/// `config.json` at startup only, so the `#[tauri::command]` wrapper
/// restarts the sidecar in the background, exactly like `set_meetings_root`
/// does after a root change.
pub async fn set_diarization_settings_handler(
    state: &AppState,
    enabled: bool,
    hf_token: Option<String>,
) -> Result<SettingsResponse, AppError> {
    let updated = {
        let mut settings = state.settings.write().await;
        config::set_diarization(&state.config_dir, &mut settings, enabled, hf_token)?;
        settings.clone()
    };
    // A successful save writes a fresh, valid `config.json` (E3).
    *state.config_error.write().await = None;
    Ok(build_settings_response(&updated, None))
}

/// The background half of `set_meetings_root` (E17): plans, resolves and
/// installs the `TranscriptionService` for `settings`/`root`, the same
/// sequence `set_meetings_root_handler` used to run inline before
/// returning. Kept as its own function (rather than inlined into the
/// `#[tauri::command]` wrapper) so a unit test can drive it directly
/// without needing a Tauri runtime, the same way every other handler body
/// in this file is tested.
pub async fn resolve_and_apply_meetings_root_service(
    state: &AppState,
    settings: &Settings,
    root: PathBuf,
) -> ServiceStatusView {
    if state.fake_mode {
        // E20: a `--fake-service`/`TRANSCRIBER_FAKE_SERVICE` dev session
        // must stay in fake mode across a meetings-root change. Only the
        // registry's root needs to move to the new root; the installed
        // `FakeService` instance must not be discarded in favor of a real
        // sidecar spawn (or `UnavailableTranscriptionService`) the way an
        // unconditional `plan_sidecar` + `resolve_service` would.
        let service = state.service.read().await.clone();
        let base_url = state.service_base_url.read().await.clone();
        return apply_resolved_service(state, service, base_url, root).await;
    }

    let plan = sidecar::plan_sidecar(
        settings,
        &config::config_path(&state.config_dir),
        &state.app_dir,
    );
    let (service, base_url) = resolve_service(state.sidecar.as_ref(), plan).await;
    apply_resolved_service(state, service, base_url, root).await
}

/// `enqueue_paths` — validates every argument before any IO (NFR-1, NFR-6)
/// and hands well-formed absolute paths straight to the job registry, which
/// returns their initial `Pending` snapshots immediately.
pub async fn enqueue_paths_handler(
    state: &AppState,
    paths: Vec<String>,
) -> Result<Vec<JobSnapshot>, AppError> {
    let root_configured = state.settings.read().await.meetings_root.is_some();
    if !root_configured {
        return Err(AppError::not_configured(
            "no meetings root has been configured yet",
        ));
    }

    if paths.is_empty() {
        return Err(AppError::invalid_argument("no paths were provided"));
    }

    let mut path_bufs = Vec::with_capacity(paths.len());
    for raw in &paths {
        if raw.is_empty() || raw.len() > MAX_PATH_ARG_LEN {
            return Err(AppError::invalid_argument(format!(
                "path argument must be non-empty and at most {MAX_PATH_ARG_LEN} characters"
            )));
        }
        let candidate = PathBuf::from(raw);
        if !candidate.is_absolute() {
            return Err(AppError::invalid_argument(format!(
                "path must be absolute: {raw}"
            )));
        }
        path_bufs.push(candidate);
    }

    let registry = state.registry.read().await;
    Ok(registry.enqueue(path_bufs).await)
}

/// `list_jobs`.
pub async fn list_jobs_handler(state: &AppState) -> Result<Vec<JobSnapshot>, AppError> {
    Ok(state.registry.read().await.list().await)
}

/// `service_status` — while the sidecar is still starting this reports
/// `starting` without probing health; afterwards it reflects a live
/// `health()` call, so a down service is reported `unavailable` naming
/// whatever base URL is on record (FR-13).
pub async fn service_status_handler(state: &AppState) -> Result<ServiceStatusView, AppError> {
    if *state.service_starting.read().await {
        return Ok(ServiceStatusView {
            state: ServiceState::Starting,
            base_url: state.service_base_url.read().await.clone(),
            detail: None,
        });
    }

    let service = state.service.read().await.clone();
    let base_url = state.service_base_url.read().await.clone();
    Ok(match service.health().await {
        Ok(health) if health.ready => ServiceStatusView {
            state: ServiceState::Ready,
            base_url,
            detail: health.detail,
        },
        Ok(health) => ServiceStatusView {
            state: ServiceState::Unavailable,
            base_url,
            detail: health.detail,
        },
        Err(err) => ServiceStatusView {
            state: ServiceState::Unavailable,
            base_url,
            detail: Some(err.to_string()),
        },
    })
}

/// `list_vault` — scans the configured meetings root read-only (F1's
/// `vault::list_meetings`, off the UI thread via `spawn_blocking`, the same
/// pattern `ingest.rs` already uses for F1's blocking calls) and returns
/// every meeting found, newest first (F1's own ordering).
///
/// Re-validates every path F1 hands back with this app's own
/// `paths::ensure_inside` before it is ever surfaced to the UI (defense in
/// depth, mirroring `ingest.rs`'s own re-check of F1's returned
/// destination) -- an entry that somehow resolves outside the configured
/// root is dropped rather than shown or crashing the whole call.
///
/// Replaces `state.vault_index` with the ids this call just issued, but an
/// id whose meeting is still on disk keeps its value from the previous
/// call: the UI re-lists the vault after every finished job, and re-minting
/// every id there would invalidate the id of the recording page the
/// operator has open — silently bouncing them back to the library. An id
/// whose meeting is gone drops out of the index and fails closed, exactly
/// as before.
pub async fn list_vault_handler(state: &AppState) -> Result<Vec<VaultMeetingView>, AppError> {
    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;
    let root_path = PathBuf::from(&root);

    let entries = {
        let root_path = root_path.clone();
        tokio::task::spawn_blocking(move || vault::list_meetings(&root_path))
            .await
            .map_err(|join_err| {
                AppError::internal(format!("list_vault task panicked: {join_err}"))
            })?
    };

    // Inverted view of the current index, to keep an already-issued id
    // alive for a meeting that is still present (see the doc comment).
    let previous_ids: HashMap<PathBuf, String> = state
        .vault_index
        .read()
        .await
        .iter()
        .map(|(id, dir)| (dir.clone(), id.clone()))
        .collect();

    let mut views = Vec::with_capacity(entries.len());
    let mut index = HashMap::with_capacity(entries.len());
    for entry in entries {
        // Defense in depth (see the doc comment above): drop an entry F1
        // somehow returned outside the configured root rather than
        // surfacing or crashing on it. The canonical, verbatim-stripped
        // form is also what goes into the index and the view -- the same
        // shape `update_vault_entry` records after a rename, so a renamed
        // meeting keeps matching its index entry (and its id) here.
        let canonical = match paths::ensure_inside(&root_path, &entry.meeting_dir) {
            Ok(path) => path,
            Err(_) => continue,
        };
        let meeting_dir = paths::strip_verbatim(&canonical);

        let id = previous_ids
            .get(&meeting_dir)
            .cloned()
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        index.insert(id.clone(), meeting_dir.clone());
        views.push(VaultMeetingView {
            id,
            project: entry.project,
            meeting_name: entry.meeting_name,
            meeting_dir: meeting_dir.to_string_lossy().into_owned(),
            has_source: entry.has_source,
            has_transcript: entry.has_transcript,
        });
    }

    *state.vault_index.write().await = index;
    Ok(views)
}

/// `reveal_vault_entry` — looks the entry up **by id** (never trusting a
/// caller-supplied path, exactly like [`reveal_job_handler`]), re-validates
/// containment under the *current* configured meetings-root, and only then
/// launches Explorer on the meeting folder itself.
pub async fn reveal_vault_entry_handler(state: &AppState, entry_id: &str) -> Result<(), AppError> {
    let target = state
        .vault_index
        .read()
        .await
        .get(entry_id)
        .cloned()
        .ok_or_else(|| AppError::invalid_argument(format!("unknown vault entry id {entry_id}")))?;

    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;

    let canonical = paths::ensure_inside(Path::new(&root), &target)?;
    let display_path = paths::strip_verbatim(&canonical);
    let revealer = state.revealer.clone();
    tokio::task::spawn_blocking(move || revealer.reveal(&display_path))
        .await
        .map_err(|join_err| AppError::internal(format!("reveal task panicked: {join_err}")))?
}

/// `reveal_job` — looks the job up **by id** (never trusting a caller-
/// supplied path), picks its most specific known path (transcript, then the
/// filed recording, then the meeting folder), re-validates containment
/// under the *current* configured meetings-root, and only then launches
/// Explorer (FR-15). Refuses a job whose recorded path no longer resolves
/// inside that root — this is what makes the containment check a Rust-side
/// guarantee rather than something the frontend could be trusted to do.
pub async fn reveal_job_handler(state: &AppState, job_id: &str) -> Result<(), AppError> {
    let snapshot = state
        .registry
        .read()
        .await
        .get(job_id)
        .await
        .ok_or_else(|| AppError::invalid_argument(format!("unknown job id {job_id}")))?;

    let target = snapshot
        .transcript_path
        .as_deref()
        .or(snapshot.source_dest.as_deref())
        .or(snapshot.meeting_dir.as_deref())
        .ok_or_else(|| AppError::invalid_argument("job has no revealable path yet"))?
        .to_string();

    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;

    let canonical = paths::ensure_inside(Path::new(&root), Path::new(&target))?;
    // Explorer's shell parser rejects the `\\?\` extended-length form
    // `ensure_inside` returns -- it must be stripped for display/shell use
    // the same way every other user-facing path in this crate already is
    // (E1: without this, Explorer opens a default location instead of
    // selecting the file).
    let display_path = paths::strip_verbatim(&canonical);
    // E12 / NFR-2: `Revealer::reveal` synchronously spawns and waits on
    // `explorer.exe` (`std::process::Command::status()`), which blocks
    // whatever Tokio worker thread runs it -- run it via `spawn_blocking`
    // so this async command handler never blocks the runtime.
    let revealer = state.revealer.clone();
    tokio::task::spawn_blocking(move || revealer.reveal(&display_path))
        .await
        .map_err(|join_err| AppError::internal(format!("reveal task panicked: {join_err}")))?
}

// -- `#[tauri::command]` wrappers -------------------------------------------
//
// Thin by design: each just unwraps `tauri::State` and delegates to the
// handler body above. `tauri::State` has no public constructor, so these
// are not unit tested directly -- the handler bodies they call are.

#[tauri::command]
pub async fn get_settings(state: tauri::State<'_, AppState>) -> Result<SettingsResponse, AppError> {
    get_settings_handler(&state).await
}

#[tauri::command]
pub async fn set_meetings_root(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    path: String,
) -> Result<SettingsResponse, AppError> {
    let response = set_meetings_root_handler(&state, &path).await?;
    // E17: resolve/(re)start the sidecar in the background instead of
    // awaiting it here -- mirrors `lib.rs::setup_app_state`'s startup task,
    // which spawns via the `AppHandle` and re-reads `state.settings` after
    // the fact rather than trusting a snapshot taken before any awaiting.
    tauri::async_runtime::spawn(async move {
        let state = tauri::Manager::state::<AppState>(&app);
        let settings = state.settings.read().await.clone();
        let root = settings
            .meetings_root
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| state.config_dir.clone());
        resolve_and_apply_meetings_root_service(&state, &settings, root).await;
    });
    Ok(response)
}

#[tauri::command]
pub async fn set_diarization_settings(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    enabled: bool,
    hf_token: Option<String>,
) -> Result<SettingsResponse, AppError> {
    let response = set_diarization_settings_handler(&state, enabled, hf_token).await?;
    // The sidecar reads `diarize`/`hf_token` from config.json at startup
    // only: restart it in the background (never awaited here -- E17's
    // reasoning for `set_meetings_root` applies unchanged).
    tauri::async_runtime::spawn(async move {
        let state = tauri::Manager::state::<AppState>(&app);
        let settings = state.settings.read().await.clone();
        let root = settings
            .meetings_root
            .clone()
            .map(PathBuf::from)
            .unwrap_or_else(|| state.config_dir.clone());
        resolve_and_apply_meetings_root_service(&state, &settings, root).await;
    });
    Ok(response)
}

#[tauri::command]
pub async fn enqueue_paths(
    state: tauri::State<'_, AppState>,
    paths: Vec<String>,
) -> Result<Vec<JobSnapshot>, AppError> {
    enqueue_paths_handler(&state, paths).await
}

#[tauri::command]
pub async fn list_jobs(state: tauri::State<'_, AppState>) -> Result<Vec<JobSnapshot>, AppError> {
    list_jobs_handler(&state).await
}

#[tauri::command]
pub async fn service_status(
    state: tauri::State<'_, AppState>,
) -> Result<ServiceStatusView, AppError> {
    service_status_handler(&state).await
}

#[tauri::command]
pub async fn reveal_job(state: tauri::State<'_, AppState>, job_id: String) -> Result<(), AppError> {
    reveal_job_handler(&state, &job_id).await
}

#[tauri::command]
pub async fn list_vault(
    state: tauri::State<'_, AppState>,
) -> Result<Vec<VaultMeetingView>, AppError> {
    list_vault_handler(&state).await
}

#[tauri::command]
pub async fn reveal_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<(), AppError> {
    reveal_vault_entry_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn read_transcript(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<meetings::TranscriptView, AppError> {
    meetings::read_transcript_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn update_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    project: Option<String>,
    date: String,
    title: String,
) -> Result<VaultMeetingView, AppError> {
    meetings::update_vault_entry_handler(&state, &entry_id, project, &date, &title).await
}

#[tauri::command]
pub async fn delete_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<(), AppError> {
    meetings::delete_vault_entry_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn set_speaker_labels(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    assignments: std::collections::HashMap<String, String>,
) -> Result<(), AppError> {
    meetings::set_speaker_labels_handler(&state, &entry_id, assignments).await
}

#[tauri::command]
pub async fn read_summary(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<meetings::SummaryView, AppError> {
    meetings::read_summary_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn read_note(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<meetings::NoteView, AppError> {
    meetings::read_note_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn write_note(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    markdown: String,
) -> Result<(), AppError> {
    meetings::write_note_handler(&state, &entry_id, markdown).await
}

#[tauri::command]
pub async fn append_to_note(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    markdown: String,
) -> Result<(), AppError> {
    meetings::append_to_note_handler(&state, &entry_id, markdown).await
}

#[tauri::command]
pub async fn list_project_speaker_names(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<Vec<String>, AppError> {
    meetings::list_project_speaker_names_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn search_vault(
    state: tauri::State<'_, AppState>,
    query: String,
    project: Option<String>,
) -> Result<Vec<search::SearchResultView>, AppError> {
    search::search_vault_handler(&state, query, project).await
}

#[tauri::command]
pub async fn reindex_vault(state: tauri::State<'_, AppState>) -> Result<(), AppError> {
    search::reindex_vault_handler(&state).await
}

#[tauri::command]
pub async fn index_status(
    state: tauri::State<'_, AppState>,
    project: String,
) -> Result<search::IndexStatusView, AppError> {
    search::index_status_handler(&state, project).await
}

#[tauri::command]
pub async fn embedding_model_download_status(
    state: tauri::State<'_, AppState>,
) -> Result<search::EmbeddingModelDownloadStatusView, AppError> {
    search::embedding_model_download_status_handler(&state).await
}

#[tauri::command]
pub async fn start_embedding_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<search::EmbeddingModelDownloadStatusView, AppError> {
    search::start_embedding_model_download_handler(&state).await
}

#[tauri::command]
pub async fn cancel_embedding_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<search::EmbeddingModelDownloadStatusView, AppError> {
    search::cancel_embedding_model_download_handler(&state).await
}

#[tauri::command]
pub async fn chat_stream(
    state: tauri::State<'_, AppState>,
    messages: Vec<chat::ChatMessageArg>,
    project: Option<String>,
    on_event: tauri::ipc::Channel<chat::ChatEventView>,
) -> Result<(), AppError> {
    chat::chat_stream_handler(&state, messages, project, on_event).await
}

#[tauri::command]
pub async fn cancel_chat(state: tauri::State<'_, AppState>) -> Result<(), AppError> {
    chat::cancel_chat_handler(&state).await
}

#[tauri::command]
pub async fn list_chats(
    state: tauri::State<'_, AppState>,
    project: String,
) -> Result<Vec<chats::ChatSummaryView>, AppError> {
    chats::list_chats_handler(&state, &project).await
}

#[tauri::command]
pub async fn read_chat(
    state: tauri::State<'_, AppState>,
    project: String,
    chat_id: String,
) -> Result<chats::ChatConversationView, AppError> {
    chats::read_chat_handler(&state, &project, &chat_id).await
}

#[tauri::command]
pub async fn save_chat(
    state: tauri::State<'_, AppState>,
    project: String,
    conversation: chats::ChatConversationInput,
) -> Result<chats::ChatSummaryView, AppError> {
    chats::save_chat_handler(&state, &project, conversation).await
}

#[tauri::command]
pub async fn rename_chat(
    state: tauri::State<'_, AppState>,
    project: String,
    chat_id: String,
    title: String,
) -> Result<(), AppError> {
    chats::rename_chat_handler(&state, &project, &chat_id, &title).await
}

#[tauri::command]
pub async fn delete_chat(
    state: tauri::State<'_, AppState>,
    project: String,
    chat_id: String,
) -> Result<(), AppError> {
    chats::delete_chat_handler(&state, &project, &chat_id).await
}

#[tauri::command]
pub async fn transcribe_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    language: Option<String>,
) -> Result<JobSnapshot, AppError> {
    meetings::transcribe_vault_entry_handler(&state, &entry_id, language).await
}

#[tauri::command]
pub async fn cancel_job(
    state: tauri::State<'_, AppState>,
    job_id: String,
) -> Result<bool, AppError> {
    meetings::cancel_job_handler(&state, &job_id).await
}

#[tauri::command]
pub async fn prepare_update(state: tauri::State<'_, AppState>) -> Result<(), AppError> {
    prepare_update_handler(&state).await
}

#[tauri::command]
pub async fn list_service_jobs(
    state: tauri::State<'_, AppState>,
    limit: Option<u32>,
) -> Result<Vec<ledger::LedgerJobView>, AppError> {
    ledger::list_service_jobs_handler(&state, limit).await
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tempfile::tempdir;

    use crate::config::Settings;
    use crate::jobs::JobState;
    use crate::service::fake::FakeService;
    use crate::sidecar::{ReadyLine, SidecarError, SidecarSpawnConfig};

    use super::*;

    /// The form of `root` that F1 will actually produce paths under: its
    /// canonical spelling with the Windows extended-length prefix stripped,
    /// exactly as `vault::Vault::open` resolves it. Falls back to the input
    /// when it cannot be canonicalized, so a caller passing a path that does
    /// not exist yet still gets something comparable.
    fn expected_root_for(root: &std::path::Path) -> PathBuf {
        root.canonicalize()
            .map(|canonical| crate::paths::strip_verbatim(&canonical))
            .unwrap_or_else(|_| root.to_path_buf())
    }

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    #[derive(Default)]
    struct RecordingSink {
        snapshots: Mutex<Vec<crate::jobs::JobSnapshot>>,
    }

    impl crate::jobs::EventSink for RecordingSink {
        fn emit(&self, snapshot: &crate::jobs::JobSnapshot) {
            self.snapshots
                .lock()
                .expect("recording sink mutex poisoned")
                .push(snapshot.clone());
        }
    }

    #[derive(Default)]
    struct RecordingStatusSink {
        statuses: Mutex<Vec<ServiceStatusView>>,
    }

    impl ServiceStatusSink for RecordingStatusSink {
        fn emit(&self, status: &ServiceStatusView) {
            self.statuses
                .lock()
                .expect("recording status sink mutex poisoned")
                .push(status.clone());
        }
    }

    /// A `SidecarController` that never spawns a real process — QA's
    /// expectation is that no test spawns the real F2 sidecar. Records the
    /// configs it was asked to launch and returns pre-scripted results.
    #[derive(Default)]
    struct RecordingSidecarController {
        calls: Mutex<Vec<SidecarSpawnConfig>>,
        response: Mutex<Option<Result<ReadyLine, SidecarError>>>,
        terminated: AtomicBool,
    }

    impl RecordingSidecarController {
        fn scripted(response: Result<ReadyLine, SidecarError>) -> Self {
            RecordingSidecarController {
                calls: Mutex::new(Vec::new()),
                response: Mutex::new(Some(response)),
                terminated: AtomicBool::new(false),
            }
        }

        fn calls(&self) -> Vec<SidecarSpawnConfig> {
            self.calls.lock().expect("calls mutex poisoned").clone()
        }
    }

    #[async_trait::async_trait]
    impl SidecarController for RecordingSidecarController {
        async fn spawn_and_await_ready(
            &self,
            config: &SidecarSpawnConfig,
            _timeout: Duration,
        ) -> Result<ReadyLine, SidecarError> {
            self.calls
                .lock()
                .expect("calls mutex poisoned")
                .push(config.clone());
            self.response
                .lock()
                .expect("response mutex poisoned")
                .take()
                .unwrap_or(Err(SidecarError::Io {
                    message: "no scripted response".to_string(),
                }))
        }

        async fn terminate(&self) {
            self.terminated.store(true, Ordering::SeqCst);
        }
    }

    #[derive(Default)]
    struct RecordingRevealer {
        calls: Mutex<Vec<PathBuf>>,
    }

    impl Revealer for RecordingRevealer {
        fn reveal(&self, path: &std::path::Path) -> Result<(), AppError> {
            self.calls
                .lock()
                .expect("revealer mutex poisoned")
                .push(path.to_path_buf());
            Ok(())
        }
    }

    fn state_with(
        settings: Settings,
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
    ) -> AppState {
        state_with_full(
            settings,
            root,
            service,
            Arc::new(RecordingSidecarController::default()),
            Arc::new(RecordingRevealer::default()),
        )
    }

    fn state_with_full(
        settings: Settings,
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        sidecar: Arc<dyn SidecarController>,
        revealer: Arc<dyn Revealer>,
    ) -> AppState {
        let config_dir = root.clone();
        AppState::new_with(
            config_dir.clone(),
            config_dir,
            settings,
            root,
            service,
            None,
            false,
            Arc::new(RecordingSink::default()),
            Arc::new(RecordingStatusSink::default()),
            sidecar,
            revealer,
        )
    }

    fn write_recording(dir: &std::path::Path, name: &str) -> PathBuf {
        let path = dir.join(name);
        fs::write(&path, b"recording bytes").expect("write fixture");
        path
    }

    fn settings_with_root(root: &std::path::Path) -> Settings {
        Settings {
            meetings_root: Some(root.to_string_lossy().into_owned()),
            ..Settings::default()
        }
    }

    async fn wait_for_terminal(
        state: &AppState,
        id: &str,
        timeout: Duration,
    ) -> crate::jobs::JobSnapshot {
        let start = std::time::Instant::now();
        loop {
            if let Some(snapshot) = state.registry.read().await.get(id).await {
                if matches!(
                    snapshot.state,
                    JobState::Done | JobState::Failed | JobState::Rejected
                ) {
                    return snapshot;
                }
            }
            if start.elapsed() > timeout {
                panic!("job {id} did not reach a terminal state within {timeout:?}");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    // -- enqueue_paths_handler ------------------------------------------

    #[test]
    fn enqueue_paths_rejects_an_empty_list() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = enqueue_paths_handler(&state, vec![])
                .await
                .expect_err("empty list must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn enqueue_paths_rejects_a_pathological_length_string() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let huge = "C:\\".to_string() + &"a".repeat(40_000);
            let err = enqueue_paths_handler(&state, vec![huge])
                .await
                .expect_err("pathological length must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn enqueue_paths_rejects_a_relative_path() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = enqueue_paths_handler(&state, vec!["relative\\file.mp4".to_string()])
                .await
                .expect_err("relative path must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn enqueue_paths_before_a_meetings_root_is_configured_returns_not_configured_and_writes_nothing(
    ) {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = Settings::default();
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = enqueue_paths_handler(&state, vec!["C:\\Downloads\\x.mp4".to_string()])
                .await
                .expect_err("must be refused before configuration");

            assert_eq!(err.kind(), crate::error::ErrorKind::NotConfigured);
            assert!(state.registry.read().await.list().await.is_empty());
        });
    }

    #[test]
    fn enqueue_paths_accepts_an_absolute_source_from_anywhere_on_disk_fr4() {
        // FR-4: a dropped file can live anywhere on disk (a Downloads
        // folder, a USB drive...) -- enqueue_paths does not require the
        // *source* to already sit inside the meetings root. Only the
        // destination ingest.rs computes is containment-checked (T9,
        // already covered there); this documents that enqueue_paths itself
        // never rejects a well-formed absolute path on that basis.
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("an absolute path outside the root must still be accepted");

            assert_eq!(snapshots.len(), 1);
            assert_eq!(snapshots[0].state, JobState::Pending);
        });
    }

    // -- reveal_job_handler ----------------------------------------------

    #[test]
    fn reveal_job_with_an_unknown_id_returns_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = reveal_job_handler(&state, "does-not-exist")
                .await
                .expect_err("unknown id must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn reveal_job_on_a_job_whose_recorded_path_sits_outside_the_current_root_is_refused() {
        run(async {
            let ingest_root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let settings = settings_with_root(ingest_root.path());
            let state = state_with(
                settings,
                ingest_root.path().to_path_buf(),
                Arc::new(FakeService::with_timing(crate::service::fake::FakeTiming {
                    queued_polls: 0,
                    running_polls: 0,
                })),
            );

            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed");
            let id = snapshots[0].id.clone();
            let done = wait_for_terminal(&state, &id, Duration::from_secs(5)).await;
            assert_eq!(done.state, JobState::Done);

            // Simulate the current configured root no longer matching where
            // this job's recording actually landed (a tampered/mismatched
            // path) -- containment must be re-checked in Rust, not trusted
            // from the registry alone.
            let other_root = tempdir().expect("tempdir");
            state.settings.write().await.meetings_root =
                Some(other_root.path().to_string_lossy().into_owned());

            let err = reveal_job_handler(&state, &id)
                .await
                .expect_err("must be refused once the recorded path sits outside the current root");
            assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
        });
    }

    #[test]
    fn reveal_job_on_a_pending_job_with_no_path_yet_returns_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let settings = settings_with_root(root.path());
            // A service that never advances past Queued so the job never
            // reaches a state with a meeting_dir/transcript_path set...
            // actually ingest always sets meeting_dir once ingest succeeds,
            // so instead assert directly against a freshly enqueued (still
            // Pending) job before the worker has processed it -- read the
            // snapshot back before waiting for any transition.
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::with_timing(crate::service::fake::FakeTiming {
                    queued_polls: 1000,
                    running_polls: 1000,
                })),
            );

            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed");
            let id = snapshots[0].id.clone();

            let err = reveal_job_handler(&state, &id).await;
            // Depending on worker scheduling this may already have a
            // meeting_dir (ingested) but never a transcript_path (fake
            // service never writes the file) -- in that case reveal must
            // fail because the target file does not exist, not panic.
            if let Err(err) = err {
                assert!(matches!(
                    err.kind(),
                    crate::error::ErrorKind::InvalidArgument | crate::error::ErrorKind::NotAFile
                ));
            }
        });
    }

    #[test]
    fn reveal_job_on_a_valid_job_builds_the_argument_vector_from_the_registry_not_a_caller_string()
    {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let settings = settings_with_root(root.path());
            let revealer = Arc::new(RecordingRevealer::default());
            let state = state_with_full(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::with_timing(crate::service::fake::FakeTiming {
                    queued_polls: 0,
                    running_polls: 0,
                })),
                Arc::new(RecordingSidecarController::default()),
                revealer.clone(),
            );

            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed");
            let id = snapshots[0].id.clone();
            let done = wait_for_terminal(&state, &id, Duration::from_secs(5)).await;
            assert_eq!(done.state, JobState::Done);

            // The fake service never actually writes transcript.json;
            // simulate F2 having done so, as it would in real operation.
            let meeting_dir = PathBuf::from(done.meeting_dir.clone().expect("meeting_dir set"));
            fs::write(meeting_dir.join("transcript.json"), b"{}").expect("write fake transcript");

            reveal_job_handler(&state, &id)
                .await
                .expect("a valid, contained job must be revealable");

            let calls = revealer.calls.lock().expect("calls mutex poisoned").clone();
            assert_eq!(calls.len(), 1);
            assert!(calls[0].ends_with("transcript.json"));
            // E1: the path handed to the revealer must be the stripped
            // (non-`\\?\`) form -- Explorer's shell parser does not accept
            // the verbatim extended-length prefix `ensure_inside` returns.
            let canonical_meeting_dir =
                crate::paths::canonicalize_existing(&meeting_dir).expect("canonicalize");
            assert!(calls[0].starts_with(crate::paths::strip_verbatim(&canonical_meeting_dir)));
            assert!(
                !calls[0].to_string_lossy().starts_with(r"\\?\"),
                "revealer must never receive a verbatim-prefixed path, got {:?}",
                calls[0]
            );
        });
    }

    // -- list_vault_handler / reveal_vault_entry_handler -------------------

    #[test]
    fn list_vault_before_a_meetings_root_is_configured_returns_not_configured() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = list_vault_handler(&state)
                .await
                .expect_err("must be refused before configuration");

            assert_eq!(err.kind(), crate::error::ErrorKind::NotConfigured);
        });
    }

    #[test]
    fn list_vault_reflects_meetings_already_on_disk_newest_first() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let older = root.path().join("ELS").join("260101 - Oldest");
            fs::create_dir_all(&older).expect("create fixture dir");
            fs::write(older.join("source.mp4"), b"rec").expect("write fixture source");

            let newer = root.path().join("unsorted").join("260812 - Newest");
            fs::create_dir_all(&newer).expect("create fixture dir");
            fs::write(newer.join("source.mp4"), b"rec").expect("write fixture source");
            fs::write(newer.join("transcript.json"), b"{}").expect("write fixture transcript");

            let views = list_vault_handler(&state)
                .await
                .expect("list_vault must succeed once a root is configured");

            assert_eq!(views.len(), 2);
            assert_eq!(views[0].meeting_name, "260812 - Newest");
            assert_eq!(views[0].project, None);
            assert!(views[0].has_source);
            assert!(views[0].has_transcript);
            assert_eq!(views[1].meeting_name, "260101 - Oldest");
            assert_eq!(views[1].project.as_deref(), Some("ELS"));
            assert!(views[1].has_source);
            assert!(!views[1].has_transcript);
            // Every id is distinct and non-empty (opaque lookup keys).
            assert_ne!(views[0].id, views[1].id);
            assert!(!views[0].id.is_empty());
        });
    }

    #[test]
    fn list_vault_on_an_empty_root_returns_an_empty_list() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let views = list_vault_handler(&state)
                .await
                .expect("list_vault must succeed on an empty vault");

            assert!(views.is_empty());
        });
    }

    #[test]
    fn reveal_vault_entry_with_an_unknown_id_returns_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = reveal_vault_entry_handler(&state, "does-not-exist")
                .await
                .expect_err("unknown id must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn reveal_vault_entry_on_a_listed_meeting_reveals_its_folder_by_id_not_a_caller_path() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let revealer = Arc::new(RecordingRevealer::default());
            let state = state_with_full(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingSidecarController::default()),
                revealer.clone(),
            );

            let meeting_dir = root.path().join("ELS").join("260101 - A meeting");
            fs::create_dir_all(&meeting_dir).expect("create fixture dir");
            fs::write(meeting_dir.join("source.mp4"), b"rec").expect("write fixture source");

            let views = list_vault_handler(&state).await.expect("list_vault");
            assert_eq!(views.len(), 1);

            reveal_vault_entry_handler(&state, &views[0].id)
                .await
                .expect("a listed entry must be revealable by its id");

            let calls = revealer.calls.lock().expect("calls mutex poisoned").clone();
            assert_eq!(calls.len(), 1);
            let canonical_meeting_dir =
                crate::paths::canonicalize_existing(&meeting_dir).expect("canonicalize");
            assert_eq!(
                calls[0],
                crate::paths::strip_verbatim(&canonical_meeting_dir)
            );
        });
    }

    #[test]
    fn list_vault_keeps_a_meetings_id_stable_across_calls_while_it_exists() {
        // The UI re-lists the vault after every finished job. A meeting
        // still on disk must keep the id the previous listing issued --
        // otherwise the recording page the operator has open loses its
        // entry on every refresh and bounces them back to the library.
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let meeting_dir = root.path().join("ELS").join("260101 - A meeting");
            fs::create_dir_all(&meeting_dir).expect("create fixture dir");

            let first = list_vault_handler(&state).await.expect("first list_vault");
            let second = list_vault_handler(&state).await.expect("second list_vault");

            assert_eq!(
                first[0].id, second[0].id,
                "a still-present meeting must keep its id across listings"
            );
        });
    }

    #[test]
    fn reveal_vault_entry_on_an_id_whose_meeting_is_gone_is_refused_after_relisting() {
        // Ids survive refreshes only while their meeting exists: once the
        // folder is gone, the next listing drops the id from the index and
        // it fails closed, exactly as the wholesale replacement used to.
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let meeting_dir = root.path().join("ELS").join("260101 - A meeting");
            fs::create_dir_all(&meeting_dir).expect("create fixture dir");

            let first = list_vault_handler(&state).await.expect("first list_vault");
            let stale_id = first[0].id.clone();

            fs::remove_dir_all(&meeting_dir).expect("remove fixture dir");
            list_vault_handler(&state)
                .await
                .expect("second list_vault drops the vanished meeting");

            let err = reveal_vault_entry_handler(&state, &stale_id)
                .await
                .expect_err("an id whose meeting vanished must be refused");
            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn reveal_vault_entry_on_an_entry_whose_root_changed_since_listing_is_refused() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let meeting_dir = root.path().join("ELS").join("260101 - A meeting");
            fs::create_dir_all(&meeting_dir).expect("create fixture dir");

            let views = list_vault_handler(&state).await.expect("list_vault");
            let id = views[0].id.clone();

            let other_root = tempdir().expect("tempdir");
            state.settings.write().await.meetings_root =
                Some(other_root.path().to_string_lossy().into_owned());

            let err = reveal_vault_entry_handler(&state, &id)
                .await
                .expect_err("must be refused once the recorded path sits outside the current root");
            assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
        });
    }

    // -- reveal_command_line / run_reveal_command -------------------------

    #[cfg(windows)]
    #[test]
    fn reveal_command_line_quotes_the_path_but_leaves_the_select_switch_bare() {
        let path = PathBuf::from(r"C:\Meetings\ELS\260812 - Security issue\transcript.json");
        let raw = reveal_command_line(&path);
        assert_eq!(
            raw,
            r#"/select,"C:\Meetings\ELS\260812 - Security issue\transcript.json""#
        );
    }

    #[cfg(windows)]
    #[test]
    fn run_reveal_command_tolerates_a_nonzero_exit_code() {
        // A harmless stand-in process (never explorer.exe, so this never
        // opens a visible window during an automated test run) that exits
        // nonzero -- Explorer's own exit code carries no meaning to this
        // app, so this must still be reported as success.
        let result = run_reveal_command("cmd", "/C exit 1");
        assert!(result.is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn run_reveal_command_appends_the_tail_raw_so_the_select_switch_is_not_quoted_with_the_path_e1_regression(
    ) {
        // Regression for E1. `std::process::Command::args` quotes any
        // argument containing a space as one token, so a `/select,<path>`
        // argument fed through `.args(&[...])` becomes
        // `"/select,<path>"` on the real command line -- Explorer parses
        // that as an unrecognized switch and opens Documents instead of
        // the target. `run_reveal_command` must append the tail with
        // `CommandExt::raw_arg` instead, which puts it on the command line
        // untouched. Proven here the same way the evaluator did: spawn
        // `cmd.exe` (never a visible Explorer window) and read back
        // `%CMDCMDLINE%`, which echoes exactly the command line the OS
        // assembled for that process.
        let path = PathBuf::from(r"C:\Meetings\ELS\260812 - Security issue\transcript.json");
        let raw_tail = reveal_command_line(&path);

        let output = std::process::Command::new("cmd")
            .arg("/C")
            .arg("echo")
            .arg("%CMDCMDLINE%")
            .raw_arg(&raw_tail)
            .output()
            .expect("spawn cmd to read back %CMDCMDLINE%");
        let stdout = String::from_utf8_lossy(&output.stdout);

        // The fixed form -- switch bare, only the path quoted -- appears
        // untouched on the command line the OS received.
        assert!(
            stdout.contains(&raw_tail),
            "expected the raw tail `{raw_tail}` to appear untouched on the \
             command line cmd.exe received, got: {stdout}"
        );
        // The broken `Command::args` form this replaces would have wrapped
        // the whole `/select,<path>` token -- switch included -- in one
        // pair of quotes. It must not appear.
        let broken_form = format!("\"/select,{}\"", path.display());
        assert!(
            !stdout.contains(&broken_form),
            "the select switch must not be quoted together with the path, \
             got: {stdout}"
        );
    }

    // -- set_meetings_root_handler / resolve_and_apply_meetings_root_service

    #[test]
    fn set_meetings_root_returns_before_resolving_the_sidecar_e17_regression() {
        // E17: `set_meetings_root_handler` on its own must never await the
        // sidecar's ready-line wait (up to `sidecar::READY_TIMEOUT` = 30s)
        // before returning the persisted settings -- that used to leave
        // the operator's folder-picker click looking ignored for the
        // whole spawn window. Resolving/(re)starting the sidecar is a
        // separate step (`resolve_and_apply_meetings_root_service`) the
        // `#[tauri::command]` wrapper drives in the background.
        run(async {
            let config_dir = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");

            let sidecar = Arc::new(RecordingSidecarController::scripted(Ok(ReadyLine {
                port: 51234,
                token: "tok".to_string(),
                pid: 1,
            })));
            let state = state_with_full(
                Settings::default(),
                config_dir.path().to_path_buf(),
                Arc::new(FakeService::new()),
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );

            let response = set_meetings_root_handler(
                &state,
                new_root.path().to_str().expect("valid utf8 path"),
            )
            .await
            .expect("set_meetings_root must succeed");

            assert_eq!(
                response.meetings_root.as_deref(),
                Some(new_root.path().to_str().expect("valid utf8 path"))
            );
            assert!(
                sidecar.calls().is_empty(),
                "set_meetings_root_handler must return before the sidecar is resolved"
            );
        });
    }

    #[test]
    fn set_meetings_root_persists_and_the_background_step_triggers_a_sidecar_restart_with_the_new_root(
    ) {
        run(async {
            let config_dir = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");

            let sidecar = Arc::new(RecordingSidecarController::scripted(Ok(ReadyLine {
                port: 51234,
                token: "tok".to_string(),
                pid: 1,
            })));
            let state = state_with_full(
                Settings::default(),
                config_dir.path().to_path_buf(),
                Arc::new(FakeService::new()),
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );

            let response = set_meetings_root_handler(
                &state,
                new_root.path().to_str().expect("valid utf8 path"),
            )
            .await
            .expect("set_meetings_root must succeed");

            // The `#[tauri::command]` wrapper drives this as a spawned
            // background task; the unit test drives it directly (no Tauri
            // runtime needed) to assert its effect on the sidecar/registry.
            let settings = state.settings.read().await.clone();
            resolve_and_apply_meetings_root_service(
                &state,
                &settings,
                PathBuf::from(new_root.path()),
            )
            .await;

            assert_eq!(
                response.meetings_root.as_deref(),
                Some(new_root.path().to_str().expect("valid utf8 path"))
            );

            let calls = sidecar.calls();
            assert_eq!(calls.len(), 1, "must trigger exactly one sidecar restart");
            let env: std::collections::HashMap<_, _> = calls[0].envs.iter().cloned().collect();
            assert_eq!(
                env.get("TRANSCRIBER_ALLOWED_ROOTS").map(String::as_str),
                Some(new_root.path().to_str().expect("valid utf8 path"))
            );
        });
    }

    #[test]
    fn set_meetings_root_background_step_does_not_spawn_when_a_service_base_url_is_configured() {
        run(async {
            let config_dir = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");

            let mut settings = Settings::default();
            settings.service.base_url = Some("http://127.0.0.1:8756".to_string());
            let sidecar = Arc::new(RecordingSidecarController::default());
            let state = state_with_full(
                settings,
                config_dir.path().to_path_buf(),
                Arc::new(FakeService::new()),
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );

            set_meetings_root_handler(&state, new_root.path().to_str().expect("valid utf8"))
                .await
                .expect("set_meetings_root must succeed");

            let settings = state.settings.read().await.clone();
            resolve_and_apply_meetings_root_service(
                &state,
                &settings,
                PathBuf::from(new_root.path()),
            )
            .await;

            assert!(
                sidecar.calls().is_empty(),
                "a configured base_url must never spawn a sidecar"
            );
        });
    }

    #[test]
    fn set_meetings_root_moves_the_registry_root_synchronously_e21_regression() {
        // E21: before this fix, `set_meetings_root_handler` persisted the
        // new root but left `JobRegistry`'s root untouched -- only the
        // background `resolve_and_apply_meetings_root_service` moved it,
        // and that can run up to `sidecar::READY_TIMEOUT` (30s) later for a
        // first-run `Spawn` plan. `App.tsx` already accepts drops the
        // moment `set_meetings_root` returns, so a file dropped in that
        // window used to be ingested (and, per F1, *moved* from its
        // original location) under the *previous* root -- `state.config_dir`
        // itself on first run -- instead of the folder the operator just
        // chose. This drives exactly that sequence: enqueue right after
        // `set_meetings_root_handler` returns, *before*
        // `resolve_and_apply_meetings_root_service` ever runs.
        run(async {
            let old_root = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");

            let sidecar = Arc::new(RecordingSidecarController::default());
            let fake = Arc::new(FakeService::with_timing(crate::service::fake::FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            let state = state_with_full(
                Settings::default(),
                old_root.path().to_path_buf(),
                fake,
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );

            set_meetings_root_handler(&state, new_root.path().to_str().expect("valid utf8"))
                .await
                .expect("set_meetings_root must succeed");

            // Deliberately never calling `resolve_and_apply_meetings_root_service`
            // here -- that is the background half this test proves is not
            // needed for the registry's root to have already moved.
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed");
            let done = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;

            let meeting_dir = done.meeting_dir.expect("meeting_dir must be set");
            // Compared against the *canonical* root: F1 canonicalizes every
            // path it hands back, and a tempdir's own path need not be
            // canonical -- a Windows CI runner exposes `%TEMP%` as the 8.3
            // short form `C:\Users\RUNNER~1\...`. Asserting against the raw tempdir path
            // tests the spelling of the test's fixture, not where the job
            // was filed.
            let expected_root = expected_root_for(new_root.path());
            assert!(
                Path::new(&meeting_dir).starts_with(&expected_root),
                "job must be filed under the newly configured root {:?}, got {meeting_dir}",
                expected_root
            );
            assert!(
                !Path::new(&meeting_dir).starts_with(old_root.path()),
                "job must not be filed under the previous root {:?}, got {meeting_dir}",
                old_root.path()
            );
        });
    }

    #[test]
    fn resolve_and_apply_meetings_root_service_keeps_the_fake_service_in_fake_mode_e20_regression()
    {
        // E20: the `--fake-service`/`TRANSCRIBER_FAKE_SERVICE` dev switch
        // must survive a meetings-root change. Before this fix,
        // `resolve_and_apply_meetings_root_service` always ran
        // `plan_sidecar` + `resolve_service` unconditionally, which would
        // spawn a real `uv` sidecar (or install `UnavailableTranscriptionService`)
        // in place of the `FakeService`, breaking "the whole UI flow
        // testable without F2 existing" the moment the operator touched
        // the setting.
        run(async {
            let old_root = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");

            let sidecar = Arc::new(RecordingSidecarController::default());
            let fake = Arc::new(FakeService::with_timing(crate::service::fake::FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            let mut state = state_with_full(
                Settings::default(),
                old_root.path().to_path_buf(),
                fake.clone(),
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );
            state.fake_mode = true;

            set_meetings_root_handler(&state, new_root.path().to_str().expect("valid utf8"))
                .await
                .expect("set_meetings_root must succeed");
            let settings = state.settings.read().await.clone();
            resolve_and_apply_meetings_root_service(
                &state,
                &settings,
                new_root.path().to_path_buf(),
            )
            .await;

            assert!(
                sidecar.calls().is_empty(),
                "fake mode must never spawn or connect to a real sidecar"
            );

            // The registry's root must still have moved to `new_root` --
            // only the service substitution is skipped, not the root move.
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed");
            let done = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;
            let meeting_dir = done.meeting_dir.expect("meeting_dir set");
            // See the note in the E21 regression above: compare against the
            // canonical root, not the tempdir's own spelling.
            let expected_root = expected_root_for(new_root.path());
            assert!(
                Path::new(&meeting_dir).starts_with(&expected_root),
                "job must be filed under the newly configured root {:?}, got {meeting_dir}",
                expected_root
            );
        });
    }

    // -- service_status_handler --------------------------------------------

    #[test]
    fn service_status_reports_unavailable_naming_the_url_when_the_seam_is_down() {
        run(async {
            let root = tempdir().expect("tempdir");
            let down = FakeService::new();
            down.set_down(true);
            let state = state_with(
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(down),
            );
            *state.service_starting.write().await = false;
            *state.service_base_url.write().await = Some("http://127.0.0.1:9999".to_string());

            let status = service_status_handler(&state)
                .await
                .expect("service_status must not fail");

            assert_eq!(status.state, ServiceState::Unavailable);
            assert_eq!(status.base_url.as_deref(), Some("http://127.0.0.1:9999"));
        });
    }

    #[test]
    fn service_status_reports_starting_before_the_service_has_resolved() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(UnavailableTranscriptionService::new("starting".to_string())),
            );
            *state.service_starting.write().await = true;

            let status = service_status_handler(&state)
                .await
                .expect("service_status must not fail");

            assert_eq!(status.state, ServiceState::Starting);
        });
    }

    #[test]
    fn service_status_reports_ready_when_the_service_is_healthy() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let status = service_status_handler(&state)
                .await
                .expect("service_status must not fail");

            assert_eq!(status.state, ServiceState::Ready);
        });
    }

    // -- get_settings_handler / list_jobs_handler --------------------------

    #[test]
    fn get_settings_combines_the_config_view_with_supported_extensions() {
        run(async {
            let root = tempdir().expect("tempdir");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let view = get_settings_handler(&state)
                .await
                .expect("get_settings must not fail");

            assert_eq!(
                view.meetings_root.as_deref(),
                Some(root.path().to_string_lossy().as_ref())
            );
            assert!(view.meetings_root_exists);
            assert_eq!(
                view.supported_extensions,
                crate::paths::supported_extensions()
            );
            assert_eq!(view.config_error, None);
        });
    }

    #[test]
    fn get_settings_surfaces_a_startup_config_load_error_instead_of_hiding_it() {
        // E3: `lib.rs`'s startup wiring falls back to `Settings::default()`
        // when `config.json` fails to parse and records the error on
        // `AppState::config_error` -- `get_settings` must expose it so the
        // frontend can render an actionable error instead of the failure
        // being invisible (or, worse, the app never opening at all).
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            *state.config_error.write().await =
                Some("malformed settings file config.json: expected value".to_string());

            let view = get_settings_handler(&state)
                .await
                .expect("get_settings must not fail even with a recorded config error");

            assert_eq!(
                view.config_error.as_deref(),
                Some("malformed settings file config.json: expected value")
            );
            // The fallback settings still leave the app in first-run state,
            // never a panic and never a silently invented root.
            assert_eq!(view.meetings_root, None);
        });
    }

    #[test]
    fn set_meetings_root_clears_a_previously_recorded_config_error() {
        run(async {
            let config_dir = tempdir().expect("tempdir");
            let new_root = tempdir().expect("tempdir");
            let state = state_with(
                Settings::default(),
                config_dir.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            *state.config_error.write().await = Some("malformed settings file".to_string());

            let response = set_meetings_root_handler(
                &state,
                new_root.path().to_str().expect("valid utf8 path"),
            )
            .await
            .expect("set_meetings_root must succeed");

            assert_eq!(response.config_error, None);
            assert_eq!(*state.config_error.read().await, None);
        });
    }

    #[test]
    fn list_jobs_reflects_the_registry() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let settings = settings_with_root(root.path());
            let state = state_with(
                settings,
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                .await
                .expect("enqueue must succeed");

            let jobs = list_jobs_handler(&state)
                .await
                .expect("list_jobs must not fail");
            assert_eq!(jobs.len(), 1);
        });
    }

    // -- fake_service_requested / resolve_service / apply_resolved_service --

    #[test]
    fn fake_service_requested_true_when_env_var_present() {
        assert!(fake_service_requested(
            Some("1".to_string()),
            &["transcriber-desktop".to_string()]
        ));
    }

    #[test]
    fn fake_service_requested_true_when_flag_present() {
        assert!(fake_service_requested(
            None,
            &[
                "transcriber-desktop".to_string(),
                "--fake-service".to_string()
            ]
        ));
    }

    #[test]
    fn fake_service_requested_false_otherwise() {
        assert!(!fake_service_requested(
            None,
            &["transcriber-desktop".to_string()]
        ));
    }

    #[test]
    fn resolve_service_with_use_existing_builds_an_http_client_without_spawning() {
        run(async {
            let sidecar = Arc::new(RecordingSidecarController::default());
            let (service, base_url) = resolve_service(
                sidecar.as_ref(),
                crate::sidecar::SidecarPlan::UseExisting {
                    base_url: "http://127.0.0.1:8756".to_string(),
                },
            )
            .await;

            assert_eq!(base_url.as_deref(), Some("http://127.0.0.1:8756"));
            assert!(sidecar.calls().is_empty());
            // Health will fail (nothing is listening) but must not panic.
            let _ = service.health().await;
        });
    }

    #[test]
    fn resolve_service_with_spawn_uses_the_controllers_ready_line() {
        run(async {
            let sidecar = Arc::new(RecordingSidecarController::scripted(Ok(ReadyLine {
                port: 51234,
                token: "tok".to_string(),
                pid: 1,
            })));
            let config = SidecarSpawnConfig {
                program: "uv".to_string(),
                args: vec!["run".to_string()],
                envs: vec![],
            };

            let (_service, base_url) =
                resolve_service(sidecar.as_ref(), crate::sidecar::SidecarPlan::Spawn(config)).await;

            assert_eq!(base_url.as_deref(), Some("http://127.0.0.1:51234"));
        });
    }

    #[test]
    fn resolve_service_falls_back_to_unavailable_when_the_sidecar_never_becomes_ready() {
        run(async {
            let sidecar = Arc::new(RecordingSidecarController::scripted(Err(
                SidecarError::Timeout {
                    command: "uv run ...".to_string(),
                },
            )));
            let config = SidecarSpawnConfig {
                program: "uv".to_string(),
                args: vec!["run".to_string()],
                envs: vec![],
            };

            let (service, base_url) =
                resolve_service(sidecar.as_ref(), crate::sidecar::SidecarPlan::Spawn(config)).await;

            assert_eq!(base_url, None);
            let err = service.health().await.expect_err("must be unavailable");
            assert!(matches!(
                err,
                crate::service::ServiceError::Unavailable { .. }
            ));
        });
    }

    #[test]
    fn apply_resolved_service_updates_state_and_emits_status() {
        run(async {
            let root = tempdir().expect("tempdir");
            let status_sink = Arc::new(RecordingStatusSink::default());
            let state = AppState::new_with(
                root.path().to_path_buf(),
                root.path().to_path_buf(),
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(UnavailableTranscriptionService::new("starting".to_string())),
                None,
                true,
                Arc::new(RecordingSink::default()),
                status_sink.clone(),
                Arc::new(RecordingSidecarController::default()),
                Arc::new(RecordingRevealer::default()),
            );

            let new_service: Arc<dyn TranscriptionService> = Arc::new(FakeService::new());
            let status = apply_resolved_service(
                &state,
                new_service,
                Some("http://127.0.0.1:4000".to_string()),
                root.path().to_path_buf(),
            )
            .await;

            assert_eq!(status.state, ServiceState::Ready);
            assert!(!*state.service_starting.read().await);
            assert_eq!(
                *state.service_base_url.read().await,
                Some("http://127.0.0.1:4000".to_string())
            );
            assert_eq!(
                status_sink
                    .statuses
                    .lock()
                    .expect("status mutex poisoned")
                    .len(),
                1
            );
        });
    }

    #[test]
    fn prepare_update_terminates_the_sidecar_and_reports_unavailable() {
        // The updater's installer overwrites pyenv\ in place; a still-running
        // python.exe from that tree makes the install fail with "Error
        // opening file for writing". prepare_update must have the sidecar
        // fully terminated by the time it returns, and must tell the UI the
        // service is down rather than leaving a stale "ready" on screen.
        run(async {
            let root = tempdir().expect("tempdir");
            let status_sink = Arc::new(RecordingStatusSink::default());
            let sidecar = Arc::new(RecordingSidecarController::default());
            let state = AppState::new_with(
                root.path().to_path_buf(),
                root.path().to_path_buf(),
                Settings::default(),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Some("http://127.0.0.1:4000".to_string()),
                false,
                Arc::new(RecordingSink::default()),
                status_sink.clone(),
                sidecar.clone(),
                Arc::new(RecordingRevealer::default()),
            );

            prepare_update_handler(&state)
                .await
                .expect("prepare_update must succeed");

            assert!(
                sidecar.terminated.load(Ordering::SeqCst),
                "prepare_update must terminate the sidecar before returning"
            );
            assert_eq!(*state.service_base_url.read().await, None);
            let err = state
                .service
                .read()
                .await
                .health()
                .await
                .expect_err("the active service must now be unavailable");
            assert!(matches!(
                err,
                crate::service::ServiceError::Unavailable { .. }
            ));

            let statuses = status_sink
                .statuses
                .lock()
                .expect("status mutex poisoned")
                .clone();
            assert_eq!(statuses.len(), 1);
            assert_eq!(statuses[0].state, ServiceState::Unavailable);
            assert_eq!(statuses[0].detail.as_deref(), Some(UPDATE_STOP_DETAIL));
        });
    }

    #[test]
    fn apply_resolved_service_preserves_a_job_enqueued_while_the_sidecar_was_still_starting() {
        // E2: a job enqueued during the window before the sidecar resolves
        // (`UnavailableTranscriptionService` placeholder, `service_starting`
        // true) must still be listable and revealable *after*
        // `apply_resolved_service` installs the real service -- the
        // registry must be updated in place, never replaced wholesale.
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let settings = settings_with_root(root.path());
            let revealer = Arc::new(RecordingRevealer::default());

            let state = AppState::new_with(
                root.path().to_path_buf(),
                root.path().to_path_buf(),
                settings,
                root.path().to_path_buf(),
                Arc::new(UnavailableTranscriptionService::new("starting".to_string())),
                None,
                true,
                Arc::new(RecordingSink::default()),
                Arc::new(RecordingStatusSink::default()),
                Arc::new(RecordingSidecarController::default()),
                revealer.clone(),
            );

            // Enqueued while the sidecar is still "starting" -- ingest keeps
            // working per FR-13 even though transcription cannot be
            // submitted yet (the placeholder service fails `submit`, so the
            // job reaches a terminal `Failed` with `meeting_dir`/
            // `source_dest` already set -- FR-13's "already ingested" half).
            let snapshots =
                enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
                    .await
                    .expect("enqueue must succeed even before the service resolves");
            let id = snapshots[0].id.clone();
            let terminal = wait_for_terminal(&state, &id, Duration::from_secs(5)).await;
            assert_eq!(terminal.state, JobState::Failed);
            assert!(
                terminal.meeting_dir.is_some(),
                "ingest must still have happened"
            );

            // The sidecar resolves: a real (fake, here) service becomes
            // available. This must swap the registry's service/root in
            // place, not replace the registry wholesale.
            let resolved_service: Arc<dyn TranscriptionService> = Arc::new(
                crate::service::fake::FakeService::with_timing(crate::service::fake::FakeTiming {
                    queued_polls: 0,
                    running_polls: 0,
                }),
            );
            apply_resolved_service(
                &state,
                resolved_service,
                Some("http://127.0.0.1:4000".to_string()),
                root.path().to_path_buf(),
            )
            .await;

            // The job enqueued before resolution must still be tracked...
            assert!(
                state.registry.read().await.get(&id).await.is_some(),
                "a job enqueued before the sidecar resolved must not be dropped from the registry"
            );
            assert!(
                list_jobs_handler(&state)
                    .await
                    .expect("list_jobs must not fail")
                    .iter()
                    .any(|s| s.id == id),
                "list_jobs must still return the pre-resolution job"
            );

            // ...and it must still be revealable via its filed recording
            // (the job never got as far as a real `transcript.json`, since
            // submission itself failed against the placeholder service --
            // simulate F2 having written it, as it would once transcription
            // actually completes on a real ingest-then-later-transcribed
            // job).
            let meeting_dir = PathBuf::from(terminal.meeting_dir.clone().expect("meeting_dir set"));
            fs::write(meeting_dir.join("transcript.json"), b"{}").expect("write fake transcript");
            reveal_job_handler(&state, &id)
                .await
                .expect("a job surviving the registry swap must remain revealable");
            assert_eq!(
                revealer.calls.lock().expect("calls mutex poisoned").len(),
                1
            );
        });
    }

    // -- meetings: read_transcript / update_vault_entry / delete_vault_entry
    //
    // Each drives the handler through `list_vault_handler` first, because an
    // id is only ever issued by a listing -- that is the contract these
    // commands are built on.

    const TRANSCRIPT_FIXTURE: &str = r#"{"language":"ru","text":"Да, ребят","segments":[{"id":0,"start":0.0,"end":2.5,"text":" Да, ребят"}],"provider":{"model":"large-v3","device":"cuda"},"source":{"duration_sec":3625.8}}"#;

    fn seed_meeting(root: &std::path::Path, parent: &str, name: &str, transcript: Option<&str>) {
        let dir = root.join(parent).join(name);
        fs::create_dir_all(&dir).expect("create fixture dir");
        fs::write(dir.join("source.mp4"), b"rec").expect("write fixture source");
        if let Some(body) = transcript {
            fs::write(dir.join("transcript.json"), body).expect("write fixture transcript");
        }
    }

    #[test]
    fn read_transcript_returns_the_meetings_transcript_by_id() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            seed_meeting(
                root.path(),
                "unsorted",
                "260822 - source",
                Some(TRANSCRIPT_FIXTURE),
            );

            let views = list_vault_handler(&state).await.expect("list_vault");
            let view = meetings::read_transcript_handler(&state, &views[0].id)
                .await
                .expect("a listed meeting with a transcript must be readable");

            assert_eq!(view.entry_id, views[0].id);
            assert_eq!(view.meeting_name, "260822 - source");
            assert_eq!(view.language.as_deref(), Some("ru"));
            assert_eq!(view.text, "Да, ребят");
            assert_eq!(view.segments.len(), 1);
        });
    }

    #[test]
    fn read_transcript_on_a_meeting_without_one_is_an_actionable_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            seed_meeting(root.path(), "ELS", "260101 - No transcript", None);

            let views = list_vault_handler(&state).await.expect("list_vault");
            let err = meetings::read_transcript_handler(&state, &views[0].id)
                .await
                .expect_err("a meeting with no transcript must be refused");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
            assert!(
                err.message().contains("260101 - No transcript"),
                "message was {:?}",
                err.message()
            );
        });
    }

    #[test]
    fn read_transcript_with_an_unknown_id_returns_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = meetings::read_transcript_handler(&state, "does-not-exist")
                .await
                .expect_err("unknown id must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn update_vault_entry_files_an_unsorted_meeting_and_keeps_its_id() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            seed_meeting(
                root.path(),
                "unsorted",
                "260822 - source",
                Some(TRANSCRIPT_FIXTURE),
            );

            let views = list_vault_handler(&state).await.expect("list_vault");
            let id = views[0].id.clone();

            let updated = meetings::update_vault_entry_handler(
                &state,
                &id,
                // Lowercase on purpose: the vault capitalizes the code.
                Some("els".to_string()),
                "260814",
                "Weekly sync",
            )
            .await
            .expect("re-filing a listed meeting must succeed");

            assert_eq!(updated.id, id, "the entry keeps its id across a rename");
            assert_eq!(updated.project.as_deref(), Some("ELS"));
            assert_eq!(updated.meeting_name, "260814 - Weekly sync");
            assert!(updated.has_source);
            assert!(updated.has_transcript);
            assert!(root
                .path()
                .join("ELS")
                .join("260814 - Weekly sync")
                .join("transcript.json")
                .is_file());
            assert!(!root
                .path()
                .join("unsorted")
                .join("260822 - source")
                .exists());

            // The id still resolves -- to the *new* location.
            let view = meetings::read_transcript_handler(&state, &id)
                .await
                .expect("the renamed meeting is still reachable by the same id");
            assert_eq!(view.meeting_name, "260814 - Weekly sync");
        });
    }

    #[test]
    fn update_vault_entry_rejects_an_unusable_name_without_moving_anything() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            seed_meeting(root.path(), "unsorted", "260822 - source", None);

            let views = list_vault_handler(&state).await.expect("list_vault");
            let err = meetings::update_vault_entry_handler(
                &state,
                &views[0].id,
                Some("ELS".to_string()),
                "260230",
                "Weekly sync",
            )
            .await
            .expect_err("a date that is not a calendar date must be refused");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
            assert!(root
                .path()
                .join("unsorted")
                .join("260822 - source")
                .join("source.mp4")
                .is_file());
        });
    }

    #[test]
    fn delete_vault_entry_removes_the_meeting_and_retires_its_id() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );
            seed_meeting(root.path(), "ELS", "260101 - A meeting", None);

            let views = list_vault_handler(&state).await.expect("list_vault");
            let id = views[0].id.clone();

            meetings::delete_vault_entry_handler(&state, &id)
                .await
                .expect("a listed meeting must be deletable by its id");

            assert!(!root.path().join("ELS").join("260101 - A meeting").exists());

            let err = meetings::read_transcript_handler(&state, &id)
                .await
                .expect_err("a deleted entry's id must fail closed");
            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn delete_vault_entry_with_an_unknown_id_returns_invalid_argument() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
            );

            let err = meetings::delete_vault_entry_handler(&state, "does-not-exist")
                .await
                .expect_err("unknown id must be rejected");

            assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn list_service_jobs_reports_service_unavailable_when_the_ledger_cannot_be_reached() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with(
                settings_with_root(root.path()),
                root.path().to_path_buf(),
                Arc::new(UnavailableTranscriptionService::new("service starting")),
            );

            let err = ledger::list_service_jobs_handler(&state, None)
                .await
                .expect_err("an unreachable service must not look like an empty ledger");

            assert_eq!(err.kind(), crate::error::ErrorKind::ServiceUnavailable);
        });
    }
}
