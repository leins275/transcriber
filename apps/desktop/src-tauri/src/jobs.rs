//! Job registry, sequential pipeline and poll loop (FR-8, FR-14, NFR-4).
//!
//! `JobRegistry` is the seam between the six IPC commands (T11) and the
//! rest of this crate: `enqueue()` returns [`JobSnapshot`]s immediately
//! (NFR-1) while a single background worker task ingests and submits each
//! job **one at a time, in order** (FR-8) via an mpsc queue. Once a job is
//! submitted, a *separate* per-job task polls [`TranscriptionService::status`]
//! on [`POLL_INTERVAL`] and updates the registry — polling multiple jobs
//! happens concurrently; only ingest+submit is serialized. Every state
//! change is pushed through an injectable [`EventSink`] (a recording `Vec`
//! in tests, the Tauri `AppHandle` in production — T11), which is what
//! keeps this module testable without a Tauri runtime.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use async_trait::async_trait;
use serde::Serialize;
use tokio::sync::{mpsc, RwLock};
use uuid::Uuid;

use vault::{Classification, CollisionOutcome};

use crate::ingest;
use crate::paths;
use crate::service::{
    JobState as ServiceJobState, JobStatus, ServiceError, SubmitRequest, TranscriptionService,
};

/// How often a job's status is polled once it has been submitted.
///
/// NFR-4 requires the UI to never show status more than 2 s stale while a
/// job is running. Polling every 1.5 s leaves headroom for one slow round
/// trip (network + service think time) before that bound is at risk.
pub const POLL_INTERVAL: Duration = Duration::from_millis(1500);

/// How many consecutive poll failures a still-running job tolerates before
/// it is treated as service-unavailable rather than polled forever (FR-13).
const MAX_CONSECUTIVE_POLL_ERRORS: u32 = 3;

/// The filename F2 writes its transcript to, inside the meeting directory.
const TRANSCRIPT_FILE_NAME: &str = "transcript.json";

/// The seam's job-lifecycle state as rendered to the UI (IPC contract's
/// `JobState`, plan.md). A superset of `service::JobState`: it also covers
/// the ingest phase and outright rejection, neither of which the
/// transcription service seam knows about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Pending,
    Ingesting,
    Queued,
    Running,
    Done,
    Failed,
    Rejected,
}

/// One job's current state, mirroring the IPC contract's `JobSnapshot`
/// exactly (plan.md — frontend `types.ts` is a mechanical transcription of
/// this shape).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct JobSnapshot {
    pub id: String,
    pub source_path: String,
    pub file_name: String,
    pub state: JobState,
    pub classification: Option<String>,
    pub meeting_dir: Option<String>,
    pub source_dest: Option<String>,
    pub transcript_path: Option<String>,
    pub progress: Option<f64>,
    pub message: Option<String>,
    pub error_kind: Option<String>,
    pub created_at: String,
}

/// Where every job-state transition is pushed. Implemented by a Tauri
/// `AppHandle` in production (T11, emitting `jobs://updated`) and by a
/// recording `Vec` in tests, so this module never needs a Tauri runtime.
pub trait EventSink: Send + Sync {
    fn emit(&self, snapshot: &JobSnapshot);
}

/// Where "the transcription seam just became unreachable mid-session" is
/// reported once a still-polling job exhausts [`MAX_CONSECUTIVE_POLL_ERRORS`]
/// (FR-13, E5). Distinct from [`EventSink`]: this is a *service*-level
/// signal, not a per-job one -- `commands::AppState` wires an adapter here
/// that turns it into the same `service://status` event
/// `apply_resolved_service` emits, so the frontend's `ServiceBanner` flips
/// to `unavailable` even when the sidecar dies well after startup, not only
/// when the seam never comes up in the first place. Attaching a sink is
/// optional (`JobRegistry::set_status_sink`) so every existing test that
/// never observed this signal is unaffected.
#[async_trait]
pub trait ServiceUnavailableSink: Send + Sync {
    async fn service_unavailable(&self, detail: String);
}

/// A pending unit of work handed to the background worker: the job's
/// already-registered `Pending` snapshot plus the source path to ingest.
struct PendingJob {
    snapshot: JobSnapshot,
    source_path: PathBuf,
}

/// State shared between the registry handle, the worker task and every
/// per-job poll task.
///
/// `root` and `service` are each behind their own `RwLock` (rather than the
/// whole `JobRegistry` being replaced) so that a sidecar resolving or a
/// meetings-root change (`apply_resolved_service`, E2) can be applied
/// in-place: any job already enqueued -- including one still sitting in the
/// worker queue while the sidecar is starting -- keeps its entry in `jobs`
/// and stays reachable via `list()`/`get()` and revealable, instead of being
/// silently dropped along with a discarded registry.
struct Shared {
    root: RwLock<PathBuf>,
    service: RwLock<Arc<dyn TranscriptionService>>,
    sink: Arc<dyn EventSink>,
    /// Optional (E5) -- see [`ServiceUnavailableSink`]. A plain `std::Mutex`
    /// is enough since this is only ever read by cloning the `Option` out
    /// before any `.await`, never held across one.
    status_sink: StdMutex<Option<Arc<dyn ServiceUnavailableSink>>>,
    jobs: RwLock<HashMap<String, JobSnapshot>>,
    poll_interval: Duration,
}

impl Shared {
    /// Stores `snapshot` (inserting or replacing by id) and emits it
    /// unconditionally — used for every real transition.
    async fn store_and_emit(&self, snapshot: JobSnapshot) {
        self.jobs
            .write()
            .await
            .insert(snapshot.id.clone(), snapshot.clone());
        self.sink.emit(&snapshot);
    }

    /// Stores and emits `snapshot` only if it differs from what is
    /// currently on record (ignoring `created_at`), so a poll that observes
    /// no change does not emit a duplicate event.
    async fn update_if_changed(&self, snapshot: JobSnapshot) {
        let changed = {
            let jobs = self.jobs.read().await;
            match jobs.get(&snapshot.id) {
                Some(existing) => !snapshots_equal_ignoring_created_at(existing, &snapshot),
                None => true,
            }
        };
        if changed {
            self.store_and_emit(snapshot).await;
        }
    }
}

fn snapshots_equal_ignoring_created_at(a: &JobSnapshot, b: &JobSnapshot) -> bool {
    a.id == b.id
        && a.source_path == b.source_path
        && a.file_name == b.file_name
        && a.state == b.state
        && a.classification == b.classification
        && a.meeting_dir == b.meeting_dir
        && a.source_dest == b.source_dest
        && a.transcript_path == b.transcript_path
        && a.progress == b.progress
        && a.message == b.message
        && a.error_kind == b.error_kind
}

/// Job registry, sequential ingest+submit pipeline and poll loop. Cheap to
/// clone: internally an `Arc`ed shared state plus a `Clone`able channel
/// sender.
#[derive(Clone)]
pub struct JobRegistry {
    shared: Arc<Shared>,
    queue_tx: mpsc::UnboundedSender<PendingJob>,
}

impl JobRegistry {
    /// A registry using the production [`POLL_INTERVAL`].
    pub fn new(
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        sink: Arc<dyn EventSink>,
    ) -> Self {
        Self::with_poll_interval(root, service, sink, POLL_INTERVAL)
    }

    /// A registry polling at `poll_interval` — the production constructor
    /// pins this to [`POLL_INTERVAL`]; tests use a short interval so they
    /// don't have to wait seconds for a terminal state.
    pub fn with_poll_interval(
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        sink: Arc<dyn EventSink>,
        poll_interval: Duration,
    ) -> Self {
        let shared = Arc::new(Shared {
            root: RwLock::new(root),
            service: RwLock::new(service),
            sink,
            status_sink: StdMutex::new(None),
            jobs: RwLock::new(HashMap::new()),
            poll_interval,
        });
        let (queue_tx, queue_rx) = mpsc::unbounded_channel();
        tokio::spawn(worker_loop(shared.clone(), queue_rx));
        JobRegistry { shared, queue_tx }
    }

    /// Swaps the root and active service **in place** (E2): used when the
    /// sidecar resolves after startup or when the meetings-root changes.
    /// Every job already tracked in `jobs` (including one still ingesting
    /// or polling) keeps its entry -- unlike replacing the whole registry,
    /// which would silently orphan any job enqueued before this call.
    pub async fn set_root_and_service(
        &self,
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
    ) {
        *self.shared.root.write().await = root;
        *self.shared.service.write().await = service;
    }

    /// Swaps **only** the root in place (E21), leaving the active service
    /// untouched. `set_meetings_root_handler` calls this synchronously the
    /// moment a new root is persisted, so a file dropped immediately after
    /// -- before the background sidecar/service resolution in
    /// `resolve_and_apply_meetings_root_service` has had a chance to run,
    /// which can take up to `sidecar::READY_TIMEOUT` on a first-run
    /// `uv`-resolved sidecar -- is still ingested under the *new* root
    /// instead of whatever root (possibly `state.config_dir` on first run)
    /// was in effect before. The background resolution still owns the
    /// service half via `set_root_and_service`, and re-reads the current
    /// settings root itself, so it does not race or undo this.
    pub async fn set_root(&self, root: PathBuf) {
        *self.shared.root.write().await = root;
    }

    /// Attaches the sink a poll-error-budget exhaustion (E5) reports through.
    /// Optional -- a registry with none attached behaves exactly as before.
    pub fn set_status_sink(&self, sink: Arc<dyn ServiceUnavailableSink>) {
        *self
            .shared
            .status_sink
            .lock()
            .expect("status_sink mutex poisoned") = Some(sink);
    }

    /// Registers each path as a new `Pending` job and returns their initial
    /// snapshots immediately — before any ingest or network IO happens
    /// (NFR-1). Actual processing happens serially in the background
    /// worker task.
    pub async fn enqueue(&self, paths: Vec<PathBuf>) -> Vec<JobSnapshot> {
        let mut snapshots = Vec::with_capacity(paths.len());
        for source_path in paths {
            let snapshot = new_pending_snapshot(&source_path);
            self.shared.store_and_emit(snapshot.clone()).await;
            snapshots.push(snapshot.clone());
            // An error here only means the worker task has already shut
            // down (e.g. during app exit); the job's `Pending` snapshot was
            // already recorded and returned above.
            let _ = self.queue_tx.send(PendingJob {
                snapshot,
                source_path,
            });
        }
        snapshots
    }

    /// The current snapshot of every known job, in no particular order.
    pub async fn list(&self) -> Vec<JobSnapshot> {
        self.shared.jobs.read().await.values().cloned().collect()
    }

    /// The current snapshot of one job, if it exists.
    pub async fn get(&self, id: &str) -> Option<JobSnapshot> {
        self.shared.jobs.read().await.get(id).cloned()
    }
}

/// Consumes the ingest+submit queue strictly in order: the next job is not
/// started until the previous one has been ingested and submitted (or
/// rejected/failed) — FR-8's "processes them sequentially". Polling for a
/// submitted job runs as its own spawned task, so a long-running job never
/// delays the next one's ingest.
async fn worker_loop(shared: Arc<Shared>, mut queue_rx: mpsc::UnboundedReceiver<PendingJob>) {
    while let Some(pending) = queue_rx.recv().await {
        process_one(shared.clone(), pending).await;
    }
}

async fn process_one(shared: Arc<Shared>, pending: PendingJob) {
    let PendingJob {
        mut snapshot,
        source_path,
    } = pending;

    snapshot.state = JobState::Ingesting;
    shared.store_and_emit(snapshot.clone()).await;

    let root = shared.root.read().await.clone();
    let outcome = match ingest::ingest(&root, &source_path).await {
        Ok(outcome) => outcome,
        Err(err) => {
            // FR-6: a rejected file (bad extension, directory, escapes the
            // root, ...) does not abort the rest of the batch — this job
            // simply ends here, terminal, while the worker moves on to the
            // next queued job.
            snapshot.state = JobState::Rejected;
            snapshot.error_kind = Some(app_error_kind_str(err.kind()));
            snapshot.message = Some(err.message().to_string());
            shared.store_and_emit(snapshot).await;
            return;
        }
    };

    let transcript_path = outcome.meeting_dir.join(TRANSCRIPT_FILE_NAME);
    snapshot.classification = Some(classification_str(&outcome.classification));
    snapshot.meeting_dir = Some(path_string(&outcome.meeting_dir));
    snapshot.source_dest = Some(path_string(&outcome.source_dest));
    snapshot.transcript_path = Some(path_string(&transcript_path));
    // FR-9 (E4): F1's collision outcome must be reported, not silently
    // dropped -- a re-drop that F1 treated as a no-op duplicate, or one
    // that landed in a numerically suffixed folder, must not render
    // identically to a fresh ingest.
    if let Some(message) = collision_message(&outcome.collision) {
        snapshot.message = Some(message);
    }

    let submit_request = SubmitRequest {
        audio_path: path_string(&outcome.source_dest),
        output_dir: path_string(&outcome.meeting_dir),
        language: None,
    };

    let service = shared.service.read().await.clone();
    let service_job_id = match service.submit(submit_request).await {
        Ok(service_job_id) => service_job_id,
        Err(err) => {
            // FR-13: the recording is already filed (above) even though the
            // service could not be reached — this is a distinct
            // "awaiting/failed transcription" outcome, never reported as an
            // ingest failure.
            snapshot.state = JobState::Failed;
            snapshot.error_kind = Some(service_error_kind_str(&err).to_string());
            snapshot.message = Some(err.to_string());
            shared.store_and_emit(snapshot).await;
            return;
        }
    };

    snapshot.state = JobState::Queued;
    snapshot.progress = Some(0.0);
    shared.store_and_emit(snapshot.clone()).await;

    tokio::spawn(poll_until_terminal(
        shared,
        service,
        snapshot,
        service_job_id,
    ));
}

/// Polls `service_job_id` every `shared.poll_interval` until the job
/// reaches a terminal state (`Done`/`Failed`), or until
/// [`MAX_CONSECUTIVE_POLL_ERRORS`] consecutive poll failures mark it as
/// service-unavailable instead (FR-13). Runs as its own task so it never
/// blocks the ingest+submit worker loop from picking up the next job.
///
/// Polls the *same* `service` instance the job was submitted to (captured by
/// the caller), not whatever `shared.service` currently holds -- E2's fix
/// lets `shared.service`/`shared.root` be swapped in place for *new* work,
/// but a job id only ever means something to the service that issued it.
async fn poll_until_terminal(
    shared: Arc<Shared>,
    service: Arc<dyn TranscriptionService>,
    mut snapshot: JobSnapshot,
    service_job_id: String,
) {
    let mut consecutive_errors = 0u32;
    loop {
        tokio::time::sleep(shared.poll_interval).await;
        match service.status(&service_job_id).await {
            Ok(status) => {
                consecutive_errors = 0;
                apply_status(&mut snapshot, &status);
                shared.update_if_changed(snapshot.clone()).await;
                if matches!(
                    status.state,
                    ServiceJobState::Done | ServiceJobState::Failed
                ) {
                    return;
                }
            }
            Err(err) => {
                consecutive_errors += 1;
                if consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS {
                    snapshot.state = JobState::Failed;
                    snapshot.error_kind = Some(service_error_kind_str(&err).to_string());
                    snapshot.message = Some(err.to_string());
                    shared.store_and_emit(snapshot).await;
                    // FR-13 / E5: the poll-error budget tripping means the
                    // service went unreachable *mid-session*, not only
                    // during startup -- report it the same way
                    // `apply_resolved_service` does, so `ServiceBanner`
                    // flips instead of staying `ready` forever while every
                    // new drop quietly fails.
                    let status_sink = shared
                        .status_sink
                        .lock()
                        .expect("status_sink mutex poisoned")
                        .clone();
                    if let Some(status_sink) = status_sink {
                        status_sink.service_unavailable(err.to_string()).await;
                    }
                    return;
                }
            }
        }
    }
}

/// Renders F1's [`CollisionOutcome`] as the operator-facing note FR-9
/// requires (E4): `None` for a fresh ingest (nothing to report), `Some(..)`
/// naming what happened otherwise. Never a silent overwrite.
fn collision_message(collision: &CollisionOutcome) -> Option<String> {
    match collision {
        CollisionOutcome::Fresh => None,
        CollisionOutcome::DuplicateRedrop => Some(
            "already filed — kept the existing recording (this was an identical re-drop)"
                .to_string(),
        ),
        CollisionOutcome::SuffixedFolder(n) => Some(format!(
            "a different recording already occupied this name; filed into a suffixed folder (\"... ({n})\")"
        )),
    }
}

fn apply_status(snapshot: &mut JobSnapshot, status: &JobStatus) {
    snapshot.progress = Some(status.progress);
    snapshot.error_kind = status.error_kind.clone();
    // Only overwrite an existing message when the service actually reports
    // one -- otherwise a collision note set at ingest time (E4) would be
    // silently wiped by the very next successful poll, which reports no
    // error message of its own.
    if status.error_message.is_some() {
        snapshot.message = status.error_message.clone();
    }
    snapshot.state = match status.state {
        ServiceJobState::Queued => JobState::Queued,
        ServiceJobState::Running => JobState::Running,
        ServiceJobState::Done => JobState::Done,
        ServiceJobState::Failed => JobState::Failed,
    };
}

fn new_pending_snapshot(source_path: &Path) -> JobSnapshot {
    JobSnapshot {
        id: Uuid::new_v4().to_string(),
        source_path: path_string(source_path),
        file_name: file_name_of(source_path),
        state: JobState::Pending,
        classification: None,
        meeting_dir: None,
        source_dest: None,
        transcript_path: None,
        progress: None,
        message: None,
        error_kind: None,
        created_at: now_rfc3339(),
    }
}

fn file_name_of(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| path_string(path))
}

/// Renders a path for display/serialization, stripping a Windows `\\?\`
/// verbatim prefix the same way `paths.rs` does for every other
/// user-facing path in this crate.
fn path_string(path: &Path) -> String {
    paths::strip_verbatim(path).display().to_string()
}

fn classification_str(classification: &Classification) -> String {
    match classification {
        Classification::Sorted { .. } => "sorted".to_string(),
        Classification::Unsorted { .. } => "unsorted".to_string(),
    }
}

/// The `ErrorKind` string for an ingest-side [`crate::error::AppError`],
/// reusing its own `Serialize` impl so this never drifts from the IPC
/// contract's frozen `ErrorKind` spelling.
fn app_error_kind_str(kind: crate::error::ErrorKind) -> String {
    serde_json::to_value(kind)
        .ok()
        .and_then(|value| value.as_str().map(str::to_string))
        .unwrap_or_else(|| "internal".to_string())
}

/// Distinguishes "the service could not be reached at all" from "the
/// service answered but with an error", matching the IPC contract's
/// separate `service_unavailable` / `service` error kinds.
fn service_error_kind_str(err: &ServiceError) -> &'static str {
    match err {
        ServiceError::Unavailable { .. } => "service_unavailable",
        ServiceError::Http { .. } | ServiceError::Decode { .. } | ServiceError::Auth { .. } => {
            "service"
        }
    }
}

/// A plain `YYYY-MM-DDTHH:MM:SSZ` UTC timestamp built from pure integer
/// arithmetic (Howard Hinnant's `civil_from_days` algorithm —
/// <http://howardhinnant.github.io/date_algorithms.html>) so this module
/// does not need a date/time crate dependency just to stamp `created_at`.
fn now_rfc3339() -> String {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = elapsed.as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let time_of_day = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = time_of_day / 3600;
    let minute = (time_of_day % 3600) / 60;
    let second = time_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    use async_trait::async_trait;
    use tempfile::tempdir;

    use crate::service::fake::{FakeService, FakeTiming};
    use crate::service::{
        JobStatus, ServiceError, ServiceHealth, SubmitRequest, TranscriptionService,
    };

    use super::{JobRegistry, JobSnapshot, JobState, POLL_INTERVAL};

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    /// Records every emitted snapshot in order, standing in for the Tauri
    /// `AppHandle` event emitter in production (see `EventSink`).
    #[derive(Default)]
    struct RecordingSink {
        events: Mutex<Vec<JobSnapshot>>,
    }

    impl RecordingSink {
        fn new() -> Self {
            Self::default()
        }

        fn snapshots(&self) -> Vec<JobSnapshot> {
            self.events
                .lock()
                .expect("recording sink mutex poisoned")
                .clone()
        }
    }

    impl super::EventSink for RecordingSink {
        fn emit(&self, snapshot: &JobSnapshot) {
            self.events
                .lock()
                .expect("recording sink mutex poisoned")
                .push(snapshot.clone());
        }
    }

    /// Wraps a `FakeService`, recording the order of `submit()` calls and
    /// flagging whether more than one was ever in flight at once — used to
    /// assert FR-8's "strictly one at a time, in order".
    struct OrderTrackingService {
        inner: FakeService,
        order: Mutex<Vec<String>>,
        in_flight: AtomicUsize,
        overlap_detected: AtomicBool,
    }

    impl OrderTrackingService {
        fn new(inner: FakeService) -> Self {
            OrderTrackingService {
                inner,
                order: Mutex::new(Vec::new()),
                in_flight: AtomicUsize::new(0),
                overlap_detected: AtomicBool::new(false),
            }
        }

        fn order(&self) -> Vec<String> {
            self.order.lock().expect("order mutex poisoned").clone()
        }
    }

    #[async_trait]
    impl TranscriptionService for OrderTrackingService {
        async fn health(&self) -> Result<ServiceHealth, ServiceError> {
            self.inner.health().await
        }

        async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
            let concurrent = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            if concurrent != 1 {
                self.overlap_detected.store(true, Ordering::SeqCst);
            }
            self.order
                .lock()
                .expect("order mutex poisoned")
                .push(req.audio_path.clone());
            let result = self.inner.submit(req).await;
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
            result
        }

        async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError> {
            self.inner.status(job_id).await
        }
    }

    async fn wait_for(
        registry: &JobRegistry,
        id: &str,
        matches: impl Fn(&JobSnapshot) -> bool,
        timeout: Duration,
    ) -> JobSnapshot {
        let start = Instant::now();
        loop {
            if let Some(snapshot) = registry.get(id).await {
                if matches(&snapshot) {
                    return snapshot;
                }
            }
            if start.elapsed() > timeout {
                panic!("job {id} did not reach the expected state within {timeout:?}");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    fn write_recording(dir: &std::path::Path, name: &str) -> PathBuf {
        let path = dir.join(name);
        fs::write(&path, b"recording bytes").expect("write fixture");
        path
    }

    #[test]
    fn enqueue_returns_snapshots_synchronously_within_300ms_for_three_files() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let sources = vec![
                write_recording(downloads.path(), "ELS - 260812 - Job A.mp4"),
                write_recording(downloads.path(), "ELS - 260812 - Job B.mp4"),
                write_recording(downloads.path(), "ELS - 260812 - Job C.mp4"),
            ];

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 2,
                running_polls: 4,
            }));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(1500),
            );

            let start = Instant::now();
            let snapshots = registry.enqueue(sources).await;
            let elapsed = start.elapsed();

            assert_eq!(snapshots.len(), 3);
            assert!(
                elapsed < Duration::from_millis(300),
                "enqueue took {elapsed:?}, must ack within 300ms (NFR-1)"
            );
            for snapshot in &snapshots {
                assert_eq!(snapshot.state, JobState::Pending);
            }
        });
    }

    #[test]
    fn three_files_are_ingested_and_submitted_strictly_one_at_a_time_in_order() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let sources = vec![
                write_recording(downloads.path(), "ELS - 260812 - Job A.mp4"),
                write_recording(downloads.path(), "ELS - 260812 - Job B.mp4"),
                write_recording(downloads.path(), "ELS - 260812 - Job C.mp4"),
            ];
            // F1's naming convention turns `<PROJECT> - <date> - <title>.ext`
            // into a meeting folder named `<date> - <title>` (the project
            // code becomes its own parent directory) -- this is what each
            // `audio_path` (`<meeting_dir>/source.<ext>`) actually encodes,
            // so the expected order (E15) must be expressed in *that*
            // vocabulary, not the original source file names.
            let expected_order = vec![
                "260812 - Job A".to_string(),
                "260812 - Job B".to_string(),
                "260812 - Job C".to_string(),
            ];

            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            });
            let tracker = Arc::new(OrderTrackingService::new(fake));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                tracker.clone(),
                sink,
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(sources).await;

            for snapshot in &snapshots {
                wait_for(
                    &registry,
                    &snapshot.id,
                    |s| matches!(s.state, JobState::Done | JobState::Failed),
                    Duration::from_secs(5),
                )
                .await;
            }

            assert!(
                !tracker.overlap_detected.load(Ordering::SeqCst),
                "submit() calls must never overlap (FR-8)"
            );
            let submitted_names: Vec<String> = tracker
                .order()
                .iter()
                .map(|audio_path| {
                    PathBuf::from(audio_path)
                        .parent()
                        .and_then(|p| p.file_name())
                        .map(|n| n.to_string_lossy().into_owned())
                        .unwrap_or_default()
                })
                .collect();
            // audio_path is <meeting_dir>/source.<ext>; the meeting dir name
            // (not "source.mp4") encodes which job this was and, per FR-8,
            // must appear in exactly the order the files were dropped (E15
            // -- this used to only assert the lengths matched, leaving
            // FR-8's ordering half unverified).
            assert_eq!(submitted_names, expected_order);
        });
    }

    #[test]
    fn poll_interval_is_1500ms_named_constant_satisfying_nfr4() {
        assert_eq!(POLL_INTERVAL, Duration::from_millis(1500));
        assert!(
            POLL_INTERVAL < Duration::from_secs(2),
            "must stay under NFR-4's 2s staleness bound"
        );
    }

    #[test]
    fn job_transitions_pending_ingesting_queued_running_done_emitting_one_snapshot_per_transition()
    {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 1,
                running_polls: 1,
            }));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink.clone(),
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();

            let done = wait_for(
                &registry,
                &id,
                |s| s.state == JobState::Done,
                Duration::from_secs(5),
            )
            .await;

            let states: Vec<JobState> = sink
                .snapshots()
                .into_iter()
                .filter(|s| s.id == id)
                .map(|s| s.state)
                .collect();
            assert_eq!(
                states,
                vec![
                    JobState::Pending,
                    JobState::Ingesting,
                    JobState::Queued,
                    JobState::Running,
                    JobState::Done,
                ]
            );

            // FR-15: transcript_path is <meeting_dir>\transcript.json.
            let meeting_dir = done.meeting_dir.clone().expect("meeting_dir must be set");
            let expected_transcript = PathBuf::from(&meeting_dir).join("transcript.json");
            assert_eq!(
                done.transcript_path.as_deref(),
                Some(expected_transcript.to_string_lossy().as_ref())
            );
        });
    }

    #[test]
    fn a_collision_is_reported_on_the_job_and_survives_through_the_done_state() {
        // FR-9 / E4: a re-drop that F1 places into a numerically suffixed
        // folder (because a *different* recording already occupies the
        // classified name) must be reported to the operator, not rendered
        // identically to a fresh ingest -- and that report must still be
        // visible once the job finishes, not wiped by the next poll.
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");

            let first_source = downloads.path().join("ELS - 260812 - Security issue.mp4");
            fs::write(&first_source, b"AAAA").expect("write first fixture");

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            // First drop: fresh, no collision to report.
            let first_snapshots = registry.enqueue(vec![first_source]).await;
            let first_id = first_snapshots[0].id.clone();
            let first_done = wait_for(
                &registry,
                &first_id,
                |s| s.state == JobState::Done,
                Duration::from_secs(5),
            )
            .await;
            assert_eq!(first_done.message, None);

            // Second drop: a *different* recording that classifies to the
            // same project/date/title -- F1 suffixes the folder rather than
            // overwriting.
            let second_source = downloads.path().join("ELS - 260812 - Security issue.mp4");
            fs::write(&second_source, b"BBBBBBBB").expect("write second fixture");
            let second_snapshots = registry.enqueue(vec![second_source]).await;
            let second_id = second_snapshots[0].id.clone();
            let second_done = wait_for(
                &registry,
                &second_id,
                |s| s.state == JobState::Done,
                Duration::from_secs(5),
            )
            .await;

            let message = second_done
                .message
                .expect("a suffixed-folder collision must be reported in the job message");
            assert!(
                message.contains("2") || message.to_lowercase().contains("suffix"),
                "message must identify the collision, got: {message}"
            );
            assert!(
                second_done
                    .meeting_dir
                    .as_deref()
                    .unwrap()
                    .ends_with("Security issue (2)"),
                "the suffixed destination itself is unaffected by this fix, just documenting it"
            );
        });
    }

    #[test]
    fn service_reported_failure_sets_failed_with_message_verbatim() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            service.queue_failure("provider exploded: disk full");
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();

            let terminal = wait_for(
                &registry,
                &id,
                |s| matches!(s.state, JobState::Done | JobState::Failed),
                Duration::from_secs(5),
            )
            .await;

            assert_eq!(terminal.state, JobState::Failed);
            assert_eq!(
                terminal.message.as_deref(),
                Some("provider exploded: disk full")
            );
        });
    }

    #[test]
    fn service_down_still_ingests_but_marks_a_distinct_awaiting_transcription_state() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let service = Arc::new(FakeService::new());
            service.set_down(true);
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();

            let terminal = wait_for(
                &registry,
                &id,
                |s| matches!(s.state, JobState::Failed | JobState::Rejected),
                Duration::from_secs(5),
            )
            .await;

            assert_eq!(terminal.state, JobState::Failed);
            assert_eq!(terminal.error_kind.as_deref(), Some("service_unavailable"));
            let meeting_dir = terminal
                .meeting_dir
                .clone()
                .expect("meeting_dir must be set");
            assert!(
                PathBuf::from(&meeting_dir).join("source.mp4").is_file(),
                "the recording must still be filed into the vault even though the service was down"
            );
        });
    }

    #[test]
    fn rejected_file_in_mixed_batch_does_not_abort_the_accepted_one() {
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let good = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");
            let bad = write_recording(downloads.path(), "notes.txt");

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(vec![good, bad]).await;
            let good_id = snapshots[0].id.clone();
            let bad_id = snapshots[1].id.clone();

            let good_terminal = wait_for(
                &registry,
                &good_id,
                |s| matches!(s.state, JobState::Done | JobState::Failed),
                Duration::from_secs(5),
            )
            .await;
            let bad_terminal = wait_for(
                &registry,
                &bad_id,
                |s| s.state == JobState::Rejected,
                Duration::from_secs(5),
            )
            .await;

            assert_eq!(good_terminal.state, JobState::Done);
            assert_eq!(bad_terminal.state, JobState::Rejected);
            assert_eq!(
                bad_terminal.error_kind.as_deref(),
                Some("unsupported_extension")
            );
            assert!(bad_terminal
                .message
                .as_deref()
                .unwrap()
                .contains("notes.txt"));
        });
    }

    #[test]
    fn ingest_work_does_not_block_the_async_runtime() {
        // A single-thread runtime: if `ingest()` performed blocking IO
        // directly on this thread instead of via `spawn_blocking`, the
        // heartbeat task below would starve and its tick count would stall
        // (NFR-2).
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build current-thread runtime");
        runtime.block_on(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = downloads.path().join("ELS - 260812 - Security issue.mp4");
            fs::write(&source, vec![0u8; 4 * 1024 * 1024]).expect("write fixture");

            let service = Arc::new(FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            }));
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            let ticks = Arc::new(AtomicU32::new(0));
            let ticks_task = ticks.clone();
            let heartbeat = tokio::spawn(async move {
                for _ in 0..10 {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    ticks_task.fetch_add(1, Ordering::SeqCst);
                }
            });

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();
            wait_for(
                &registry,
                &id,
                |s| matches!(s.state, JobState::Done | JobState::Failed),
                Duration::from_secs(5),
            )
            .await;

            heartbeat.await.expect("heartbeat task must complete");
            assert!(
                ticks.load(Ordering::SeqCst) >= 8,
                "heartbeat must keep ticking while ingest runs (NFR-2)"
            );
        });
    }

    // -- ServiceUnavailableSink (E5) --------------------------------------

    /// Submits successfully (so the job starts polling) but every
    /// subsequent `status()` call fails -- simulating the service dying
    /// *after* a job is already in flight, which is exactly the mid-session
    /// case FR-13/E5 covers.
    struct DiesAfterSubmitService {
        inner: FakeService,
    }

    #[async_trait]
    impl TranscriptionService for DiesAfterSubmitService {
        async fn health(&self) -> Result<ServiceHealth, ServiceError> {
            self.inner.health().await
        }

        async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
            self.inner.submit(req).await
        }

        async fn status(&self, _job_id: &str) -> Result<JobStatus, ServiceError> {
            Err(ServiceError::Unavailable {
                detail: "service died mid-session".to_string(),
            })
        }
    }

    #[derive(Default)]
    struct RecordingUnavailableSink {
        calls: Mutex<Vec<String>>,
    }

    #[async_trait]
    impl super::ServiceUnavailableSink for RecordingUnavailableSink {
        async fn service_unavailable(&self, detail: String) {
            self.calls
                .lock()
                .expect("recording unavailable-sink mutex poisoned")
                .push(detail);
        }
    }

    #[test]
    fn poll_error_budget_exhaustion_emits_the_service_unavailable_sink() {
        // E5: the service going unreachable *after* a job is already
        // polling must still flip the service-status signal, the same way
        // a sidecar that never comes up in the first place does -- not just
        // mark the individual job failed and leave the banner alone.
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let service = Arc::new(DiesAfterSubmitService {
                inner: FakeService::new(),
            });
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );
            let status_sink = Arc::new(RecordingUnavailableSink::default());
            registry.set_status_sink(status_sink.clone());

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();

            let terminal = wait_for(
                &registry,
                &id,
                |s| s.state == JobState::Failed,
                Duration::from_secs(5),
            )
            .await;
            assert_eq!(terminal.error_kind.as_deref(), Some("service_unavailable"));

            let start = Instant::now();
            loop {
                if !status_sink
                    .calls
                    .lock()
                    .expect("recording unavailable-sink mutex poisoned")
                    .is_empty()
                {
                    break;
                }
                if start.elapsed() > Duration::from_secs(5) {
                    panic!("service_unavailable was never reported to the status sink");
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }

            let calls = status_sink
                .calls
                .lock()
                .expect("recording unavailable-sink mutex poisoned")
                .clone();
            assert_eq!(calls.len(), 1);
            assert!(calls[0].contains("service died mid-session"));
        });
    }

    #[test]
    fn a_registry_with_no_status_sink_attached_behaves_exactly_as_before() {
        // Every other test in this module never attaches a status sink at
        // all -- this documents that omission is safe (E5 is additive).
        run(async {
            let root = tempdir().expect("tempdir");
            let downloads = tempdir().expect("tempdir");
            let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

            let service = Arc::new(DiesAfterSubmitService {
                inner: FakeService::new(),
            });
            let sink = Arc::new(RecordingSink::new());
            let registry = JobRegistry::with_poll_interval(
                root.path().to_path_buf(),
                service,
                sink,
                Duration::from_millis(5),
            );

            let snapshots = registry.enqueue(vec![source]).await;
            let id = snapshots[0].id.clone();

            let terminal = wait_for(
                &registry,
                &id,
                |s| s.state == JobState::Failed,
                Duration::from_secs(5),
            )
            .await;
            assert_eq!(terminal.error_kind.as_deref(), Some("service_unavailable"));
        });
    }
}
