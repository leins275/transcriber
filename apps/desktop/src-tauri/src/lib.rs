//! Transcriber desktop app — Rust privileged process.
//!
//! This file declares the crate's module tree up front so wave-2 tasks
//! never need to add a `mod` declaration to a file another task is also
//! touching. Each module starts as an empty stub and is filled in by its
//! owning task (see `specs/tauri-desktop-app/plan.md`).

/// `AppError`/`ErrorKind` taxonomy, serialized to the UI (frozen here; do
/// not edit outside this task).
pub mod error;

/// Resolves the application folder and every path that hangs off it: the
/// bundled Python runtime, the model directory, and `models\`/`logs\`/
/// `data\` (T9, FR-8, FR-11-as-superseded).
pub mod app_paths;

/// `config.json` load/save/validate (FR-16..18).
pub mod config;

/// Path canonicalization and containment (FR-11, FR-15).
pub mod paths;

/// Wrapper over F1's vault crate, off the UI thread (FR-9, FR-10).
pub mod ingest;

/// `TranscriptionService` trait, job model, HTTP binding and in-memory fake
/// (FR-12).
pub mod service;

/// Spawns F2, parses its ready line, kills it on exit.
pub mod sidecar;

/// Job registry, sequential pipeline, poll loop (FR-8, FR-14, NFR-4).
pub mod jobs;

/// `#[tauri::command]` handlers — the only IPC surface.
pub mod commands;

use std::path::PathBuf;
use std::sync::Arc;

use tauri::{Emitter, Manager};

use service::fake::FakeService;
use service::TranscriptionService;

/// Pushes every job transition to the frontend as `jobs://updated`
/// (`jobs::EventSink`) and every service reachability change as
/// `service://status` (`commands::ServiceStatusSink`) — the only two
/// events this app emits (plan.md's IPC contract).
struct TauriEventSink(tauri::AppHandle);

impl jobs::EventSink for TauriEventSink {
    fn emit(&self, snapshot: &jobs::JobSnapshot) {
        if let Err(err) = self.0.emit("jobs://updated", snapshot) {
            eprintln!("[transcriber] failed to emit jobs://updated: {err}");
        }
    }
}

impl commands::ServiceStatusSink for TauriEventSink {
    fn emit(&self, status: &commands::ServiceStatusView) {
        if let Err(err) = self.0.emit("service://status", status) {
            eprintln!("[transcriber] failed to emit service://status: {err}");
        }
    }
}

/// Resolves `app_config_dir()`, loads settings, decides which
/// `TranscriptionService` to start with, and manages the resulting
/// [`commands::AppState`]. Spawning F2 (when applicable) is kicked off as a
/// background task so the window paints before the sidecar is ready
/// (NFR-3) -- `commands::apply_resolved_service` installs the real service
/// once (or if) it comes up.
fn setup_app_state(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();
    let config_dir = app.path().app_config_dir()?;
    // T9 / FR-8, FR-11-as-superseded: the application folder is the
    // installed app's own directory (Q4-A's `%LOCALAPPDATA%\Programs\
    // Transcriber\`), not `%APPDATA%\<identifier>\` -- that is where the
    // bundled Python runtime, `models\`/`logs\`/`data\` and the sidecar's
    // resolved model path all actually live. `config_dir` (above) stays the
    // one place `config.json` itself is read from/written to, per the
    // Configuration contract's "one config.json in %APPDATA%" split.
    let app_dir = app_paths::app_dir();
    // On Windows the NSIS post-install hook creates the app folder's
    // `models\`/`logs\`/`data\` skeleton; on macOS nothing does (the app
    // folder is `~/Library/Application Support/Transcriber`, never touched
    // by the .dmg), so make sure the folder itself exists. Best-effort and
    // idempotent on both platforms.
    let _ = std::fs::create_dir_all(&app_dir);
    // E3 / NFR-6: a malformed or unreadable `config.json` must never keep
    // the window from opening -- fall back to first-run defaults and carry
    // the error forward so `get_settings` can render it as an actionable
    // error instead of the app silently discarding the operator's settings
    // (or panicking before any window exists at all).
    let (settings, config_load_error) = match config::load(&config_dir) {
        Ok(settings) => (settings, None),
        Err(err) => {
            eprintln!("[transcriber] failed to load config.json, falling back to first-run defaults: {err}");
            (config::Settings::default(), Some(err.to_string()))
        }
    };
    let root = settings
        .meetings_root
        .clone()
        .map(PathBuf::from)
        .unwrap_or_else(|| config_dir.clone());

    let sink_impl = Arc::new(TauriEventSink(handle.clone()));
    let sink: Arc<dyn jobs::EventSink> = sink_impl.clone();
    let status_sink: Arc<dyn commands::ServiceStatusSink> = sink_impl;

    let fake_requested = commands::fake_service_requested(
        std::env::var("TRANSCRIBER_FAKE_SERVICE").ok(),
        &std::env::args().collect::<Vec<_>>(),
    );

    let initial_service: Arc<dyn TranscriptionService> = if fake_requested {
        Arc::new(FakeService::new())
    } else {
        Arc::new(commands::UnavailableTranscriptionService::new(
            "service starting".to_string(),
        ))
    };

    // `AppState::new` builds a `JobRegistry`, which spawns its worker task
    // via a bare `tokio::spawn` (jobs.rs, not owned by this task) --that
    // requires an entered Tokio runtime context, which a Tauri `setup()`
    // hook does not have on its own. `async_runtime::block_on` enters
    // Tauri's own Tokio runtime for the duration of this (synchronous)
    // construction, which is enough for the nested `tokio::spawn` to find
    // a runtime.
    let mut state = tauri::async_runtime::block_on(async {
        commands::AppState::new(
            config_dir,
            app_dir,
            settings,
            root,
            initial_service,
            None,
            !fake_requested,
            sink,
            status_sink,
        )
    });
    // `state` is not yet shared (that happens at `app.manage` below), so a
    // direct field replacement needs no lock.
    if let Some(message) = config_load_error {
        state.config_error = tokio::sync::RwLock::new(Some(message));
    }
    // E20: record whether this session was started in fake-service mode so
    // `commands::resolve_and_apply_meetings_root_service` can keep it that
    // way across a later `set_meetings_root` instead of silently spawning
    // a real sidecar the first time the operator changes the setting.
    state.fake_mode = fake_requested;
    app.manage(state);

    // A configured `--fake-service`/`TRANSCRIBER_FAKE_SERVICE` dev switch
    // already resolved above -- nothing more to start in the background.
    if !fake_requested {
        tauri::async_runtime::spawn(async move {
            let state = handle.state::<commands::AppState>();
            let settings_snapshot = state.settings.read().await.clone();
            let config_path = config::config_path(&state.config_dir);
            let plan = sidecar::plan_sidecar(&settings_snapshot, &config_path, &state.app_dir);
            let (service, base_url) = commands::resolve_service(state.sidecar.as_ref(), plan).await;
            // E18: re-read `state.settings` *after* the (up to 30s) await
            // above rather than reusing `settings_snapshot` -- if the
            // operator called `set_meetings_root` while this task was
            // still resolving, the root installed here must be whatever
            // is current now, not the stale pre-await value (which, on
            // first run, is `state.config_dir`). This does not eliminate
            // every out-of-order interleaving between this task and
            // `set_meetings_root`'s own background resolution (that would
            // need a generation counter or a shared lock across both
            // paths), but it removes the specific defect of installing a
            // root nobody asked for.
            let current_settings = state.settings.read().await.clone();
            let root = current_settings
                .meetings_root
                .clone()
                .map(PathBuf::from)
                .unwrap_or_else(|| state.config_dir.clone());
            commands::apply_resolved_service(&state, service, base_url, root).await;
        });
    }

    Ok(())
}

/// Builds and runs the Tauri application: registers the dialog plugin,
/// wires managed state and the six IPC commands (T11's own `commands.rs`),
/// and kills any running sidecar child on exit.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        // The updater checks a signed manifest on GitHub Releases and can
        // install a newer build; `process` is what lets it relaunch
        // afterwards. Both are inert without the signing public key in
        // tauri.conf.json, so a fork that has not set one up cannot be
        // silently updated by anybody else.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(setup_app_state)
        .invoke_handler(tauri::generate_handler![
            commands::get_settings,
            commands::set_meetings_root,
            commands::enqueue_paths,
            commands::list_jobs,
            commands::service_status,
            commands::reveal_job,
            commands::list_vault,
            commands::reveal_vault_entry,
            commands::read_transcript,
            commands::update_vault_entry,
            commands::delete_vault_entry,
            commands::set_speaker_labels,
            commands::read_summary,
            commands::transcribe_vault_entry,
            commands::cancel_job,
            commands::prepare_update,
            commands::list_service_jobs,
            commands::model::model_download_status,
            commands::model::start_model_download,
            commands::model::cancel_model_download,
            commands::llm::summarize_vault_entry,
            commands::llm::extract_vault_entry,
            commands::llm::export_recording,
            commands::llm::llm_model_download_status,
            commands::llm::start_llm_model_download,
            commands::llm::cancel_llm_model_download,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<commands::AppState>() {
                    tauri::async_runtime::block_on(state.sidecar.terminate());
                }
            }
        });
}
