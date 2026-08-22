//! The `list_service_jobs` `#[tauri::command]` handler: a read-only window
//! onto F2's own sqlite job ledger.
//!
//! A thin proxy over the `TranscriptionService` seam's `list_ledger_jobs`,
//! the same "existing authenticated loopback client" every other command in
//! this crate goes through — this app never opens F2's database file itself.
//! That matters for more than tidiness: F2 holds the ledger open in WAL mode
//! from its own process, and a second reader poking at the file behind its
//! back is exactly how a "database is locked" bug gets born.
//!
//! Distinct from `list_jobs`, which reports *this session's* in-memory
//! ingest+transcribe pipeline (`jobs::JobRegistry`). The ledger is F2's
//! durable record: it survives a restart of both processes and is what
//! answers "what has this service actually done, and what went wrong".

use serde::Serialize;

use crate::error::AppError;
use crate::service::{LedgerJob, ServiceError};

use super::AppState;

/// The default number of ledger rows fetched when the caller does not ask
/// for a specific count. Matches F2's own `GET /v1/jobs` default.
const DEFAULT_LIMIT: u32 = 50;

/// F2's hard ceiling on that endpoint (`Query(ge=1, le=500)`). Clamping here
/// rather than forwarding an out-of-range value turns what would be an
/// opaque `400 request validation failed` from F2 into the largest page it
/// will actually serve.
const MAX_LIMIT: u32 = 500;

/// One ledger row, as the UI renders it.
///
/// Mirrors [`LedgerJob`] one-for-one — this exists because that type lives on
/// the service seam and is deliberately not `Serialize` (the seam is not an
/// IPC contract), the same split `model::ModelDownloadStatusView` makes.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LedgerJobView {
    pub job_id: String,
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
}

impl From<LedgerJob> for LedgerJobView {
    fn from(job: LedgerJob) -> Self {
        LedgerJobView {
            job_id: job.job_id,
            status: job.status,
            created_at: job.created_at,
            started_at: job.started_at,
            finished_at: job.finished_at,
            provider: job.provider,
            model: job.model,
            device: job.device,
            source_path: job.source_path,
            output_path: job.output_path,
            audio_duration_sec: job.audio_duration_sec,
            elapsed_sec: job.elapsed_sec,
            realtime_factor: job.realtime_factor,
            language: job.language,
            segment_count: job.segment_count,
            error_kind: job.error_kind,
            error_message: job.error_message,
            service_version: job.service_version,
        }
    }
}

/// Clamps a caller-supplied limit into the range F2 accepts.
///
/// `None` means "no preference" and takes [`DEFAULT_LIMIT`]; `0` is treated
/// as 1 rather than rejected, since asking for nothing is a UI bug, not
/// something worth failing an operator's panel over.
pub fn clamp_limit(requested: Option<u32>) -> u32 {
    match requested {
        None => DEFAULT_LIMIT,
        Some(value) => value.clamp(1, MAX_LIMIT),
    }
}

/// `list_service_jobs` — newest-first rows from F2's job ledger.
pub async fn list_service_jobs_handler(
    state: &AppState,
    limit: Option<u32>,
) -> Result<Vec<LedgerJobView>, AppError> {
    let service = state.service.read().await.clone();
    let jobs = service
        .list_ledger_jobs(clamp_limit(limit))
        .await
        .map_err(map_service_error)?;
    Ok(jobs.into_iter().map(LedgerJobView::from).collect())
}

/// Maps a seam failure onto the IPC error taxonomy, keeping "the service
/// isn't up" (an expected state the UI renders as a quiet notice) apart from
/// "the service answered badly" (a real error worth surfacing).
fn map_service_error(err: ServiceError) -> AppError {
    match err {
        ServiceError::Unavailable { .. } => AppError::service_unavailable(err.to_string()),
        other => AppError::service(other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_limit_takes_the_backend_default() {
        assert_eq!(clamp_limit(None), DEFAULT_LIMIT);
    }

    #[test]
    fn a_limit_over_the_backend_ceiling_is_clamped_not_rejected() {
        assert_eq!(clamp_limit(Some(10_000)), MAX_LIMIT);
    }

    #[test]
    fn a_zero_limit_becomes_one_rather_than_a_validation_failure() {
        assert_eq!(clamp_limit(Some(0)), 1);
    }

    #[test]
    fn a_limit_in_range_is_passed_through() {
        assert_eq!(clamp_limit(Some(25)), 25);
    }

    #[test]
    fn an_unreachable_service_maps_to_service_unavailable_not_a_hard_error() {
        let err = map_service_error(ServiceError::Unavailable {
            detail: "connection refused".to_string(),
        });

        assert_eq!(err.kind(), crate::error::ErrorKind::ServiceUnavailable);
    }

    #[test]
    fn a_bad_response_maps_to_service() {
        let err = map_service_error(ServiceError::Http {
            status: 500,
            message: "boom".to_string(),
        });

        assert_eq!(err.kind(), crate::error::ErrorKind::Service);
    }
}
