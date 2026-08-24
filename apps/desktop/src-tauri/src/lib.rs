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
/// engine's model directory, and `models\`/`logs\`/`data\`.
pub mod app_paths;

/// `config.json` load/save/validate (FR-16..18).
pub mod config;

/// Path canonicalization and containment (FR-11, FR-15).
pub mod paths;

/// Wrapper over F1's vault crate, off the UI thread (FR-9, FR-10).
pub mod ingest;

/// `TranscriptionService` trait, job model, the in-process engine binding and
/// an in-memory fake (FR-12).
pub mod service;

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

/// Resolves `app_config_dir()`, loads settings, starts the engine, and
/// manages the resulting [`commands::AppState`].
///
/// Starting the engine is cheap -- it opens the ledger and spawns a worker
/// thread; models load lazily on the first job -- so unlike the sidecar it
/// replaced there is nothing to wait for and nothing to resolve in the
/// background.
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

    // `--fake-service` / `TRANSCRIBER_FAKE_SERVICE`: run the UI with no
    // models and no inference, which is how the frontend is developed.
    let fake_requested = commands::fake_service_requested(
        std::env::var("TRANSCRIBER_FAKE_SERVICE").ok(),
        &std::env::args().collect::<Vec<_>>(),
    );

    let engine = if fake_requested {
        None
    } else {
        start_engine(&app_dir, &config::config_path(&config_dir))
    };

    let initial_service: Arc<dyn TranscriptionService> = match &engine {
        Some(engine) => Arc::new(service::local::LocalTranscriptionService::new(
            engine.clone(),
        )),
        // Either the fake was asked for, or the engine could not start. The
        // window still opens: the transcription seam being down has never been
        // allowed to stop that (FR-13).
        None if fake_requested => Arc::new(FakeService::new()),
        None => Arc::new(commands::UnavailableTranscriptionService::new(
            "the engine could not be started".to_string(),
        )),
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
            // Nothing is starting in the background any more: the engine is
            // either up by now or it is not.
            false,
            sink,
            status_sink,
        )
    });
    // `state` is not yet shared (that happens at `app.manage` below), so a
    // direct field replacement needs no lock.
    if let Some(message) = config_load_error {
        state.config_error = tokio::sync::RwLock::new(Some(message));
    }
    if let Some(engine) = engine {
        state.engine = Some(engine);
    }
    app.manage(state);

    Ok(())
}

/// Starts the in-process engine.
///
/// Returns `None` if the engine cannot be started -- a config that will not
/// load, a ledger that will not open. Startup then continues with an
/// unavailable service rather than failing: the transcription seam being down
/// has never been allowed to stop the window from opening (FR-13), and that
/// holds whether the seam is a child process or a thread.
fn start_engine(
    app_dir: &std::path::Path,
    config_path: &std::path::Path,
) -> Option<engine::EngineHandle> {
    let mut env = engine::config::process_env();
    // The desktop app has already resolved both of these; the engine must not
    // re-derive them from its own executable path and disagree.
    env.insert(
        "TRANSCRIBER_APP_DIR".to_string(),
        app_dir.display().to_string(),
    );

    let config = match engine::Config::load(Some(config_path), &env) {
        Ok(config) => config,
        Err(err) => {
            eprintln!("[transcriber] engine: cannot load config: {err}");
            return None;
        }
    };
    let ledger = match engine::Ledger::open(&config.db_path) {
        Ok(ledger) => ledger,
        Err(err) => {
            eprintln!("[transcriber] engine: cannot open the job ledger: {err}");
            return None;
        }
    };

    // Each runner is built on the worker thread, so the factory carries a
    // copy of the configuration rather than a borrow of it.
    let runner_config = config.clone();

    match engine::EngineHandle::start(
        config,
        ledger,
        Box::new(move || {
            Box::new(engine::runner::EngineRunner::new(runner_config.clone()))
                as Box<dyn engine::JobRunner>
        }),
    ) {
        Ok(handle) => {
            eprintln!("[transcriber] engine started");
            Some(handle)
        }
        Err(err) => {
            eprintln!("[transcriber] engine: cannot start: {err}");
            None
        }
    }
}

/// Builds and runs the Tauri application: registers the dialog plugin,
/// wires managed state and the six IPC commands (T11's own `commands.rs`),
/// and shuts the engine down cleanly on exit.
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
            commands::llm::export_project_essence,
            commands::llm::list_project_artifacts,
            commands::llm::read_artifact,
            commands::llm::reveal_artifact,
            commands::llm::list_project_reports,
            commands::llm::read_report,
            commands::llm::reveal_report,
            commands::llm::llm_model_download_status,
            commands::llm::start_llm_model_download,
            commands::llm::cancel_llm_model_download,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                // Stop the worker and let it finish the job in hand rather
                // than tearing a half-written transcript out from under it.
                if let Some(state) = app_handle.try_state::<commands::AppState>() {
                    if let Some(engine) = state.engine.as_ref() {
                        engine.shutdown();
                    }
                }
            }
        });
}
