//! The job lifecycle, end to end through a real worker thread and a real
//! ledger -- with a fake runner in place of the engines.
//!
//! This is the port of what `jobs.py`'s pipeline tests covered: that a job
//! moves through the states the UI polls for, that failures and cancellations
//! stay distinguishable, that warnings survive a success, and that a crash in
//! the work does not take the queue with it.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use engine::config::Config;
use engine::fakes::{FakeBehaviour, FakeRunner};
use engine::jobs::{EngineHandle, JobKind, JobRequest, JobRunner, JobState};
use engine::ledger::Ledger;
use wire::ErrorKind;

struct Harness {
    _dir: tempfile::TempDir,
    engine: EngineHandle,
}

impl Drop for Harness {
    fn drop(&mut self) {
        self.engine.shutdown();
    }
}

fn harness_with(
    make_runner: impl Fn() -> Box<dyn JobRunner> + Send + 'static,
    timeout: Option<u64>,
) -> Harness {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut env = engine::config::Env::new();
    env.insert(
        "TRANSCRIBER_APP_DIR".to_string(),
        dir.path().display().to_string(),
    );
    let mut config = Config::load(None, &env).expect("config");
    config.job_timeout_sec = timeout;

    let ledger = Ledger::open(&config.db_path).expect("ledger");
    let engine = EngineHandle::start(config, ledger, Box::new(make_runner)).expect("engine");
    Harness { _dir: dir, engine }
}

fn harness(behaviour: FakeBehaviour) -> Harness {
    harness_with(
        move || Box::new(FakeRunner::new(behaviour.clone())) as Box<dyn JobRunner>,
        None,
    )
}

fn request(kind: JobKind) -> JobRequest {
    JobRequest {
        kind,
        input_path: "C:\\vault\\ELS\\260812 - Demo\\source.mp4".to_string(),
        output_dir: "C:\\vault\\ELS\\260812 - Demo".to_string(),
        language: Some("ru".to_string()),
    }
}

/// Poll the way the desktop app does, with a deadline so a hung engine fails
/// the test instead of hanging it.
fn wait_for_terminal(engine: &EngineHandle, job_id: &str) -> engine::jobs::JobSnapshot {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let snapshot = engine.status(job_id).expect("job is known");
        if snapshot.state.is_terminal() {
            return snapshot;
        }
        assert!(
            Instant::now() < deadline,
            "job {job_id} never reached a terminal state (last: {:?})",
            snapshot.state
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn a_job_runs_to_success_and_lands_in_the_ledger() {
    let h = harness(FakeBehaviour::default());
    let job_id = h
        .engine
        .submit(request(JobKind::Transcribe))
        .expect("submit");

    let snapshot = wait_for_terminal(&h.engine, &job_id);
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.progress, 1.0);
    assert!(snapshot.error_kind.is_none());

    let rows = h.engine.list_ledger_jobs(None).expect("ledger");
    let row = rows
        .iter()
        .find(|r| r.job_id == job_id)
        .expect("ledger row");
    assert_eq!(row.status, "succeeded");
    assert_eq!(row.job_type, "transcribe");
    assert_eq!(row.device.as_deref(), Some("fake"));
    assert_eq!(row.segment_count, Some(3));
    assert!(row.elapsed_sec.is_some());
}

#[test]
fn every_job_kind_records_its_own_wire_name() {
    let h = harness(FakeBehaviour::default());
    for kind in [
        JobKind::Transcribe,
        JobKind::Summarize,
        JobKind::ActionItems,
        JobKind::Facts,
        JobKind::Report,
        JobKind::Export,
    ] {
        let job_id = h.engine.submit(request(kind)).expect("submit");
        assert_eq!(
            wait_for_terminal(&h.engine, &job_id).state,
            JobState::Succeeded
        );
        let rows = h.engine.list_ledger_jobs(None).expect("ledger");
        let row = rows.iter().find(|r| r.job_id == job_id).expect("row");
        assert_eq!(row.job_type, kind.wire_name());
    }
}

#[test]
fn a_failure_is_attributed_verbatim() {
    let h = harness(FakeBehaviour::Fail(
        ErrorKind::AudioDecode,
        "could not decode source.mp4".to_string(),
    ));
    let job_id = h
        .engine
        .submit(request(JobKind::Transcribe))
        .expect("submit");

    let snapshot = wait_for_terminal(&h.engine, &job_id);
    assert_eq!(snapshot.state, JobState::Failed);
    assert_eq!(snapshot.error_kind, Some(ErrorKind::AudioDecode));
    assert_eq!(
        snapshot.error_message.as_deref(),
        Some("could not decode source.mp4"),
        "the engine's own words must reach the UI unrewritten"
    );

    let rows = h.engine.list_ledger_jobs(None).expect("ledger");
    let row = rows.iter().find(|r| r.job_id == job_id).expect("row");
    assert_eq!(row.error_kind.as_deref(), Some("audio_decode"));
}

#[test]
fn warnings_survive_a_successful_job() {
    // A failed screenshot pass or an unrenderable PDF degrades the result
    // without failing it; the caller still has to see it.
    let h = harness_with(
        || {
            Box::new(
                FakeRunner::new(FakeBehaviour::default())
                    .with_warnings(vec!["pdf render failed".to_string()]),
            ) as Box<dyn JobRunner>
        },
        None,
    );
    let job_id = h.engine.submit(request(JobKind::Export)).expect("submit");

    let snapshot = wait_for_terminal(&h.engine, &job_id);
    assert_eq!(snapshot.state, JobState::Succeeded);
    assert_eq!(snapshot.warnings, vec!["pdf render failed".to_string()]);
}

#[test]
fn cancelling_a_running_job_ends_it_as_cancelled_not_failed() {
    let h = harness(FakeBehaviour::Hang);
    let job_id = h
        .engine
        .submit(request(JobKind::Transcribe))
        .expect("submit");

    // Wait until it is actually running, so this tests the running path.
    let deadline = Instant::now() + Duration::from_secs(5);
    while h.engine.status(&job_id).expect("known").state != JobState::Running {
        assert!(Instant::now() < deadline, "job never started");
        std::thread::sleep(Duration::from_millis(5));
    }

    h.engine.cancel(&job_id).expect("cancel");
    let snapshot = wait_for_terminal(&h.engine, &job_id);
    assert_eq!(snapshot.state, JobState::Cancelled);
    assert_eq!(snapshot.error_message.as_deref(), Some("cancelled"));

    let rows = h.engine.list_ledger_jobs(None).expect("ledger");
    let row = rows.iter().find(|r| r.job_id == job_id).expect("row");
    assert_eq!(row.status, "cancelled");
    assert_eq!(row.error_kind.as_deref(), Some("cancelled"));
}

#[test]
fn cancelling_an_unknown_job_is_an_error_not_a_silent_success() {
    let h = harness(FakeBehaviour::default());
    assert!(h.engine.cancel("job-does-not-exist").is_err());
}

#[test]
fn a_job_that_outruns_its_deadline_fails_as_a_timeout() {
    let h = harness_with(
        || Box::new(FakeRunner::new(FakeBehaviour::Hang)) as Box<dyn JobRunner>,
        Some(1),
    );
    let job_id = h
        .engine
        .submit(request(JobKind::Transcribe))
        .expect("submit");

    let snapshot = wait_for_terminal(&h.engine, &job_id);
    assert_eq!(snapshot.state, JobState::Failed);
    assert_eq!(
        snapshot.error_kind,
        Some(ErrorKind::Timeout),
        "a deadline that fired reads as a timeout, not as a user's cancellation"
    );
}

#[test]
fn a_panicking_job_fails_alone_and_the_runner_is_rebuilt() {
    // The containment contract: a panic through the runner must not take the
    // worker with it, and the next job must not inherit a half-initialised
    // engine -- so the runner is rebuilt rather than reused.
    let builds = Arc::new(AtomicUsize::new(0));
    let behaviours = Arc::new(std::sync::Mutex::new(vec![
        FakeBehaviour::default(),
        FakeBehaviour::Panic,
    ]));

    let h = {
        let builds = Arc::clone(&builds);
        let behaviours = Arc::clone(&behaviours);
        harness_with(
            move || {
                // First build panics, every later build behaves.
                let behaviour = behaviours
                    .lock()
                    .expect("behaviours")
                    .pop()
                    .unwrap_or_default();
                Box::new(FakeRunner::new(behaviour).counting_builds(Arc::clone(&builds)))
                    as Box<dyn JobRunner>
            },
            None,
        )
    };

    let panicking = h
        .engine
        .submit(request(JobKind::Transcribe))
        .expect("submit");
    let snapshot = wait_for_terminal(&h.engine, &panicking);
    assert_eq!(snapshot.state, JobState::Failed);
    assert_eq!(snapshot.error_kind, Some(ErrorKind::Internal));

    let after = h
        .engine
        .submit(request(JobKind::Summarize))
        .expect("submit");
    assert_eq!(
        wait_for_terminal(&h.engine, &after).state,
        JobState::Succeeded,
        "the queue must survive a panicking job"
    );
    assert_eq!(
        builds.load(Ordering::SeqCst),
        2,
        "the panic should have forced exactly one rebuild"
    );
}

#[test]
fn jobs_run_one_at_a_time_in_submission_order() {
    // The invariant that keeps a whisper model and a multi-gigabyte LLM from
    // being resident together.
    let h = harness(FakeBehaviour::Succeed {
        steps: 2,
        step_delay: Duration::from_millis(5),
    });

    let ids: Vec<String> = (0..3)
        .map(|_| {
            h.engine
                .submit(request(JobKind::Transcribe))
                .expect("submit")
        })
        .collect();

    // At most one job is ever running.
    let deadline = Instant::now() + Duration::from_secs(10);
    while ids
        .iter()
        .any(|id| !h.engine.status(id).expect("known").state.is_terminal())
    {
        let running = ids
            .iter()
            .filter(|id| h.engine.status(id).expect("known").state == JobState::Running)
            .count();
        assert!(running <= 1, "{running} jobs ran at once");
        assert!(Instant::now() < deadline, "jobs never finished");
        std::thread::sleep(Duration::from_millis(2));
    }

    for id in &ids {
        assert_eq!(
            h.engine.status(id).expect("known").state,
            JobState::Succeeded
        );
    }
}

#[test]
fn an_unknown_job_id_has_no_status() {
    let h = harness(FakeBehaviour::default());
    assert!(h.engine.status("job-nope").is_none());
}

#[test]
fn a_crash_left_running_row_is_reconciled_at_startup() {
    // What replaces the sidecar's ability to die alone: a job left `running`
    // by a process that never came back is attributed on the next launch.
    let dir = tempfile::tempdir().expect("tempdir");
    let db_path = dir.path().join("data").join("jobs.sqlite3");
    {
        let ledger = Ledger::open(&db_path).expect("ledger");
        ledger
            .insert_job(&engine::ledger::NewJob {
                job_id: "interrupted",
                job_type: "transcribe",
                provider: "local",
                model: "large-v3",
                device: "cuda",
                source_path: "a.mp4",
                output_path: "out",
                language: None,
                meeting_json: None,
            })
            .expect("insert");
        ledger.mark_running("interrupted", None).expect("running");
    }

    let mut env = engine::config::Env::new();
    env.insert(
        "TRANSCRIBER_APP_DIR".to_string(),
        dir.path().display().to_string(),
    );
    let config = Config::load(None, &env).expect("config");
    let ledger = Ledger::open(&config.db_path).expect("reopen");
    let handle = EngineHandle::start(
        config,
        ledger,
        Box::new(|| Box::new(FakeRunner::default()) as Box<dyn JobRunner>),
    )
    .expect("engine");

    let rows = handle.list_ledger_jobs(None).expect("ledger");
    let row = rows
        .iter()
        .find(|r| r.job_id == "interrupted")
        .expect("row");
    assert_eq!(row.status, "failed");
    assert_eq!(row.error_kind.as_deref(), Some("internal"));
    handle.shutdown();
}

#[test]
fn submitting_after_shutdown_is_refused() {
    let h = harness(FakeBehaviour::default());
    h.engine.shutdown();
    assert!(h.engine.submit(request(JobKind::Transcribe)).is_err());
}
