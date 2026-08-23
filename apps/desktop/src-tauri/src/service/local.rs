//! [`TranscriptionService`] over the in-process engine.
//!
//! The same seam `http.rs` binds to a loopback HTTP service, bound instead to
//! a function call. That is the whole point of having kept the trait: the job
//! registry, the thirty commands and the entire frontend cannot tell the
//! difference, so replacing the Python sidecar changes what is behind the
//! seam and nothing in front of it.
//!
//! Two things are genuinely different and worth naming:
//!
//! - There is no "unavailable" to report. The engine is a thread in this
//!   process; if it is constructed, it answers. `ServiceHealth::ready` is
//!   therefore always true, and what used to mean "the sidecar is down" is now
//!   expressed as a job failing.
//! - The five-state to four-state collapse still happens, in
//!   [`JobStatus::from_wire`], because the seam's vocabulary is unchanged.

use async_trait::async_trait;
use engine::jobs::{EngineError, EngineHandle, JobKind, JobRequest};
use engine::models;

use super::{
    JobStatus, LedgerJob, LlmJobKind, LlmSubmitRequest, ServiceError, ServiceHealth, SubmitRequest,
    TranscriptionService,
};

/// Runs jobs on the engine owned by this process.
pub struct LocalTranscriptionService {
    engine: EngineHandle,
}

impl LocalTranscriptionService {
    pub fn new(engine: EngineHandle) -> Self {
        LocalTranscriptionService { engine }
    }
}

fn map_kind(kind: LlmJobKind) -> JobKind {
    match kind {
        LlmJobKind::Summarize => JobKind::Summarize,
        LlmJobKind::ActionItems => JobKind::ActionItems,
        LlmJobKind::Facts => JobKind::Facts,
        LlmJobKind::Report => JobKind::Report,
        LlmJobKind::Export => JobKind::Export,
    }
}

/// Engine errors that reach the seam are local faults, never transport ones.
fn map_error(error: EngineError) -> ServiceError {
    match error {
        EngineError::UnknownJob(id) => ServiceError::Http {
            // The HTTP binding answered 404 for this; keeping the shape means
            // the UI's existing handling still applies.
            status: 404,
            message: format!("no such job: {id}"),
        },
        other => ServiceError::Unavailable {
            detail: other.to_string(),
        },
    }
}

#[async_trait]
impl TranscriptionService for LocalTranscriptionService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        let config = self.engine.config();
        Ok(ServiceHealth {
            ready: true,
            detail: None,
            model_present: models::is_installed(&models::whisper_model_file(config)),
            // Reported once the GPU runtime payload and its downloader exist;
            // `None` is "do not offer it", which is the honest answer while
            // there is nothing to offer.
            cuda_runtime_present: None,
            llm_model_present: Some(models::is_installed(&models::llm_model_file(config))),
            llm_gpu_build_present: None,
        })
    }

    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
        let engine = self.engine.clone();
        // Submitting writes the ledger row, so it goes to a blocking thread
        // rather than stalling the async runtime on a sqlite commit.
        tokio::task::spawn_blocking(move || {
            engine.submit(JobRequest {
                kind: JobKind::Transcribe,
                input_path: req.audio_path,
                output_dir: req.output_dir,
                language: req.language,
            })
        })
        .await
        .map_err(|e| ServiceError::Unavailable {
            detail: format!("engine submit task failed: {e}"),
        })?
        .map_err(map_error)
    }

    async fn submit_llm(&self, req: LlmSubmitRequest) -> Result<String, ServiceError> {
        let engine = self.engine.clone();
        tokio::task::spawn_blocking(move || {
            engine.submit(JobRequest {
                kind: map_kind(req.kind),
                input_path: req.input_path,
                output_dir: req.output_dir,
                language: None,
            })
        })
        .await
        .map_err(|e| ServiceError::Unavailable {
            detail: format!("engine submit task failed: {e}"),
        })?
        .map_err(map_error)
    }

    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError> {
        // A read of an in-memory table; no reason to leave the runtime.
        let snapshot = self
            .engine
            .status(job_id)
            .ok_or_else(|| map_error(EngineError::UnknownJob(job_id.to_string())))?;

        JobStatus::from_wire(
            snapshot.state.wire_name(),
            snapshot.progress,
            snapshot.error_kind.map(|k| k.as_str().to_string()),
            snapshot.error_message,
        )
        .ok_or_else(|| ServiceError::Decode {
            message: format!("unrecognised job state {:?}", snapshot.state),
        })
    }

    async fn cancel(&self, job_id: &str) -> Result<(), ServiceError> {
        self.engine.cancel(job_id).map_err(map_error)
    }

    async fn list_ledger_jobs(&self, limit: u32) -> Result<Vec<LedgerJob>, ServiceError> {
        let engine = self.engine.clone();
        let rows = tokio::task::spawn_blocking(move || engine.list_ledger_jobs(Some(limit)))
            .await
            .map_err(|e| ServiceError::Unavailable {
                detail: format!("engine ledger task failed: {e}"),
            })?
            .map_err(map_error)?;

        Ok(rows
            .into_iter()
            .map(|row| LedgerJob {
                job_id: row.job_id,
                status: row.status,
                created_at: row.created_at,
                started_at: row.started_at,
                finished_at: row.finished_at,
                provider: row.provider,
                model: row.model,
                device: row.device,
                source_path: row.source_path,
                output_path: row.output_path,
                audio_duration_sec: row.audio_duration_sec,
                elapsed_sec: row.elapsed_sec,
                realtime_factor: row.realtime_factor,
                language: row.language,
                segment_count: row.segment_count,
                error_kind: row.error_kind,
                error_message: row.error_message,
                service_version: row.service_version,
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine::config::Config;
    use engine::fakes::{FakeBehaviour, FakeRunner};
    use engine::jobs::JobRunner;
    use engine::ledger::Ledger;
    use std::time::{Duration, Instant};

    use crate::service::JobState;

    struct Harness {
        _dir: tempfile::TempDir,
        service: LocalTranscriptionService,
        engine: EngineHandle,
    }

    impl Drop for Harness {
        fn drop(&mut self) {
            self.engine.shutdown();
        }
    }

    fn harness(behaviour: FakeBehaviour) -> Harness {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut env = engine::config::Env::new();
        env.insert(
            "TRANSCRIBER_APP_DIR".to_string(),
            dir.path().display().to_string(),
        );
        let config = Config::load(None, &env).expect("config");
        let ledger = Ledger::open(&config.db_path).expect("ledger");
        let engine = EngineHandle::start(
            config,
            ledger,
            Box::new(move || Box::new(FakeRunner::new(behaviour.clone())) as Box<dyn JobRunner>),
        )
        .expect("engine");

        Harness {
            _dir: dir,
            service: LocalTranscriptionService::new(engine.clone()),
            engine,
        }
    }

    fn submit_request() -> SubmitRequest {
        SubmitRequest {
            audio_path: "C:\\vault\\ELS\\260812 - Demo\\source.mp4".to_string(),
            output_dir: "C:\\vault\\ELS\\260812 - Demo".to_string(),
            language: Some("ru".to_string()),
        }
    }

    async fn wait_done(service: &LocalTranscriptionService, job_id: &str) -> JobStatus {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let status = service.status(job_id).await.expect("status");
            if matches!(status.state, JobState::Done | JobState::Failed) {
                return status;
            }
            assert!(Instant::now() < deadline, "job never finished");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    #[tokio::test]
    async fn health_is_ready_because_the_engine_is_this_process() {
        let h = harness(FakeBehaviour::default());
        let health = h.service.health().await.expect("health");
        assert!(health.ready);
        // Nothing downloaded in a fresh app folder.
        assert!(!health.model_present);
        assert_eq!(health.llm_model_present, Some(false));
    }

    #[tokio::test]
    async fn health_sees_a_model_only_once_it_is_marked_ready() {
        let h = harness(FakeBehaviour::default());
        let path = models::whisper_model_file(h.engine.config());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"weights").unwrap();
        assert!(
            !h.service.health().await.unwrap().model_present,
            "an unverified file must not count as installed"
        );

        models::mark_installed(&path).unwrap();
        assert!(h.service.health().await.unwrap().model_present);
    }

    #[tokio::test]
    async fn a_transcription_runs_through_the_seam() {
        let h = harness(FakeBehaviour::default());
        let job_id = h.service.submit(submit_request()).await.expect("submit");

        let status = wait_done(&h.service, &job_id).await;
        assert_eq!(status.state, JobState::Done);
        assert_eq!(status.progress, 1.0);
    }

    #[tokio::test]
    async fn every_llm_job_kind_reaches_the_engine() {
        let h = harness(FakeBehaviour::default());
        for kind in [
            LlmJobKind::Summarize,
            LlmJobKind::ActionItems,
            LlmJobKind::Facts,
            LlmJobKind::Report,
            LlmJobKind::Export,
        ] {
            let job_id = h
                .service
                .submit_llm(LlmSubmitRequest {
                    kind,
                    input_path: "C:\\vault\\ELS\\260812 - Demo".to_string(),
                    output_dir: "C:\\vault\\ELS\\260812 - Demo".to_string(),
                })
                .await
                .expect("submit_llm");
            assert_eq!(wait_done(&h.service, &job_id).await.state, JobState::Done);

            let rows = h.service.list_ledger_jobs(50).await.expect("ledger");
            let row = rows.iter().find(|r| r.job_id == job_id).expect("row");
            assert_eq!(row.status, "succeeded");
        }
    }

    #[tokio::test]
    async fn a_failed_job_keeps_the_engines_own_words() {
        let h = harness(FakeBehaviour::Fail(
            wire::ErrorKind::AudioDecode,
            "could not decode source.mp4".to_string(),
        ));
        let job_id = h.service.submit(submit_request()).await.expect("submit");

        let status = wait_done(&h.service, &job_id).await;
        assert_eq!(status.state, JobState::Failed);
        assert_eq!(status.error_kind.as_deref(), Some("audio_decode"));
        assert_eq!(
            status.error_message.as_deref(),
            Some("could not decode source.mp4")
        );
    }

    #[tokio::test]
    async fn a_cancelled_job_collapses_to_failed_with_a_message_to_show() {
        // The seam has four states, the engine five: `cancelled` folds onto
        // `Failed`, and the message is what tells the two apart in the UI.
        let h = harness(FakeBehaviour::Hang);
        let job_id = h.service.submit(submit_request()).await.expect("submit");

        let deadline = Instant::now() + Duration::from_secs(5);
        while h.service.status(&job_id).await.expect("status").state != JobState::Running {
            assert!(Instant::now() < deadline, "job never started");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }

        h.service.cancel(&job_id).await.expect("cancel");
        let status = wait_done(&h.service, &job_id).await;
        assert_eq!(status.state, JobState::Failed);
        assert_eq!(status.error_message.as_deref(), Some("cancelled"));
    }

    #[tokio::test]
    async fn an_unknown_job_reads_as_a_404_not_a_dead_service() {
        let h = harness(FakeBehaviour::default());
        assert!(matches!(
            h.service.status("job-nope").await,
            Err(ServiceError::Http { status: 404, .. })
        ));
        assert!(matches!(
            h.service.cancel("job-nope").await,
            Err(ServiceError::Http { status: 404, .. })
        ));
    }

    #[tokio::test]
    async fn the_ledger_panel_sees_finished_jobs_newest_first() {
        let h = harness(FakeBehaviour::default());
        let first = h.service.submit(submit_request()).await.expect("submit");
        wait_done(&h.service, &first).await;
        let second = h.service.submit(submit_request()).await.expect("submit");
        wait_done(&h.service, &second).await;

        let rows = h.service.list_ledger_jobs(10).await.expect("ledger");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].job_id, second);
        assert_eq!(rows[0].device.as_deref(), Some("fake"));
    }
}
