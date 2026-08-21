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
    JobState, JobStatus, ServiceError, ServiceHealth, SubmitRequest, TranscriptionService,
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

struct Inner {
    down: bool,
    timing: FakeTiming,
    next_outcome: ScriptedOutcome,
    jobs: HashMap<String, ScriptedJob>,
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

    /// A healthy fake whose jobs succeed, on the given timing.
    pub fn with_timing(timing: FakeTiming) -> Self {
        FakeService {
            inner: Mutex::new(Inner {
                down: false,
                timing,
                next_outcome: ScriptedOutcome::Succeed,
                jobs: HashMap::new(),
            }),
        }
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
}
