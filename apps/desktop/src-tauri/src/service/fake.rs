//! In-memory `TranscriptionService` fake (FR-12, FR-13, FR-14).
//!
//! `FakeService` walks `Queued -> Running(progress) -> Done|Failed` purely
//! from a per-job poll counter — no background task, no timer, no socket —
//! so every other task's tests can drive the whole UI flow deterministically
//! without a running F2 process.

use std::collections::HashMap;
use std::sync::Mutex;

use async_trait::async_trait;
use uuid::Uuid;

use super::{
    JobState, JobStatus, ModelDownloadState, ModelDownloadStatus, ServiceError, ServiceHealth,
    SubmitRequest, TranscriptionService,
};

/// How many `status()` polls a scripted job spends in each phase.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FakeTiming {
    /// Polls spent reporting `Queued` before advancing to `Running`.
    pub queued_polls: u32,
    /// Polls spent reporting `Running` (progress climbs linearly across
    /// these) before advancing to the job's terminal state.
    pub running_polls: u32,
}

impl Default for FakeTiming {
    fn default() -> Self {
        FakeTiming {
            queued_polls: 1,
            running_polls: 3,
        }
    }
}

/// The terminal state a scripted job resolves to.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ScriptedOutcome {
    Succeed,
    Fail(String),
}

struct ScriptedJob {
    outcome: ScriptedOutcome,
    timing: FakeTiming,
    polls: u32,
}

/// In-memory model-download simulation (T13, FR-12, FR-16, FR-17). Each
/// `model_download_status()` poll advances the transfer by one simulated
/// chunk so a caller that polls repeatedly sees monotonically increasing
/// progress, mirroring F2's real behaviour without a background thread or a
/// timer.
struct FakeModelDownload {
    present: bool,
    state: ModelDownloadState,
    downloaded_bytes: u64,
    total_bytes: u64,
    /// How many `model_download_status()` polls a started transfer spends
    /// downloading before landing on its terminal state.
    downloading_polls: u32,
    polls: u32,
    /// Script the next transfer to fail once it reaches its terminal poll,
    /// consumed by exactly one `start_model_download()` (mirrors
    /// `FakeTiming`'s own next-outcome pattern above).
    fail_next: bool,
    /// E13: whether the simulated CUDA runtime is present -- mirrors F2's
    /// `/health`'s `cuda_runtime_present` (this fake never simulates a
    /// GPU-less host, so it is never `None`).
    cuda_runtime_present: bool,
    /// E13: set by [`FakeService::set_cuda_warning`] to simulate a
    /// `SetupDownload` whose CUDA-runtime phase failed and continued into
    /// the model phase anyway (E4) -- carried verbatim on every status until
    /// cleared.
    cuda_warning: Option<String>,
}

impl FakeModelDownload {
    fn present() -> Self {
        FakeModelDownload {
            present: true,
            state: ModelDownloadState::Complete,
            downloaded_bytes: 3_000_000_000,
            total_bytes: 3_000_000_000,
            downloading_polls: 3,
            polls: 0,
            fail_next: false,
            cuda_runtime_present: true,
            cuda_warning: None,
        }
    }

    fn absent() -> Self {
        FakeModelDownload {
            present: false,
            state: ModelDownloadState::Idle,
            downloaded_bytes: 0,
            total_bytes: 3_000_000_000,
            downloading_polls: 3,
            polls: 0,
            fail_next: false,
            cuda_runtime_present: true,
            cuda_warning: None,
        }
    }

    fn start(&mut self) {
        // F2 never starts a second parallel transfer -- a start while one
        // is already running is a no-op that just returns the current
        // status (see `status()` below, called by every trait method).
        if matches!(
            self.state,
            ModelDownloadState::Downloading | ModelDownloadState::Verifying
        ) {
            return;
        }
        self.state = ModelDownloadState::Downloading;
        self.downloaded_bytes = 0;
        self.polls = 0;
    }

    fn cancel(&mut self) {
        if matches!(
            self.state,
            ModelDownloadState::Downloading | ModelDownloadState::Verifying
        ) {
            self.state = ModelDownloadState::Cancelled;
        }
    }

    /// Advances the simulated transfer by one poll (if currently
    /// downloading) and returns the resulting status -- only the `GET`
    /// (`model_download_status`) trait method calls this; `start`/`cancel`
    /// return [`FakeModelDownload::peek`] instead so a `POST` on an
    /// already-running transfer (F2's documented no-op) never itself
    /// advances progress.
    fn advance_and_peek(&mut self) -> ModelDownloadStatus {
        if self.state == ModelDownloadState::Downloading {
            self.polls += 1;
            let capped = self.polls.min(self.downloading_polls);
            self.downloaded_bytes =
                self.total_bytes * u64::from(capped) / u64::from(self.downloading_polls.max(1));
            if self.polls >= self.downloading_polls {
                if self.fail_next {
                    self.fail_next = false;
                    self.state = ModelDownloadState::Error;
                } else {
                    self.state = ModelDownloadState::Complete;
                    self.present = true;
                }
            }
        }
        self.peek()
    }

    /// Reads the current status without mutating anything.
    fn peek(&self) -> ModelDownloadStatus {
        let (error_kind, error_message) = if self.state == ModelDownloadState::Error {
            (
                Some("checksum_mismatch".to_string()),
                Some("simulated fake-service download failure".to_string()),
            )
        } else {
            (None, None)
        };
        let percent = if self.total_bytes > 0 {
            self.downloaded_bytes as f64 / self.total_bytes as f64 * 100.0
        } else {
            0.0
        };
        ModelDownloadStatus {
            state: self.state,
            downloaded_bytes: self.downloaded_bytes,
            total_bytes: self.total_bytes,
            percent,
            error_kind,
            error_message,
            cuda_warning: self.cuda_warning.clone(),
        }
    }
}

struct Inner {
    down: bool,
    timing: FakeTiming,
    next_outcome: ScriptedOutcome,
    jobs: HashMap<String, ScriptedJob>,
    model: FakeModelDownload,
}

/// In-memory fake used by tests and by `--fake-service` dev mode (T11).
pub struct FakeService {
    inner: Mutex<Inner>,
}

impl FakeService {
    /// A healthy fake whose jobs succeed, using [`FakeTiming::default`].
    pub fn new() -> Self {
        Self::with_timing(FakeTiming::default())
    }

    /// A healthy fake whose jobs succeed, on the given timing. The
    /// simulated model starts out present (dev-mode default: behaves like
    /// an already-installed app) -- use [`FakeService::with_model_absent`]
    /// to simulate a fresh install for T13's own tests.
    pub fn with_timing(timing: FakeTiming) -> Self {
        FakeService {
            inner: Mutex::new(Inner {
                down: false,
                timing,
                next_outcome: ScriptedOutcome::Succeed,
                jobs: HashMap::new(),
                model: FakeModelDownload::present(),
            }),
        }
    }

    /// A healthy fake whose simulated model is *not yet* present (T13,
    /// FR-17) -- the first-run "model missing" case.
    pub fn with_model_absent() -> Self {
        let fake = Self::new();
        fake.inner
            .lock()
            .expect("fake service mutex poisoned")
            .model = FakeModelDownload::absent();
        fake
    }

    /// Script the *next* started model transfer to fail once it reaches its
    /// terminal poll (mirrors [`FakeService::queue_failure`] for jobs).
    pub fn queue_model_download_failure(&self) {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .model
            .fail_next = true;
    }

    /// Simulate a `SetupDownload` whose CUDA-runtime phase failed and
    /// continued into the model phase anyway (E4, E13): `cuda_runtime_present`
    /// flips to `false` and `message` is carried verbatim on every
    /// subsequent health/status call, until a fresh [`FakeService`] is built.
    pub fn set_cuda_warning(&self, message: impl Into<String>) {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cuda_runtime_present = false;
        inner.model.cuda_warning = Some(message.into());
    }

    /// Simulate the *durable* CUDA-missing case (E13): a fresh sidecar
    /// process after an earlier run's failed CUDA download -- no
    /// `SetupDownload` instance survives a restart, so `cuda_warning` is
    /// `None`, but `/health.cuda_runtime_present` still durably reports the
    /// runtime missing.
    pub fn set_cuda_runtime_missing(&self) {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cuda_runtime_present = false;
    }

    /// Mark the fake up or down. While down, `health()` and `submit()`
    /// return `ServiceError::Unavailable`, but jobs submitted before going
    /// down keep reporting status (FR-13) — `status()` never consults this
    /// flag.
    pub fn set_down(&self, down: bool) {
        self.inner.lock().expect("fake service mutex poisoned").down = down;
    }

    /// Script the *next* `submit()`'s job to fail with `message` verbatim
    /// once it reaches its terminal poll. Consumed by exactly one
    /// `submit()` call; subsequent jobs default back to succeeding.
    pub fn queue_failure(&self, message: impl Into<String>) {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .next_outcome = ScriptedOutcome::Fail(message.into());
    }
}

impl Default for FakeService {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl TranscriptionService for FakeService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        let inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        Ok(ServiceHealth {
            ready: true,
            detail: None,
            model_present: inner.model.present,
            // The fake never simulates a GPU-less host: `Some(false)`
            // matches its default "no runtime downloaded yet" state, and
            // flips to `Some(true)` once a simulated CUDA phase completes
            // (`FakeModelDownload::advance_and_peek`, T13's own convention
            // for `present`/`model_present`).
            cuda_runtime_present: Some(inner.model.cuda_runtime_present),
        })
    }

    async fn submit(&self, _req: SubmitRequest) -> Result<String, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        let job_id = Uuid::new_v4().to_string();
        let outcome = std::mem::replace(&mut inner.next_outcome, ScriptedOutcome::Succeed);
        let timing = inner.timing;
        inner.jobs.insert(
            job_id.clone(),
            ScriptedJob {
                outcome,
                timing,
                polls: 0,
            },
        );
        Ok(job_id)
    }

    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        let job = inner
            .jobs
            .get_mut(job_id)
            .ok_or_else(|| ServiceError::Http {
                status: 404,
                message: format!("unknown job id {job_id}"),
            })?;

        let poll = job.polls;
        job.polls += 1;

        if poll < job.timing.queued_polls {
            return Ok(JobStatus {
                state: JobState::Queued,
                progress: 0.0,
                error_kind: None,
                error_message: None,
            });
        }

        let running_elapsed = poll - job.timing.queued_polls;
        if running_elapsed < job.timing.running_polls {
            let progress = if job.timing.running_polls == 0 {
                1.0
            } else {
                f64::from(running_elapsed) / f64::from(job.timing.running_polls)
            };
            return Ok(JobStatus {
                state: JobState::Running,
                progress,
                error_kind: None,
                error_message: None,
            });
        }

        match &job.outcome {
            ScriptedOutcome::Succeed => Ok(JobStatus {
                state: JobState::Done,
                progress: 1.0,
                error_kind: None,
                error_message: None,
            }),
            ScriptedOutcome::Fail(message) => Ok(JobStatus {
                state: JobState::Failed,
                progress: 1.0,
                error_kind: None,
                error_message: Some(message.clone()),
            }),
        }
    }

    async fn model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.model.advance_and_peek())
    }

    async fn start_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.start();
        Ok(inner.model.peek())
    }

    async fn cancel_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cancel();
        Ok(inner.model.peek())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    fn request() -> SubmitRequest {
        SubmitRequest {
            audio_path: "C:\\Meetings\\ELS\\260812\\source.mp4".to_string(),
            output_dir: "C:\\Meetings\\ELS\\260812".to_string(),
            language: None,
        }
    }

    #[test]
    fn health_on_healthy_fake_returns_ready() {
        run(async {
            let fake = FakeService::new();
            let health = fake.health().await.expect("health should succeed");
            assert!(health.ready);
        });
    }

    #[test]
    fn submit_then_status_walks_queued_running_done_with_nondecreasing_progress() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 1,
                running_polls: 4,
            });
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            let first = fake.status(&job_id).await.expect("status should succeed");
            assert_eq!(first.state, JobState::Queued);

            let mut last_progress = -1.0;
            let mut saw_running = false;
            let mut terminal = None;
            for _ in 0..10 {
                let snapshot = fake.status(&job_id).await.expect("status should succeed");
                match snapshot.state {
                    JobState::Running => {
                        saw_running = true;
                        assert!(
                            snapshot.progress >= last_progress,
                            "progress must never decrease: {} then {}",
                            last_progress,
                            snapshot.progress
                        );
                        last_progress = snapshot.progress;
                    }
                    JobState::Done => {
                        terminal = Some(snapshot);
                        break;
                    }
                    other => panic!("unexpected state before terminal: {other:?}"),
                }
            }

            assert!(saw_running, "job must pass through Running");
            let terminal = terminal.expect("job must reach Done");
            assert_eq!(terminal.progress, 1.0);
            assert_eq!(terminal.error_message, None);
        });
    }

    #[test]
    fn failing_fake_reports_failed_with_verbatim_provider_message() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 1,
            });
            fake.queue_failure("provider exploded: disk full");
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            // poll through Running, then land on the terminal Failed poll.
            let mut terminal = None;
            for _ in 0..5 {
                let snapshot = fake.status(&job_id).await.expect("status should succeed");
                if snapshot.state == JobState::Failed {
                    terminal = Some(snapshot);
                    break;
                }
            }

            let terminal = terminal.expect("job must reach Failed");
            assert_eq!(
                terminal.error_message.as_deref(),
                Some("provider exploded: disk full")
            );
        });
    }

    #[test]
    fn down_fake_rejects_health_and_submit_but_existing_jobs_still_report_status() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            });
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            fake.set_down(true);

            assert!(matches!(
                fake.health().await,
                Err(ServiceError::Unavailable { .. })
            ));
            assert!(matches!(
                fake.submit(request()).await,
                Err(ServiceError::Unavailable { .. })
            ));

            // The job submitted before going down still reports status.
            let snapshot = fake.status(&job_id).await.expect("status should succeed");
            assert_eq!(snapshot.state, JobState::Done);
        });
    }

    #[test]
    fn job_state_from_wire_collapses_five_states_to_four() {
        assert_eq!(JobState::from_wire("queued"), Some(JobState::Queued));
        assert_eq!(JobState::from_wire("running"), Some(JobState::Running));
        assert_eq!(JobState::from_wire("succeeded"), Some(JobState::Done));
        assert_eq!(JobState::from_wire("failed"), Some(JobState::Failed));
        assert_eq!(JobState::from_wire("cancelled"), Some(JobState::Failed));
        assert_eq!(JobState::from_wire("bogus"), None);
    }

    #[test]
    fn job_status_from_wire_forces_cancelled_message_and_passes_others_through() {
        let succeeded = JobStatus::from_wire("succeeded", 1.0, None, None)
            .expect("succeeded must map to a JobStatus");
        assert_eq!(succeeded.state, JobState::Done);
        assert_eq!(succeeded.error_message, None);

        let cancelled = JobStatus::from_wire("cancelled", 0.4, None, None)
            .expect("cancelled must map to a JobStatus");
        assert_eq!(cancelled.state, JobState::Failed);
        assert_eq!(cancelled.error_message.as_deref(), Some("cancelled"));

        let failed = JobStatus::from_wire(
            "failed",
            0.9,
            Some("provider_unavailable".to_string()),
            Some("provider is unavailable".to_string()),
        )
        .expect("failed must map to a JobStatus");
        assert_eq!(failed.state, JobState::Failed);
        assert_eq!(failed.error_kind.as_deref(), Some("provider_unavailable"));
        assert_eq!(
            failed.error_message.as_deref(),
            Some("provider is unavailable")
        );

        assert_eq!(JobState::from_wire("bogus"), None);
        assert_eq!(JobStatus::from_wire("bogus", 0.0, None, None), None);
    }

    // -- model download simulation (T13, FR-12, FR-16, FR-17) -------------

    #[test]
    fn a_default_fake_reports_the_model_present() {
        run(async {
            let fake = FakeService::new();
            let health = fake.health().await.expect("health should succeed");
            assert!(health.model_present);
        });
    }

    #[test]
    fn with_model_absent_reports_idle_and_not_present_until_started() {
        run(async {
            let fake = FakeService::with_model_absent();
            let health = fake.health().await.expect("health should succeed");
            assert!(!health.model_present);
            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(status.state, ModelDownloadState::Idle);
        });
    }

    #[test]
    fn starting_a_download_walks_downloading_to_complete_with_nondecreasing_bytes_and_flips_model_present(
    ) {
        run(async {
            let fake = FakeService::with_model_absent();
            let first = fake
                .start_model_download()
                .await
                .expect("start_model_download should succeed");
            assert_eq!(first.state, ModelDownloadState::Downloading);

            let mut last_downloaded = -1i64;
            let mut terminal = None;
            for _ in 0..10 {
                let status = fake
                    .model_download_status()
                    .await
                    .expect("model_download_status should succeed");
                assert!(
                    i64::try_from(status.downloaded_bytes).unwrap() >= last_downloaded,
                    "downloaded_bytes must never decrease"
                );
                last_downloaded = i64::try_from(status.downloaded_bytes).unwrap();
                if status.state == ModelDownloadState::Complete {
                    terminal = Some(status);
                    break;
                }
            }

            let terminal = terminal.expect("transfer must reach Complete");
            assert_eq!(terminal.downloaded_bytes, terminal.total_bytes);
            let health = fake.health().await.expect("health should succeed");
            assert!(
                health.model_present,
                "model_present must flip true once the simulated transfer completes"
            );
        });
    }

    #[test]
    fn a_second_start_while_downloading_does_not_restart_the_transfer() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");
            let after_one_poll = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert!(after_one_poll.downloaded_bytes > 0);

            // A second start while already downloading is a no-op -- it
            // must not reset progress back to zero (F2: never a parallel
            // transfer).
            let second = fake
                .start_model_download()
                .await
                .expect("start_model_download should succeed");
            assert_eq!(second.state, ModelDownloadState::Downloading);
            assert!(second.downloaded_bytes >= after_one_poll.downloaded_bytes);
        });
    }

    #[test]
    fn cancel_leaves_a_retryable_cancelled_state() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");
            let cancelled = fake
                .cancel_model_download()
                .await
                .expect("cancel_model_download should succeed");
            assert_eq!(cancelled.state, ModelDownloadState::Cancelled);

            // Retryable: starting again begins a fresh transfer.
            let retried = fake
                .start_model_download()
                .await
                .expect("a cancelled transfer must be retryable");
            assert_eq!(retried.state, ModelDownloadState::Downloading);
        });
    }

    #[test]
    fn queue_model_download_failure_reports_a_verbatim_error_and_never_marks_the_model_present() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.queue_model_download_failure();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");

            let mut terminal = None;
            for _ in 0..10 {
                let status = fake
                    .model_download_status()
                    .await
                    .expect("model_download_status should succeed");
                if status.state == ModelDownloadState::Error {
                    terminal = Some(status);
                    break;
                }
            }

            let terminal = terminal.expect("transfer must reach Error");
            assert!(terminal.error_message.is_some());
            let health = fake.health().await.expect("health should succeed");
            assert!(
                !health.model_present,
                "a failed transfer must never be reported as present"
            );
        });
    }

    #[test]
    fn set_cuda_runtime_missing_flips_cuda_runtime_present_without_a_warning() {
        run(async {
            let fake = FakeService::new();

            fake.set_cuda_runtime_missing();

            let health = fake.health().await.expect("health should succeed");
            assert_eq!(health.cuda_runtime_present, Some(false));
            assert!(health.model_present);

            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(
                status.cuda_warning, None,
                "a fresh process's durable CUDA-missing state carries no in-session warning"
            );
        });
    }

    #[test]
    fn set_cuda_warning_flips_cuda_runtime_present_and_carries_the_message_verbatim() {
        run(async {
            let fake = FakeService::new();
            let health_before = fake.health().await.expect("health should succeed");
            assert_eq!(health_before.cuda_runtime_present, Some(true));

            fake.set_cuda_warning("digest mismatch for nvidia_cublas_cu12.whl");

            let health_after = fake.health().await.expect("health should succeed");
            assert_eq!(health_after.cuda_runtime_present, Some(false));
            assert!(health_after.model_present);

            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(
                status.cuda_warning.as_deref(),
                Some("digest mismatch for nvidia_cublas_cu12.whl")
            );
        });
    }
}
