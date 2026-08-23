//! The job queue: one worker, one job at a time.
//!
//! Port of the lifecycle in `services/transcription/src/transcription/jobs.py`,
//! with the concurrency model the in-process design needs.
//!
//! **One dedicated worker thread owns every model.** Whisper contexts, llama
//! contexts and ONNX sessions are created on it and never leave it, so nothing
//! has to be `Send` and there is no shared mutable engine state to lock. It
//! also preserves the invariant the Python service had for free: one job at a
//! time, so a whisper model and a multi-gigabyte LLM are never resident
//! together unless asked to be. Work reaches the thread as messages; results
//! come back through a table the poll path reads.
//!
//! **Panics are contained, native crashes are not.** Every job runs inside
//! `catch_unwind`, and a caught panic fails that job and then *rebuilds* the
//! runner, because a panic through FFI can leave a model in an undefined
//! state. What this cannot catch is a segfault or an abort inside
//! whisper.cpp, llama.cpp or a CUDA driver: those end the process. That is the
//! price of dropping the sidecar, and the mitigation is
//! [`Ledger::reconcile_interrupted`], which turns a job left `running` by a
//! crash into an attributed failure on the next launch instead of a job that
//! never ends.

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex, RwLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use wire::ErrorKind;

use crate::config::Config;
use crate::ledger::{Ledger, LedgerRow, NewJob, Success};

/// How often the worker wakes to notice a shutdown request while idle.
const IDLE_TICK: Duration = Duration::from_millis(100);

/// What kind of work a job is. Wire names match the Python service's
/// `JobType`, because they are stored in the ledger's `job_type` column.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum JobKind {
    Transcribe,
    Summarize,
    ActionItems,
    Facts,
    Report,
    Export,
}

impl JobKind {
    pub fn wire_name(self) -> &'static str {
        match self {
            JobKind::Transcribe => "transcribe",
            JobKind::Summarize => "summarize",
            JobKind::ActionItems => "action_items",
            JobKind::Facts => "facts",
            JobKind::Report => "report",
            JobKind::Export => "export",
        }
    }

    pub fn from_wire(name: &str) -> Option<Self> {
        Some(match name {
            "transcribe" => JobKind::Transcribe,
            "summarize" => JobKind::Summarize,
            "action_items" => JobKind::ActionItems,
            "facts" => JobKind::Facts,
            "report" => JobKind::Report,
            "export" => JobKind::Export,
            _ => return None,
        })
    }
}

/// A job's terminal-or-not state. Five states, as the ledger and the desktop
/// seam expect: `cancelled` stays distinct from `failed` because a user
/// stopping a job is not the same as a job breaking.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}

impl JobState {
    pub fn wire_name(self) -> &'static str {
        match self {
            JobState::Queued => "queued",
            JobState::Running => "running",
            JobState::Succeeded => "succeeded",
            JobState::Failed => "failed",
            JobState::Cancelled => "cancelled",
        }
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            JobState::Succeeded | JobState::Failed | JobState::Cancelled
        )
    }
}

/// What to run.
#[derive(Debug, Clone, PartialEq)]
pub struct JobRequest {
    pub kind: JobKind,
    /// The audio file for a transcription, the meeting or project folder for
    /// everything else.
    pub input_path: String,
    pub output_dir: String,
    pub language: Option<String>,
}

/// What a finished job produced.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct JobOutcome {
    pub audio_duration_sec: Option<f64>,
    pub segment_count: Option<i64>,
    pub filtered_segment_count: i64,
    /// Manifest of what an LLM job wrote, stored in the ledger verbatim.
    pub result_json: Option<String>,
    /// Non-fatal degradations the job survived: a failed screenshot pass, a
    /// PDF that would not render. The job succeeded; the caller should still
    /// see these.
    pub warnings: Vec<String>,
}

/// Why a job failed, attributed.
#[derive(Debug, Clone, PartialEq)]
pub struct JobFailure {
    pub kind: ErrorKind,
    pub message: String,
}

impl JobFailure {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        JobFailure {
            kind,
            message: message.into(),
        }
    }
}

/// What the poll path sees.
#[derive(Debug, Clone, PartialEq)]
pub struct JobSnapshot {
    pub job_id: String,
    pub kind: JobKind,
    pub state: JobState,
    /// 0.0 to 1.0, clamped.
    pub progress: f64,
    pub warnings: Vec<String>,
    pub error_kind: Option<ErrorKind>,
    pub error_message: Option<String>,
}

/// Cooperative cancellation. Every long step checks it: whisper through its
/// abort callback, llama between decoded tokens, ffmpeg by being killed,
/// downloads between chunks.
#[derive(Debug, Clone, Default)]
pub struct CancelToken(Arc<AtomicBool>);

impl CancelToken {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

/// What a running job is given: somewhere to report progress, and the two
/// questions it must keep asking.
pub struct JobContext {
    progress: Arc<AtomicU64>,
    cancel: CancelToken,
}

impl JobContext {
    /// Report progress, clamped to 0.0..=1.0.
    pub fn set_progress(&self, value: f64) {
        let clamped = value.clamp(0.0, 1.0);
        self.progress.store(clamped.to_bits(), Ordering::Relaxed);
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancel.is_cancelled()
    }

    /// A clone of the token, for handing to something that checks it on its
    /// own schedule -- whisper's abort callback, a download loop.
    pub fn cancel_token(&self) -> CancelToken {
        self.cancel.clone()
    }

    /// `Err(JobFailure)` when cancelled, for the `?` in a pipeline step.
    pub fn check_cancelled(&self) -> Result<(), JobFailure> {
        if self.is_cancelled() {
            Err(JobFailure::new(ErrorKind::Cancelled, "cancelled"))
        } else {
            Ok(())
        }
    }
}

/// The thing that actually does the work.
///
/// One trait rather than one per job kind: the runner owns whichever engines
/// it needs and decides which to load per job, which is what keeps the
/// "never two big models at once" rule in one place. Phase B and C fill this
/// in with whisper.cpp and llama.cpp; the tests use a fake.
///
/// Deliberately **not** `Send`. A runner is built by the factory *on the
/// worker thread* and never leaves it, which is what lets it hold a raw
/// `whisper_context` or `llama_context` -- neither of which may cross threads.
/// The factory is the thing that has to be `Send`; see [`RunnerFactory`].
pub trait JobRunner {
    fn run(&mut self, job: &JobRequest, ctx: &JobContext) -> Result<JobOutcome, JobFailure>;

    /// The device the job resolved to (`cpu`, `cuda`, ...), recorded on the
    /// ledger row when the job starts. Not known before then: loading an
    /// engine is deferred until there is work for it.
    fn device(&self) -> String {
        "auto".to_string()
    }

    /// The model id to record. Defaults to whatever config said.
    fn model(&self) -> Option<String> {
        None
    }
}

/// Builds a runner, and rebuilds it after a panic has left one untrustworthy.
pub type RunnerFactory = Box<dyn Fn() -> Box<dyn JobRunner> + Send>;

/// The runner for a build whose engines are not wired up yet.
///
/// It exists so the switch that selects the in-process engine can be turned on
/// -- exercising submission, the ledger, polling and cancellation for real --
/// without any job quietly *appearing* to succeed. A fake runner would report
/// success and write nothing, which in the real app is indistinguishable from
/// a transcript that vanished. Failing loudly is the only honest placeholder.
#[derive(Debug, Default)]
pub struct UnimplementedRunner;

impl JobRunner for UnimplementedRunner {
    fn run(&mut self, job: &JobRequest, _ctx: &JobContext) -> Result<JobOutcome, JobFailure> {
        Err(JobFailure::new(
            ErrorKind::Internal,
            format!(
                "the local engine cannot run a {} job yet",
                job.kind.wire_name()
            ),
        ))
    }
}

/// Why a request to the engine could not be accepted.
#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    #[error("no such job: {0}")]
    UnknownJob(String),
    #[error("the engine is shutting down")]
    ShuttingDown,
    #[error(transparent)]
    Ledger(#[from] crate::ledger::LedgerError),
}

struct Entry {
    snapshot: JobSnapshot,
    progress: Arc<AtomicU64>,
    cancel: CancelToken,
}

impl Entry {
    /// Progress lives in an atomic so the poll path never contends with a
    /// running job; fold it into the snapshot on read.
    fn snapshot(&self) -> JobSnapshot {
        let mut snapshot = self.snapshot.clone();
        if !snapshot.state.is_terminal() {
            snapshot.progress = f64::from_bits(self.progress.load(Ordering::Relaxed));
        }
        snapshot
    }
}

type JobTable = Arc<RwLock<HashMap<String, Entry>>>;

/// A handle to the running engine. Cheap to clone, safe to share.
#[derive(Clone)]
pub struct EngineHandle {
    inner: Arc<EngineInner>,
}

struct EngineInner {
    jobs: JobTable,
    tx: Mutex<Option<Sender<(String, JobRequest)>>>,
    ledger: Arc<Ledger>,
    config: Config,
    next_id: AtomicU64,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl EngineHandle {
    /// Start the worker. The ledger is reconciled first, so any job a crash
    /// left open is attributed before new work is accepted.
    pub fn start(
        config: Config,
        ledger: Ledger,
        runner_factory: RunnerFactory,
    ) -> Result<Self, EngineError> {
        let ledger = Arc::new(ledger);
        ledger.reconcile_interrupted()?;

        let jobs: JobTable = Arc::new(RwLock::new(HashMap::new()));
        let (tx, rx) = mpsc::channel::<(String, JobRequest)>();

        let worker = {
            let jobs = Arc::clone(&jobs);
            let ledger = Arc::clone(&ledger);
            let timeout = config.job_timeout_sec.map(Duration::from_secs);
            std::thread::Builder::new()
                .name("engine-worker".to_string())
                // Whisper and llama call deep; the default stack is not worth
                // finding the limit of.
                .stack_size(8 * 1024 * 1024)
                .spawn(move || worker_loop(rx, jobs, ledger, runner_factory, timeout))
                .expect("spawning the engine worker thread")
        };

        Ok(EngineHandle {
            inner: Arc::new(EngineInner {
                jobs,
                tx: Mutex::new(Some(tx)),
                ledger,
                config,
                next_id: AtomicU64::new(1),
                worker: Mutex::new(Some(worker)),
            }),
        })
    }

    pub fn config(&self) -> &Config {
        &self.inner.config
    }

    /// Accept a job, record it in the ledger, and queue it.
    pub fn submit(&self, request: JobRequest) -> Result<String, EngineError> {
        let job_id = format!(
            "job-{:08}",
            self.inner.next_id.fetch_add(1, Ordering::SeqCst)
        );

        self.inner.ledger.insert_job(&NewJob {
            job_id: &job_id,
            job_type: request.kind.wire_name(),
            provider: "local",
            model: &self.inner.config.model,
            device: &self.inner.config.device,
            source_path: &request.input_path,
            output_path: &request.output_dir,
            language: request.language.as_deref(),
            meeting_json: None,
        })?;

        {
            let mut jobs = self.inner.jobs.write().expect("job table");
            jobs.insert(
                job_id.clone(),
                Entry {
                    snapshot: JobSnapshot {
                        job_id: job_id.clone(),
                        kind: request.kind,
                        state: JobState::Queued,
                        progress: 0.0,
                        warnings: Vec::new(),
                        error_kind: None,
                        error_message: None,
                    },
                    progress: Arc::new(AtomicU64::new(0.0f64.to_bits())),
                    cancel: CancelToken::default(),
                },
            );
        }
        let tx = self.inner.tx.lock().expect("sender");
        let Some(tx) = tx.as_ref() else {
            return Err(EngineError::ShuttingDown);
        };
        tx.send((job_id.clone(), request))
            .map_err(|_| EngineError::ShuttingDown)?;
        Ok(job_id)
    }

    /// The live view of one job.
    pub fn status(&self, job_id: &str) -> Option<JobSnapshot> {
        self.inner
            .jobs
            .read()
            .expect("job table")
            .get(job_id)
            .map(Entry::snapshot)
    }

    /// Ask a job to stop. Returns once the request is recorded, which is not
    /// the same as the job being over: the engine owns the lifecycle, and the
    /// poll loop observes the resulting `cancelled` state like any other.
    pub fn cancel(&self, job_id: &str) -> Result<(), EngineError> {
        let jobs = self.inner.jobs.read().expect("job table");
        let entry = jobs
            .get(job_id)
            .ok_or_else(|| EngineError::UnknownJob(job_id.to_string()))?;
        entry.cancel.cancel();
        Ok(())
    }

    /// The durable history, newest first.
    pub fn list_ledger_jobs(&self, limit: Option<u32>) -> Result<Vec<LedgerRow>, EngineError> {
        Ok(self.inner.ledger.list_jobs(limit, None)?)
    }

    /// Stop accepting work and wait for the worker to finish the job in hand.
    pub fn shutdown(&self) {
        // Dropping the sender is what ends the worker's loop.
        if let Ok(mut tx) = self.inner.tx.lock() {
            tx.take();
        }
        if let Ok(mut worker) = self.inner.worker.lock() {
            if let Some(handle) = worker.take() {
                let _ = handle.join();
            }
        }
    }
}

fn worker_loop(
    rx: Receiver<(String, JobRequest)>,
    jobs: JobTable,
    ledger: Arc<Ledger>,
    runner_factory: RunnerFactory,
    timeout: Option<Duration>,
) {
    let mut runner = runner_factory();

    loop {
        let (job_id, request) = match rx.recv_timeout(IDLE_TICK) {
            Ok(job) => job,
            Err(RecvTimeoutError::Timeout) => continue,
            // Every sender is gone: shutdown.
            Err(RecvTimeoutError::Disconnected) => break,
        };

        let (progress, cancel) = {
            let jobs = jobs.read().expect("job table");
            let Some(entry) = jobs.get(&job_id) else {
                continue;
            };
            (Arc::clone(&entry.progress), entry.cancel.clone())
        };

        // A job cancelled while still queued never starts.
        if cancel.is_cancelled() {
            let _ = ledger.finish_cancelled(&job_id, Some(0.0));
            finish(&jobs, &job_id, JobState::Cancelled, None, Vec::new());
            continue;
        }

        set_running(&jobs, &job_id);
        let _ = ledger.mark_running(&job_id, Some(&runner.device()));

        let started = Instant::now();
        let ctx = JobContext {
            progress,
            cancel: cancel.clone(),
        };

        // A deadline is enforced by asking the job to stop, not by killing the
        // thread: there is no safe way to unwind native code from outside it.
        let watchdog = timeout.map(|limit| {
            let cancel = cancel.clone();
            let (stop_tx, stop_rx) = mpsc::channel::<()>();
            let handle = std::thread::spawn(move || {
                if stop_rx.recv_timeout(limit) == Err(RecvTimeoutError::Timeout) {
                    cancel.cancel();
                }
            });
            (stop_tx, handle)
        });

        let result = catch_unwind(AssertUnwindSafe(|| runner.run(&request, &ctx)));

        if let Some((stop_tx, handle)) = watchdog {
            let _ = stop_tx.send(());
            let _ = handle.join();
        }

        let elapsed = started.elapsed().as_secs_f64();
        let timed_out = timeout.is_some_and(|limit| started.elapsed() >= limit);

        match result {
            Ok(Ok(outcome)) => {
                let _ = ledger.finish_succeeded(
                    &job_id,
                    &Success {
                        elapsed_sec: elapsed,
                        audio_duration_sec: outcome.audio_duration_sec,
                        segment_count: outcome.segment_count,
                        filtered_segment_count: outcome.filtered_segment_count,
                        result_json: outcome.result_json.as_deref(),
                    },
                );
                finish(&jobs, &job_id, JobState::Succeeded, None, outcome.warnings);
            }
            Ok(Err(failure)) => {
                // A cancelled job reports as cancelled even though the runner
                // surfaced it as an error: the distinction is the user's, and
                // a deadline that fired reads as a timeout, not a stop.
                if failure.kind == ErrorKind::Cancelled && !timed_out {
                    let _ = ledger.finish_cancelled(&job_id, Some(elapsed));
                    finish(&jobs, &job_id, JobState::Cancelled, None, Vec::new());
                } else {
                    let failure = if timed_out {
                        JobFailure::new(
                            ErrorKind::Timeout,
                            format!("job exceeded its {:.0}s time limit", elapsed),
                        )
                    } else {
                        failure
                    };
                    let _ = ledger.finish_failed(
                        &job_id,
                        Some(elapsed),
                        failure.kind,
                        &failure.message,
                    );
                    finish(&jobs, &job_id, JobState::Failed, Some(failure), Vec::new());
                }
            }
            Err(_) => {
                // A panic crossed the runner. Fail the job, then rebuild the
                // runner: a panic unwound through FFI can leave a model half
                // initialised, and the next job must not inherit it.
                let failure = JobFailure::new(
                    ErrorKind::Internal,
                    "the engine panicked while running this job",
                );
                let _ =
                    ledger.finish_failed(&job_id, Some(elapsed), failure.kind, &failure.message);
                finish(&jobs, &job_id, JobState::Failed, Some(failure), Vec::new());
                runner = runner_factory();
            }
        }
    }
}

fn set_running(jobs: &JobTable, job_id: &str) {
    if let Some(entry) = jobs.write().expect("job table").get_mut(job_id) {
        entry.snapshot.state = JobState::Running;
    }
}

fn finish(
    jobs: &JobTable,
    job_id: &str,
    state: JobState,
    failure: Option<JobFailure>,
    warnings: Vec<String>,
) {
    if let Some(entry) = jobs.write().expect("job table").get_mut(job_id) {
        entry.snapshot.state = state;
        entry.snapshot.warnings = warnings;
        entry.snapshot.progress = if state == JobState::Succeeded {
            1.0
        } else {
            f64::from_bits(entry.progress.load(Ordering::Relaxed))
        };
        if let Some(failure) = failure {
            entry.snapshot.error_kind = Some(failure.kind);
            entry.snapshot.error_message = Some(failure.message);
        } else if state == JobState::Cancelled {
            // The Python service attached no message to a cancellation; the UI
            // still needs something to show.
            entry.snapshot.error_message = Some("cancelled".to_string());
        }
    }
}
