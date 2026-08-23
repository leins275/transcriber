//! The in-process engine: everything the Python service used to do.
//!
//! This crate deliberately has **no dependency on Tauri or the desktop app**.
//! Its request and result types are plain serde-derived data, and the whole
//! surface is a handle plus messages. That is not decoration: running the
//! engines in-process means a native crash inside whisper.cpp, llama.cpp or a
//! CUDA driver takes the whole application down, where the old sidecar would
//! have survived it. Keeping this crate transport-agnostic means the escape
//! hatch -- hosting it in a small child process and speaking serde over stdio
//! -- stays a transport change rather than a redesign, if crash reports ever
//! say it is needed.
//!
//! - [`config`] -- defaults, `config.json` and `TRANSCRIBER_*`, minus every
//!   setting that only existed to reach a network.
//! - [`ledger`] -- the SQLite job history, shared with (and migrated from) the
//!   Python service's own database file.

pub mod config;
pub mod fakes;
pub mod jobs;
pub mod ledger;
pub mod models;

pub use config::{Config, ConfigError};
pub use jobs::{
    CancelToken, EngineError, EngineHandle, JobContext, JobFailure, JobKind, JobOutcome,
    JobRequest, JobRunner, JobSnapshot, JobState,
};
pub use ledger::{Ledger, LedgerError, LedgerRow, NewJob, Success};
