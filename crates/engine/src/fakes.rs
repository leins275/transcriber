//! A [`JobRunner`] that does no inference.
//!
//! Two audiences. The desktop app uses it to run the whole UI -- ingest, job
//! states, progress, cancellation, the ledger panel -- on a machine with no
//! models downloaded, which is also how the frontend is developed. The engine
//! uses it to test the job lifecycle without waiting on a real model, which is
//! what makes lifecycle tests fast enough to run on every change.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use wire::ErrorKind;

use crate::jobs::{JobContext, JobFailure, JobOutcome, JobRequest, JobRunner};

/// What a fake job should do instead of working.
#[derive(Debug, Clone, PartialEq)]
pub enum FakeBehaviour {
    /// Report progress in `steps`, then succeed.
    Succeed {
        steps: u32,
        step_delay: Duration,
    },
    Fail(ErrorKind, String),
    /// Panic, to exercise the worker's containment and rebuild path.
    Panic,
    /// Never finish on its own: run until cancelled or timed out.
    Hang,
}

impl Default for FakeBehaviour {
    fn default() -> Self {
        FakeBehaviour::Succeed {
            steps: 4,
            step_delay: Duration::from_millis(1),
        }
    }
}

/// A runner with no engines behind it.
#[derive(Debug, Default)]
pub struct FakeRunner {
    behaviour: FakeBehaviour,
    warnings: Vec<String>,
    /// Counts how many times the factory has built a runner, so a test can see
    /// that a panic actually caused a rebuild.
    builds: Option<Arc<AtomicUsize>>,
}

impl FakeRunner {
    pub fn new(behaviour: FakeBehaviour) -> Self {
        FakeRunner {
            behaviour,
            warnings: Vec::new(),
            builds: None,
        }
    }

    /// Degradations to report on success (a failed screenshot pass, a PDF that
    /// would not render).
    pub fn with_warnings(mut self, warnings: Vec<String>) -> Self {
        self.warnings = warnings;
        self
    }

    pub fn counting_builds(mut self, counter: Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        self.builds = Some(counter);
        self
    }
}

impl JobRunner for FakeRunner {
    fn run(&mut self, _job: &JobRequest, ctx: &JobContext) -> Result<JobOutcome, JobFailure> {
        match &self.behaviour {
            FakeBehaviour::Succeed { steps, step_delay } => {
                for step in 1..=*steps {
                    ctx.check_cancelled()?;
                    std::thread::sleep(*step_delay);
                    ctx.set_progress(step as f64 / *steps as f64);
                }
                Ok(JobOutcome {
                    audio_duration_sec: Some(12.0),
                    segment_count: Some(3),
                    filtered_segment_count: 0,
                    result_json: None,
                    warnings: self.warnings.clone(),
                })
            }
            FakeBehaviour::Fail(kind, message) => Err(JobFailure::new(*kind, message.clone())),
            FakeBehaviour::Panic => panic!("fake runner panicking on purpose"),
            FakeBehaviour::Hang => loop {
                ctx.check_cancelled()?;
                std::thread::sleep(Duration::from_millis(2));
            },
        }
    }

    fn device(&self) -> String {
        "fake".to_string()
    }
}
