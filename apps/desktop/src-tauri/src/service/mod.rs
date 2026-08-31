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

    /// `GET /v1/model/download` (T13, FR-12, FR-17). Default: unsupported --
    /// only `http::HttpTranscriptionService` and `fake::FakeService`
    /// override this; every other implementor of this trait (in particular
    /// the job-pipeline test fakes in `jobs.rs`, which this task does not
    /// own) inherits this default instead of needing an edit just to keep
    /// compiling.
    async fn model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "model download is not supported by this service".to_string(),
        })
    }

    /// `POST /v1/model/download` -- starts a transfer, or returns the
    /// already-running one's status (F2 never starts a second parallel
    /// transfer). Default: unsupported (see `model_download_status`).
    async fn start_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "model download is not supported by this service".to_string(),
        })
    }

    /// `DELETE /v1/model/download` -- cancels a transfer, leaving it in a
    /// retryable state. Default: unsupported (see `model_download_status`).
    async fn cancel_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "model download is not supported by this service".to_string(),
        })
    }

    /// `DELETE /v1/jobs/{id}` -- asks F2 to stop a job it is running.
    ///
    /// Returns `Ok(())` once F2 has accepted the request, which is not the
    /// same as the job being over: F2 owns the lifecycle, and the poll loop
    /// observes the resulting `cancelled` status like any other terminal
    /// state. Default: unsupported, for the same reason the model-download
    /// trio is (`jobs.rs`'s pipeline fakes must not need an edit to keep
    /// compiling).
    async fn cancel(&self, _job_id: &str) -> Result<(), ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "cancelling a job is not supported by this service".to_string(),
        })
    }

    /// `GET /v1/jobs` -- F2's own sqlite job ledger, newest first.
    ///
    /// Distinct from `status()`, which asks about one *live* job this app
    /// submitted: the ledger is F2's durable record and outlives both the
    /// job and this app's session, which is what makes it the right source
    /// for a "what has this service actually done" panel. Default:
    /// unsupported, for the same reason the model-download trio above is
    /// (`jobs.rs`'s pipeline fakes must not need an edit to keep
    /// compiling).
    async fn list_ledger_jobs(&self, _limit: u32) -> Result<Vec<LedgerJob>, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "the job ledger is not supported by this service".to_string(),
        })
    }

    /// `POST /v1/jobs` with a derived (LLM) job type -- summarize /
    /// per-recording export.
    /// Returns F2's `job_id`, polled through the same `status()` as a
    /// transcription. Default: unsupported (the house rule -- pipeline
    /// fakes must not need an edit to keep compiling).
    async fn submit_llm(&self, _req: LlmSubmitRequest) -> Result<String, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "llm jobs are not supported by this service".to_string(),
        })
    }

    /// `POST /v1/jobs` with `job_type: "index"` -- F2's vault-wide
    /// incremental search re-index. Fire-and-forget by every caller: never
    /// polled, never a user-visible job, and an error (an older service
    /// that does not know the type) is simply dropped. Default: unsupported
    /// (the house rule -- pipeline fakes must not need an edit to keep
    /// compiling).
    async fn submit_index(&self) -> Result<String, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "index jobs are not supported by this service".to_string(),
        })
    }

    /// `GET /v1/llm-model/download` -- the GGUF slot's status. Same wire
    /// shape as the whisper trio, its own independent transfer. Default:
    /// unsupported (see `model_download_status`).
    async fn llm_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "llm model download is not supported by this service".to_string(),
        })
    }

    /// `POST /v1/llm-model/download`. Default: unsupported.
    async fn start_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "llm model download is not supported by this service".to_string(),
        })
    }

    /// `DELETE /v1/llm-model/download`. Default: unsupported.
    async fn cancel_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "llm model download is not supported by this service".to_string(),
        })
    }

    /// `GET /v1/llm-models` -- the curated LLM catalog with per-model
    /// presence, active flag and download slot status. Default: unsupported
    /// (see `model_download_status`).
    async fn llm_models(&self) -> Result<LlmModelsStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "the llm model catalog is not supported by this service".to_string(),
        })
    }

    /// `POST /v1/llm-models/{id}/download`. Default: unsupported.
    async fn start_llm_model_download_for(
        &self,
        _model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "the llm model catalog is not supported by this service".to_string(),
        })
    }

    /// `DELETE /v1/llm-models/{id}/download` (cancel). Default: unsupported.
    async fn cancel_llm_model_download_for(
        &self,
        _model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "the llm model catalog is not supported by this service".to_string(),
        })
    }

    /// `DELETE /v1/llm-models/{id}` -- delete a downloaded model's file.
    /// Default: unsupported.
    async fn delete_llm_model(&self, _model_id: &str) -> Result<LlmModelsStatus, ServiceError> {
        Err(ServiceError::Unavailable {
            detail: "the llm model catalog is not supported by this service".to_string(),
        })
    }
}

/// The derived (LLM) job kinds F2 runs over already-transcribed material.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LlmJobKind {
    /// Write `<meeting>/summary.md` from the transcript (the summary
    /// carries the action items as a section).
    Summarize,
    /// Deterministic per-recording export into the meeting folder itself
    /// (`export.md` + the share-named PDF, overwritten on re-run).
    Export,
}

impl LlmJobKind {
    /// F2's `job_type` wire value (`schema.py::JobType`).
    pub fn wire_name(self) -> &'static str {
        match self {
            LlmJobKind::Summarize => "summarize",
            LlmJobKind::Export => "export",
        }
    }
}

/// `POST /v1/jobs` request body for a derived job: F2 takes the meeting
/// directory as `input_path` and writes its artifacts under
/// `output_dir` -- both computed on this side, both validated against F2's
/// own allowlist over there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LlmSubmitRequest {
    pub kind: LlmJobKind,
    pub input_path: String,
    pub output_dir: String,
}

/// One row of F2's sqlite job ledger (`services/transcription/.../ledger.py`
/// -- the `jobs` table), reduced to the columns worth showing.
///
/// Every field but `job_id`/`status` is optional because the row is written
/// in stages: it is inserted at submission time with only the columns known
/// then, and filled in as the job starts and finishes. A row for a job that
/// is still queued genuinely has no `elapsed_sec`, and that is information,
/// not a decode failure.
#[derive(Debug, Clone, PartialEq)]
pub struct LedgerJob {
    pub job_id: String,
    /// F2's own five-state wire vocabulary (`queued`, `running`,
    /// `succeeded`, `failed`, `cancelled`), passed through verbatim rather
    /// than collapsed onto [`JobState`]'s four: a ledger reader wants to
    /// see that a job was cancelled rather than that it "failed".
    pub status: String,
    pub created_at: Option<String>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub device: Option<String>,
    pub source_path: Option<String>,
    pub output_path: Option<String>,
    pub audio_duration_sec: Option<f64>,
    pub elapsed_sec: Option<f64>,
    pub realtime_factor: Option<f64>,
    pub language: Option<String>,
    pub segment_count: Option<i64>,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
    pub service_version: Option<String>,
    /// The recording's original file name as recorded at submit time (FR-1),
    /// read back out of the row's `meeting_json` column. `None` for every
    /// pre-feature row, for a retranscribe of an already-filed recording
    /// (FR-5), and for any `meeting_json` this side cannot make sense of
    /// (NFR-2) -- the UI derives a display name from `source_path` instead.
    ///
    /// Parsed once, here on the seam, so the panel stays presentational
    /// (FR-2).
    pub original_file_name: Option<String>,
}

/// `POST /v1/jobs` request body (F2's `JobCreate`, minus the fields this
/// app never sets).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmitRequest {
    pub audio_path: String,
    pub output_dir: String,
    pub language: Option<String>,
    /// The recording's *original* file name, as it was dropped, before the
    /// vault renamed it to `source.<ext>` (FR-1). `None` whenever no such
    /// name is known -- notably a retranscribe of an already-filed recording,
    /// where only `source.<ext>` exists on disk and calling that the
    /// "original file name" would be a lie (FR-5). Travels in F2's existing
    /// `meeting` object and is persisted verbatim as `meeting_json`.
    pub original_file_name: Option<String>,
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
    /// F2's `model_present` field (T13, FR-17) -- whether the pinned whisper
    /// snapshot's `.ready` marker already exists, so the app can detect a
    /// missing model without guessing.
    pub model_present: bool,
    /// F2's `cuda_runtime_present` field (E13) -- `None` on a host F2 itself
    /// judged GPU-less (never prompt about a runtime that machine could
    /// never use), `Some(bool)` otherwise: whether the CUDA runtime wheels'
    /// `.ready` marker exists under the app folder's `runtime/`.
    pub cuda_runtime_present: Option<bool>,
    /// F2's `llm_model_present` field (the LLM feature) -- whether the
    /// configured GGUF file is on disk. `None` when the field is absent
    /// (a build of F2 older than the feature).
    pub llm_model_present: Option<bool>,
    /// F2's `llm_gpu_build_present` field -- whether the first-run CUDA
    /// build of the LLM runtime is on disk. `None` on a GPU-less host (or
    /// an older F2): never offer GPU acceleration a machine cannot use.
    pub llm_gpu_build_present: Option<bool>,
}

/// Wire-level model-download state (mirrors F2's `DownloadState`, T13,
/// FR-12).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelDownloadState {
    Idle,
    Downloading,
    Verifying,
    Complete,
    Cancelled,
    Error,
}

impl ModelDownloadState {
    /// Maps one of F2's six wire status strings. Returns `None` for
    /// anything undocumented, so callers never guess at an unrecognised
    /// state.
    pub fn from_wire(state: &str) -> Option<Self> {
        match state {
            "idle" => Some(ModelDownloadState::Idle),
            "downloading" => Some(ModelDownloadState::Downloading),
            "verifying" => Some(ModelDownloadState::Verifying),
            "complete" => Some(ModelDownloadState::Complete),
            "cancelled" => Some(ModelDownloadState::Cancelled),
            "error" => Some(ModelDownloadState::Error),
            _ => None,
        }
    }
}

/// `GET`/`POST`/`DELETE /v1/model/download` response (T13, FR-12, FR-17).
#[derive(Debug, Clone, PartialEq)]
pub struct ModelDownloadStatus {
    pub state: ModelDownloadState,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
    /// F2's `cuda_warning` field (E13) -- set once a `SetupDownload`'s
    /// CUDA-runtime phase has failed and the setup continued into the model
    /// phase anyway (E4). Verbatim backend text, never re-worded here.
    pub cuda_warning: Option<String>,
}

/// One curated model row from `GET /v1/llm-models`.
#[derive(Debug, Clone, PartialEq)]
pub struct LlmCatalogModel {
    pub id: String,
    pub label: String,
    pub file: String,
    /// Approximate GGUF size for UI display; `None` for the escape-hatch
    /// row (a hand-configured model outside the catalog).
    pub size_bytes: Option<u64>,
    /// `false` for the escape-hatch row.
    pub catalog: bool,
    pub present: bool,
    pub active: bool,
    pub download: ModelDownloadStatus,
}

/// `GET /v1/llm-models` response: the catalog plus which model is active.
#[derive(Debug, Clone, PartialEq)]
pub struct LlmModelsStatus {
    pub active: String,
    pub models: Vec<LlmCatalogModel>,
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
