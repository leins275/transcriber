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
    /// The recording's original file name, when the row recorded one (FR-2).
    /// `None` -- `null` over IPC -- for every row that did not, which the
    /// panel renders via its `source_path`-derived fallback (FR-3).
    pub original_file_name: Option<String>,
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
            original_file_name: job.original_file_name,
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

    /// A ledger row with every column populated, so a pass-through test can
    /// tell "copied" from "defaulted".
    fn full_job() -> LedgerJob {
        LedgerJob {
            job_id: "job-1".to_string(),
            status: "succeeded".to_string(),
            created_at: Some("2026-08-24T10:00:00Z".to_string()),
            started_at: Some("2026-08-24T10:00:01Z".to_string()),
            finished_at: Some("2026-08-24T10:03:00Z".to_string()),
            provider: Some("whisper_cpp".to_string()),
            model: Some("large-v3".to_string()),
            device: Some("cuda".to_string()),
            source_path: Some("C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4".to_string()),
            output_path: Some(
                "C:\\Meetings\\ELS\\260812 - Security issue\\transcript.md".to_string(),
            ),
            audio_duration_sec: Some(1800.0),
            elapsed_sec: Some(179.0),
            realtime_factor: Some(10.05),
            language: Some("ru".to_string()),
            segment_count: Some(412),
            error_kind: Some("none".to_string()),
            error_message: Some("".to_string()),
            service_version: Some("0.9.0".to_string()),
            original_file_name: Some("ELS - 260812 - Security issue.mp4".to_string()),
        }
    }

    #[test]
    fn the_view_carries_the_recorded_original_file_name() {
        // FR-2: the name parsed once on the seam reaches the UI's row shape.
        let view = LedgerJobView::from(full_job());

        assert_eq!(
            view.original_file_name.as_deref(),
            Some("ELS - 260812 - Security issue.mp4")
        );
    }

    #[test]
    fn a_row_without_a_recorded_name_reaches_the_view_as_none() {
        // FR-3/NFR-1: pre-feature rows stay absent rather than inventing a
        // name; the fallback is the panel's job.
        let view = LedgerJobView::from(LedgerJob {
            original_file_name: None,
            ..full_job()
        });

        assert_eq!(view.original_file_name, None);
    }

    #[test]
    fn every_pre_existing_field_survives_the_conversion_unchanged() {
        // FR-4: the new field is additive -- nothing else about the row moved.
        let job = full_job();
        let view = LedgerJobView::from(job.clone());

        assert_eq!(view.job_id, job.job_id);
        assert_eq!(view.status, job.status);
        assert_eq!(view.created_at, job.created_at);
        assert_eq!(view.started_at, job.started_at);
        assert_eq!(view.finished_at, job.finished_at);
        assert_eq!(view.provider, job.provider);
        assert_eq!(view.model, job.model);
        assert_eq!(view.device, job.device);
        assert_eq!(view.source_path, job.source_path);
        assert_eq!(view.output_path, job.output_path);
        assert_eq!(view.audio_duration_sec, job.audio_duration_sec);
        assert_eq!(view.elapsed_sec, job.elapsed_sec);
        assert_eq!(view.realtime_factor, job.realtime_factor);
        assert_eq!(view.language, job.language);
        assert_eq!(view.segment_count, job.segment_count);
        assert_eq!(view.error_kind, job.error_kind);
        assert_eq!(view.error_message, job.error_message);
        assert_eq!(view.service_version, job.service_version);
    }

    #[test]
    fn the_view_serializes_the_original_file_name_as_snake_case_for_the_webview() {
        // The IPC contract `types.ts` pins: `original_file_name: string | null`.
        let json = serde_json::to_value(LedgerJobView::from(full_job()))
            .expect("the view must serialize for IPC");

        assert_eq!(
            json.get("original_file_name").and_then(|v| v.as_str()),
            Some("ELS - 260812 - Security issue.mp4")
        );
    }

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
