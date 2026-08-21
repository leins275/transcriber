//! `TranscriptionService` trait and job model (FR-12).
//!
//! This module defines exactly one abstraction over F2 (the transcription
//! service): a trait, its request/response types, and the state collapse
//! from F2's five wire-level job states onto the seam's four (plan.md
//! "Service seam (FR-12)"). `http.rs` (T7) binds this trait to the real
//! HTTP API; `fake.rs` (this task) is an in-memory implementation used by
//! every other task's tests so nothing needs a running F2 process.

use async_trait::async_trait;
use std::fmt;

pub mod fake;
pub mod http;

/// One abstraction over F2, implemented by `http::HttpTranscriptionService`
/// (real) and `fake::FakeService` (tests/dev).
#[async_trait]
pub trait TranscriptionService: Send + Sync {
    async fn health(&self) -> Result<ServiceHealth, ServiceError>;
    /// Submit a job; returns F2's `job_id`.
    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError>;
    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError>;
}

/// `POST /v1/jobs` request body (F2's `JobCreate`, minus the fields this
/// app never sets).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmitRequest {
    pub audio_path: String,
    pub output_dir: String,
    pub language: Option<String>,
}

/// The seam's four job states (FR-12). F2 has five (`queued`, `running`,
/// `succeeded`, `failed`, `cancelled`); the collapse lives in
/// [`JobState::from_wire`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Queued,
    Running,
    Done,
    Failed,
}

impl JobState {
    /// Map one of F2's five wire status strings onto the seam's four
    /// states: `queued -> Queued`, `running -> Running`,
    /// `succeeded -> Done`, `failed -> Failed`, `cancelled -> Failed`.
    /// Returns `None` for anything F2 does not document, so callers never
    /// guess at an unrecognised status.
    pub fn from_wire(status: &str) -> Option<Self> {
        match status {
            "queued" => Some(JobState::Queued),
            "running" => Some(JobState::Running),
            "succeeded" => Some(JobState::Done),
            "failed" | "cancelled" => Some(JobState::Failed),
            _ => None,
        }
    }
}

/// `GET /v1/jobs/{id}` response, collapsed onto the seam's four states.
/// `error_kind`/`error_message` pass through F2's body unchanged, except
/// that a `cancelled` job (which F2 never attaches a message to) is given
/// the literal message `"cancelled"` so the UI always has something to
/// show (plan's "Service seam (FR-12)").
#[derive(Debug, Clone, PartialEq)]
pub struct JobStatus {
    pub state: JobState,
    pub progress: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
}

impl JobStatus {
    /// Build a `JobStatus` from F2's wire fields, applying the same
    /// collapse as [`JobState::from_wire`] plus the `cancelled` message
    /// override. Returns `None` for an unrecognised wire status.
    pub fn from_wire(
        status: &str,
        progress: f64,
        error_kind: Option<String>,
        error_message: Option<String>,
    ) -> Option<Self> {
        let state = JobState::from_wire(status)?;
        let error_message = if status == "cancelled" {
            Some("cancelled".to_string())
        } else {
            error_message
        };
        Some(JobStatus {
            state,
            progress,
            error_kind,
            error_message,
        })
    }
}

/// `GET /health` response, reduced to what the app acts on.
#[derive(Debug, Clone, PartialEq)]
pub struct ServiceHealth {
    pub ready: bool,
    pub detail: Option<String>,
}

/// Every way a `TranscriptionService` call can fail (FR-13, FR-14, NFR-6).
/// No variant panics its way into existence: every constructor site takes
/// an owned message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceError {
    /// The service could not be reached at all (down, refused, timed out).
    Unavailable { detail: String },
    /// The service answered with a non-2xx HTTP status.
    Http { status: u16, message: String },
    /// The response body could not be decoded into the expected shape.
    Decode { message: String },
    /// The service rejected the configured bearer token.
    Auth { message: String },
}

impl fmt::Display for ServiceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ServiceError::Unavailable { detail } => write!(f, "service unavailable: {detail}"),
            ServiceError::Http { status, message } => {
                write!(f, "service http error {status}: {message}")
            }
            ServiceError::Decode { message } => {
                write!(f, "service response decode error: {message}")
            }
            ServiceError::Auth { message } => write!(f, "service auth error: {message}"),
        }
    }
}

impl std::error::Error for ServiceError {}
