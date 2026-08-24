//! Shared harness for T13's integration tests (`tests/e2e_flow.rs`).
//!
//! Builds a real [`transcriber_desktop_lib::commands::AppState`] wired
//! exactly the way `lib.rs` wires it in production — a real `JobRegistry`
//! (T10), real `ingest`/`paths` (T4/T9) against a `tempfile` vault root, and
//! a real `service::fake::FakeService` (T5) standing in for the engine — so these
//! tests exercise the crate's public surface end to end (`commands` +
//! `jobs` + `ingest`) with no model loaded and no inference run. The only
//! piece swapped out is the collaborator that would otherwise touch the OS
//! shell: `Revealer` (never opens a visible Explorer window).

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tempfile::tempdir;

use transcriber_desktop_lib::commands::{AppState, Revealer, ServiceStatusSink, ServiceStatusView};
use transcriber_desktop_lib::config::Settings;
use transcriber_desktop_lib::error::AppError;
use transcriber_desktop_lib::jobs::{EventSink, JobSnapshot, JobState};
use transcriber_desktop_lib::service::TranscriptionService;

/// Records every emitted `jobs://updated` snapshot, standing in for the
/// Tauri `AppHandle` emitter this crate uses in production.
#[derive(Default)]
pub struct RecordingSink {
    snapshots: Mutex<Vec<JobSnapshot>>,
}

impl EventSink for RecordingSink {
    fn emit(&self, snapshot: &JobSnapshot) {
        self.snapshots
            .lock()
            .expect("recording sink mutex poisoned")
            .push(snapshot.clone());
    }
}

/// Records every `service://status` transition.
#[derive(Default)]
pub struct RecordingStatusSink {
    statuses: Mutex<Vec<ServiceStatusView>>,
}

impl ServiceStatusSink for RecordingStatusSink {
    fn emit(&self, status: &ServiceStatusView) {
        self.statuses
            .lock()
            .expect("status sink mutex poisoned")
            .push(status.clone());
    }
}

/// Records every path Explorer would have been asked to reveal, so a test
/// never opens a visible window.
#[derive(Default)]
pub struct RecordingRevealer {
    pub calls: Mutex<Vec<PathBuf>>,
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

/// Builds an `AppState` wired against `root` (both as the meetings root and
/// the config dir, which these tests never read from) and `service`.
pub fn build_state(root: PathBuf, service: Arc<dyn TranscriptionService>) -> AppState {
    let settings = Settings {
        meetings_root: Some(root.to_string_lossy().into_owned()),
        ..Settings::default()
    };
    AppState::new_with(
        root.clone(),
        root.clone(),
        settings,
        root,
        service,
        None,
        false,
        Arc::new(RecordingSink::default()),
        Arc::new(RecordingStatusSink::default()),
        Arc::new(RecordingRevealer::default()),
    )
}

/// Writes a fixture recording at `dir/name` with distinguishable content.
pub fn write_recording(dir: &std::path::Path, name: &str, content: &[u8]) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, content).expect("write fixture recording");
    path
}

/// A fresh, empty `tempdir` standing in for a Downloads-like source folder
/// or the meetings-root vault, per test.
pub fn new_tempdir() -> tempfile::TempDir {
    tempdir().expect("create tempdir")
}

/// Polls `state`'s job registry until `id` reaches a terminal `JobState`
/// (`Done`/`Failed`/`Rejected`) or `timeout` elapses.
pub async fn wait_for_terminal(state: &AppState, id: &str, timeout: Duration) -> JobSnapshot {
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

/// Runs `future` to completion on a fresh multi-thread Tokio runtime — the
/// same pattern every other module's own `#[cfg(test)]` mod already uses,
/// since this crate's `tokio` dependency does not enable the `macros`
/// feature (so `#[tokio::test]` is unavailable).
pub fn run<F: std::future::Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime")
        .block_on(future)
}
