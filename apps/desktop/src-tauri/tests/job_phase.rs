//! T5 (F3 "honest job progress"): the Rust seam carries the service's new
//! `phase` label and its now-nullable `progress` from `GET /v1/jobs/{id}`
//! all the way to the `JobSnapshot` the UI receives (FR-7).
//!
//! Everything here is driven through the crate's public surface
//! (`service::JobStatus`, `service::http::HttpTranscriptionService`,
//! `service::fake::FakeService`, `jobs::JobRegistry`) exactly the way
//! `tests/e2e_flow.rs` drives it: no Tauri runtime, no real F2 sidecar, and
//! no assertion on a private field. The only test double is a scripted
//! `TranscriptionService` -- the seam's own out-of-process boundary, which
//! is what this task's contract is about -- plus `wiremock` standing in for
//! F2's HTTP surface.
//!
//! Deliberately self-contained (no `mod common`): the helpers below are the
//! three lines of this file's own harness, and pulling in the e2e harness
//! would compile its unused pieces into this binary.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use wiremock::matchers::{method, path as path_matcher};
use wiremock::{Mock, MockServer, ResponseTemplate};

use transcriber_desktop_lib::jobs::{EventSink, JobRegistry, JobSnapshot, JobState};
use transcriber_desktop_lib::service::fake::{FakeService, FakeTiming};
use transcriber_desktop_lib::service::http::HttpTranscriptionService;
use transcriber_desktop_lib::service::{
    JobState as ServiceJobState, JobStatus, ServiceError, ServiceHealth, SubmitRequest,
    TranscriptionService,
};

// -- harness ---------------------------------------------------------------

/// Runs `future` on a fresh multi-thread Tokio runtime — this crate's
/// `tokio` dependency does not enable the `macros` feature, so
/// `#[tokio::test]` is unavailable (same pattern as `tests/e2e_flow.rs`).
fn run<F: std::future::Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime")
        .block_on(future)
}

/// Records every emitted `jobs://updated` snapshot in order, standing in
/// for the Tauri `AppHandle` emitter used in production.
#[derive(Default)]
struct RecordingSink {
    events: Mutex<Vec<JobSnapshot>>,
}

impl RecordingSink {
    fn snapshots(&self) -> Vec<JobSnapshot> {
        self.events
            .lock()
            .expect("recording sink mutex poisoned")
            .clone()
    }
}

impl EventSink for RecordingSink {
    fn emit(&self, snapshot: &JobSnapshot) {
        self.events
            .lock()
            .expect("recording sink mutex poisoned")
            .push(snapshot.clone());
    }
}

fn write_recording(dir: &Path, name: &str) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, b"recording bytes").expect("write fixture recording");
    path
}

fn submit_request() -> SubmitRequest {
    SubmitRequest {
        audio_path: "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4".to_string(),
        output_dir: "C:\\Meetings\\ELS\\260812 - Security issue".to_string(),
        language: None,
        original_file_name: None,
        max_speakers: None,
        speaker_match_threshold: None,
    }
}

/// Polls `registry` until `id` reaches a terminal state, panicking on
/// timeout so a broken poll loop fails loudly instead of hanging.
async fn wait_for_terminal(registry: &JobRegistry, id: &str, timeout: Duration) -> JobSnapshot {
    let start = Instant::now();
    loop {
        if let Some(snapshot) = registry.get(id).await {
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
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

/// Enqueues one recording against a `FakeService` and hands back the
/// `Pending` snapshot the registry answered with — the only way to obtain a
/// real `JobSnapshot` from outside the crate, used by the serialisation
/// tests below so they never hand-write a struct literal that would have to
/// be edited every time the IPC contract grows a field.
async fn a_pending_snapshot() -> JobSnapshot {
    let root = tempfile::tempdir().expect("tempdir");
    let downloads = tempfile::tempdir().expect("tempdir");
    let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

    let registry = JobRegistry::with_poll_interval(
        root.path().to_path_buf(),
        Arc::new(FakeService::new()),
        Arc::new(RecordingSink::default()),
        Duration::from_millis(5),
    );
    let mut snapshots = registry.enqueue(vec![source]).await;
    snapshots.pop().expect("enqueue answers one snapshot")
}

/// A `TranscriptionService` that replays a scripted list of `JobStatus`
/// values, one per `status()` poll (repeating the last one once the script
/// runs out). Stands in for F2 itself — the seam's out-of-process
/// dependency — so a sequence of polls that differ *only* in `phase` can be
/// produced deterministically, with no sleeps and no real service.
struct ScriptedStatusService {
    statuses: Mutex<VecDeque<JobStatus>>,
    last: Mutex<Option<JobStatus>>,
}

impl ScriptedStatusService {
    fn new(statuses: Vec<JobStatus>) -> Self {
        ScriptedStatusService {
            statuses: Mutex::new(statuses.into()),
            last: Mutex::new(None),
        }
    }
}

#[async_trait]
impl TranscriptionService for ScriptedStatusService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        Ok(ServiceHealth {
            ready: true,
            detail: None,
            model_present: true,
            cuda_runtime_present: Some(true),
            llm_model_present: Some(true),
            llm_gpu_build_present: Some(true),
            embedding_model_present: Some(true),
        })
    }

    async fn submit(&self, _req: SubmitRequest) -> Result<String, ServiceError> {
        Ok("scripted-job".to_string())
    }

    async fn status(&self, _job_id: &str) -> Result<JobStatus, ServiceError> {
        let next = self
            .statuses
            .lock()
            .expect("scripted statuses mutex poisoned")
            .pop_front();
        let mut last = self.last.lock().expect("scripted last mutex poisoned");
        if let Some(status) = next {
            *last = Some(status);
        }
        Ok(last
            .clone()
            .expect("the script must supply at least one status"))
    }
}

// -- FR-7: `JobStatus::from_wire` --------------------------------------------

#[test]
fn a_status_with_no_linear_signal_carries_a_null_progress_and_its_phase_label() {
    let status = JobStatus::from_wire(
        "running",
        None,
        Some("rendering PDF".to_string()),
        None,
        None,
    )
    .expect("`running` is a status F2 documents");

    assert_eq!(status.state, ServiceJobState::Running);
    assert_eq!(status.progress, None);
    assert_eq!(status.phase.as_deref(), Some("rendering PDF"));
}

#[test]
fn a_cancelled_job_still_gets_the_cancelled_message_alongside_a_numeric_progress() {
    let status = JobStatus::from_wire("cancelled", Some(0.3), None, None, None)
        .expect("`cancelled` is a status F2 documents");

    assert_eq!(status.state, ServiceJobState::Failed);
    assert_eq!(status.progress, Some(0.3));
    assert_eq!(status.phase, None);
    assert_eq!(status.error_message.as_deref(), Some("cancelled"));
}

// -- FR-7: `HttpTranscriptionService::status` decoding ------------------------

#[test]
fn status_decodes_a_null_progress_and_a_phase_from_the_service() {
    run(async {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path_matcher("/v1/jobs/job-export"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "job_id": "job-export",
                "status": "running",
                "progress": serde_json::Value::Null,
                "phase": "rendering PDF",
            })))
            .mount(&server)
            .await;

        let service = HttpTranscriptionService::new(&server.uri(), None)
            .expect("loopback base url must be accepted");

        let status = service
            .status("job-export")
            .await
            .expect("a null progress must decode, not fail the poll");

        assert_eq!(status.progress, None);
        assert_eq!(status.phase.as_deref(), Some("rendering PDF"));
    });
}

#[test]
fn status_from_an_older_service_without_a_phase_key_decodes_to_no_phase() {
    run(async {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path_matcher("/v1/jobs/job-legacy"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "job_id": "job-legacy",
                "status": "running",
                "progress": 0.5,
            })))
            .mount(&server)
            .await;

        let service = HttpTranscriptionService::new(&server.uri(), None)
            .expect("loopback base url must be accepted");

        let status = service
            .status("job-legacy")
            .await
            .expect("a body with no phase key must still decode");

        assert_eq!(status.progress, Some(0.5));
        assert_eq!(status.phase, None);
    });
}

// -- FR-7: the fake service keeps its scripted walk ---------------------------

#[test]
fn the_fake_service_walks_queued_running_done_with_numeric_progress_and_no_phase() {
    run(async {
        let fake = FakeService::with_timing(FakeTiming {
            queued_polls: 1,
            running_polls: 2,
        });
        let job_id = fake
            .submit(submit_request())
            .await
            .expect("the healthy fake accepts a submission");

        let mut observed_states = Vec::new();
        let mut last_progress = -1.0_f64;
        for _ in 0..4 {
            let status = fake.status(&job_id).await.expect("status should succeed");
            let progress = status
                .progress
                .expect("the fake reports a number for every poll");
            assert!(
                progress >= last_progress,
                "progress must never decrease: {last_progress} then {progress}"
            );
            last_progress = progress;
            assert_eq!(
                status.phase, None,
                "the fake scripts no phases -- that is the service's job"
            );
            observed_states.push(status.state);
        }

        assert_eq!(
            observed_states,
            vec![
                ServiceJobState::Queued,
                ServiceJobState::Running,
                ServiceJobState::Running,
                ServiceJobState::Done,
            ]
        );
        assert_eq!(last_progress, 1.0);
    });
}

// -- FR-7: the poll loop propagates a phase-only change -----------------------

#[test]
fn a_poll_whose_only_change_is_the_phase_is_still_emitted_to_the_event_sink() {
    // The dedupe in the poll loop drops a snapshot identical to the one on
    // record. A summarize job holds `progress: null` and the same `running`
    // state for its whole life, so if the phase is not part of that
    // comparison every label after the first one is swallowed and the UI
    // never moves past "reading transcript".
    run(async {
        let root = tempfile::tempdir().expect("tempdir");
        let downloads = tempfile::tempdir().expect("tempdir");
        let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

        let service = Arc::new(ScriptedStatusService::new(vec![
            JobStatus {
                state: ServiceJobState::Running,
                progress: None,
                phase: Some("reading transcript".to_string()),
                error_kind: None,
                error_message: None,
            },
            JobStatus {
                state: ServiceJobState::Running,
                progress: None,
                phase: Some("reading transcript".to_string()),
                error_kind: None,
                error_message: None,
            },
            JobStatus {
                state: ServiceJobState::Running,
                progress: None,
                phase: Some("writing summary · 12 tokens".to_string()),
                error_kind: None,
                error_message: None,
            },
            JobStatus {
                state: ServiceJobState::Done,
                progress: Some(1.0),
                phase: None,
                error_kind: None,
                error_message: None,
            },
        ]));
        let sink = Arc::new(RecordingSink::default());
        let registry = JobRegistry::with_poll_interval(
            root.path().to_path_buf(),
            service,
            sink.clone(),
            Duration::from_millis(5),
        );

        let snapshots = registry.enqueue(vec![source]).await;
        wait_for_terminal(&registry, &snapshots[0].id, Duration::from_secs(5)).await;

        let running_phases: Vec<Option<String>> = sink
            .snapshots()
            .iter()
            .filter(|snapshot| snapshot.state == JobState::Running)
            .map(|snapshot| snapshot.phase.clone())
            .collect();
        assert_eq!(
            running_phases,
            vec![
                Some("reading transcript".to_string()),
                Some("writing summary · 12 tokens".to_string()),
            ],
            "one emission per distinct phase: the repeated poll is still deduped, \
             the new label is not"
        );
    });
}

#[test]
fn a_finished_job_ends_with_the_services_full_progress_and_no_phase() {
    run(async {
        let root = tempfile::tempdir().expect("tempdir");
        let downloads = tempfile::tempdir().expect("tempdir");
        let source = write_recording(downloads.path(), "ELS - 260812 - Security issue.mp4");

        let service = Arc::new(ScriptedStatusService::new(vec![
            JobStatus {
                state: ServiceJobState::Running,
                progress: None,
                phase: Some("rendering PDF".to_string()),
                error_kind: None,
                error_message: None,
            },
            JobStatus {
                state: ServiceJobState::Done,
                progress: Some(1.0),
                phase: None,
                error_kind: None,
                error_message: None,
            },
        ]));
        let registry = JobRegistry::with_poll_interval(
            root.path().to_path_buf(),
            service,
            Arc::new(RecordingSink::default()),
            Duration::from_millis(5),
        );

        let snapshots = registry.enqueue(vec![source]).await;
        let terminal = wait_for_terminal(&registry, &snapshots[0].id, Duration::from_secs(5)).await;

        assert_eq!(terminal.state, JobState::Done);
        assert_eq!(terminal.progress, Some(1.0));
        assert_eq!(terminal.phase, None);
    });
}

// -- FR-7: the IPC shape the UI receives -------------------------------------

#[test]
fn a_job_snapshot_without_a_phase_puts_null_on_the_wire_under_phase() {
    run(async {
        let snapshot = a_pending_snapshot().await;

        let wire = serde_json::to_value(&snapshot).expect("a snapshot must serialise");

        assert_eq!(
            wire.get("phase"),
            Some(&serde_json::Value::Null),
            "the key must always be present so the UI can read it unconditionally"
        );
    });
}

#[test]
fn a_job_snapshot_puts_its_phase_on_the_wire_under_phase() {
    run(async {
        let snapshot = JobSnapshot {
            phase: Some("segmenting speech".to_string()),
            ..a_pending_snapshot().await
        };

        let wire = serde_json::to_value(&snapshot).expect("a snapshot must serialise");

        assert_eq!(
            wire.get("phase").and_then(serde_json::Value::as_str),
            Some("segmenting speech")
        );
    });
}
