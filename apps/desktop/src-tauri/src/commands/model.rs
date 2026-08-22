//! The three model-download `#[tauri::command]` handlers (T13, FR-12,
//! FR-17): thin proxies over the `TranscriptionService` seam's own
//! `model_download_status`/`start_model_download`/`cancel_model_download`
//! methods (T13's own extension of that trait, `service/mod.rs`), the same
//! "existing authenticated loopback client" every other command in this
//! crate already goes through. None of the three takes any argument: the
//! model payload's destination is resolved entirely on F2's side from
//! `config.app_dir` (F2's `model_routes.build_download`), never from a
//! caller-supplied path -- so there is no path argument here to validate or
//! smuggle a traversal through (F3 NFR-6, desktop profile IPC
//! authorization).

use serde::Serialize;

use crate::error::AppError;
use crate::service::{ModelDownloadState, ModelDownloadStatus, ServiceError};

use super::AppState;

/// IPC contract's model-download state (mirrors [`ModelDownloadState`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelDownloadStateView {
    Idle,
    Downloading,
    Verifying,
    Complete,
    Cancelled,
    Error,
}

impl From<ModelDownloadState> for ModelDownloadStateView {
    fn from(state: ModelDownloadState) -> Self {
        match state {
            ModelDownloadState::Idle => ModelDownloadStateView::Idle,
            ModelDownloadState::Downloading => ModelDownloadStateView::Downloading,
            ModelDownloadState::Verifying => ModelDownloadStateView::Verifying,
            ModelDownloadState::Complete => ModelDownloadStateView::Complete,
            ModelDownloadState::Cancelled => ModelDownloadStateView::Cancelled,
            ModelDownloadState::Error => ModelDownloadStateView::Error,
        }
    }
}

/// The frontend's `ModelDownloadStatusView` (`types.ts`) -- F2's own
/// `{state, downloaded_bytes, total_bytes, percent, error_kind,
/// error_message, cuda_warning}` wire shape plus `model_present` and
/// `cuda_runtime_present`, both sourced from `/health` rather than the
/// download-status endpoint itself (FR-17: the app must be able to tell
/// "model missing" apart from "no transfer has ever run"; E13: same
/// reasoning for the CUDA runtime).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelDownloadStatusView {
    pub state: ModelDownloadStateView,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
    pub model_present: bool,
    /// E13: verbatim backend text -- set once a `SetupDownload`'s
    /// CUDA-runtime phase has failed and the setup continued into the model
    /// phase anyway (E4). Never re-worded here.
    pub cuda_warning: Option<String>,
    /// E13: `None` when F2 itself judged this host GPU-less (never prompt
    /// about a runtime that machine could never use).
    pub cuda_runtime_present: Option<bool>,
}

/// Health-derived fields the download status alone cannot answer (FR-17,
/// E13): whether the model is present, and whether the CUDA runtime is.
struct HealthFields {
    model_present: bool,
    cuda_runtime_present: Option<bool>,
}

fn build_view(status: ModelDownloadStatus, health: HealthFields) -> ModelDownloadStatusView {
    ModelDownloadStatusView {
        state: status.state.into(),
        downloaded_bytes: status.downloaded_bytes,
        total_bytes: status.total_bytes,
        percent: status.percent,
        error_kind: status.error_kind,
        error_message: status.error_message,
        model_present: health.model_present,
        cuda_warning: status.cuda_warning,
        cuda_runtime_present: health.cuda_runtime_present,
    }
}

/// Maps a seam-level [`ServiceError`] onto the frozen [`AppError`] taxonomy
/// (`error.rs` takes no new variants) -- `Unavailable`/`Auth` both mean "the
/// operator cannot act on this right now", so both map to
/// `service_unavailable`; a non-2xx or an undecodable body is this app's own
/// `service` kind.
fn map_service_error(err: ServiceError) -> AppError {
    match err {
        ServiceError::Unavailable { detail } => AppError::service_unavailable(detail),
        ServiceError::Auth { message } => AppError::service_unavailable(message),
        ServiceError::Http { status, message } => {
            AppError::service(format!("model download service error {status}: {message}"))
        }
        ServiceError::Decode { message } => AppError::service(message),
    }
}

/// Whatever `/health` currently reports for `model_present`/
/// `cuda_runtime_present`, never panicking on a health call that itself
/// fails -- an unreachable service just means "unknown, so assume the model
/// is still missing and the CUDA runtime state is unknown" rather than
/// surfacing a second error on top of the download status one.
async fn health_fields(state: &AppState) -> HealthFields {
    let service = state.service.read().await.clone();
    service.health().await.map_or(
        HealthFields {
            model_present: false,
            cuda_runtime_present: None,
        },
        |health| HealthFields {
            model_present: health.model_present,
            cuda_runtime_present: health.cuda_runtime_present,
        },
    )
}

/// `model_download_status` handler body (`GET /v1/model/download` proxy).
pub async fn model_download_status_handler(
    state: &AppState,
) -> Result<ModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .model_download_status()
        .await
        .map_err(map_service_error)?;
    Ok(build_view(status, health_fields(state).await))
}

/// `start_model_download` handler body (`POST /v1/model/download` proxy).
pub async fn start_model_download_handler(
    state: &AppState,
) -> Result<ModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .start_model_download()
        .await
        .map_err(map_service_error)?;
    Ok(build_view(status, health_fields(state).await))
}

/// `cancel_model_download` handler body (`DELETE /v1/model/download` proxy).
pub async fn cancel_model_download_handler(
    state: &AppState,
) -> Result<ModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .cancel_model_download()
        .await
        .map_err(map_service_error)?;
    Ok(build_view(status, health_fields(state).await))
}

// -- `#[tauri::command]` wrappers -------------------------------------------
//
// Thin by design, mirroring `commands.rs`'s own convention: each just
// unwraps `tauri::State` and delegates to the handler body above. None
// takes any argument beyond `state` -- see this module's own doc comment.

#[tauri::command]
pub async fn model_download_status(
    state: tauri::State<'_, AppState>,
) -> Result<ModelDownloadStatusView, AppError> {
    model_download_status_handler(&state).await
}

#[tauri::command]
pub async fn start_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<ModelDownloadStatusView, AppError> {
    start_model_download_handler(&state).await
}

#[tauri::command]
pub async fn cancel_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<ModelDownloadStatusView, AppError> {
    cancel_model_download_handler(&state).await
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use tempfile::tempdir;

    use crate::commands::{
        AppState, ServiceStatusSink, ServiceStatusView, UnavailableTranscriptionService,
    };
    use crate::config::Settings;
    use crate::error::ErrorKind;
    use crate::service::fake::FakeService;
    use crate::service::TranscriptionService;

    use super::{
        cancel_model_download_handler, model_download_status_handler, start_model_download_handler,
    };

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    struct NoopStatusSink;
    impl ServiceStatusSink for NoopStatusSink {
        fn emit(&self, _status: &ServiceStatusView) {}
    }

    struct NoopEventSink;
    impl crate::jobs::EventSink for NoopEventSink {
        fn emit(&self, _snapshot: &crate::jobs::JobSnapshot) {}
    }

    fn state_with(service: Arc<dyn TranscriptionService>) -> AppState {
        let dir = tempdir().expect("tempdir");
        AppState::new(
            dir.path().to_path_buf(),
            dir.path().to_path_buf(),
            Settings::default(),
            dir.path().to_path_buf(),
            service,
            None,
            false,
            Arc::new(NoopEventSink),
            Arc::new(NoopStatusSink),
        )
    }

    #[test]
    fn model_download_status_returns_service_unavailable_when_unsupported() {
        run(async {
            let state = state_with(Arc::new(UnavailableTranscriptionService::new(
                "no service configured".to_string(),
            )));
            let err = model_download_status_handler(&state)
                .await
                .expect_err("unsupported service must return a typed error, not panic");
            assert_eq!(err.kind(), ErrorKind::ServiceUnavailable);
        });
    }

    #[test]
    fn start_model_download_proxies_to_the_fake_service_and_reports_progress() {
        run(async {
            let state = state_with(Arc::new(FakeService::with_model_absent()));
            let view = start_model_download_handler(&state)
                .await
                .expect("start_model_download must succeed against the fake");
            assert!(!view.model_present);
        });
    }

    #[test]
    fn model_download_status_carries_the_durable_cuda_runtime_present_flag_with_no_in_session_warning(
    ) {
        // E13 durable half: a fresh process (no `SetupDownload` instance, so
        // `cuda_warning` stays `None`) must still surface
        // `cuda_runtime_present: Some(false)` through the same view the UI
        // consumes, so the frontend can render its persistent notice without
        // an active `cuda_warning`.
        run(async {
            let fake = Arc::new(FakeService::new());
            fake.set_cuda_runtime_missing();
            let state = state_with(fake);

            let view = model_download_status_handler(&state)
                .await
                .expect("model_download_status must succeed against the fake");

            assert!(view.model_present);
            assert_eq!(view.cuda_runtime_present, Some(false));
            assert_eq!(view.cuda_warning, None);
        });
    }

    #[test]
    fn cancel_model_download_returns_to_a_retryable_state() {
        run(async {
            let state = state_with(Arc::new(FakeService::with_model_absent()));
            start_model_download_handler(&state)
                .await
                .expect("start must succeed");
            let view = cancel_model_download_handler(&state)
                .await
                .expect("cancel_model_download must succeed");
            assert!(!view.model_present);
        });
    }
}
