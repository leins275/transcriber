//! The LLM feature's `#[tauri::command]` handlers: derived jobs (summary,
//! per-recording export) and the GGUF model-download trio.
//!
//! Every handler follows the house rules the rest of `commands/` set:
//! meetings are named by the opaque id `list_vault` issued (never a path);
//! every path this module derives is containment-checked against the
//! current meetings root before any filesystem read.

use std::path::Path;

use serde::Serialize;

use crate::error::AppError;
use crate::jobs::JobSnapshot;
use crate::service::{
    LlmJobKind, LlmModelsStatus, LlmSubmitRequest, ModelDownloadStatus, ServiceError,
};

use super::meetings::{meeting_name_of, resolve_entry};
use super::model::ModelDownloadStateView;
use super::AppState;

/// The GGUF download's status view -- the whisper trio's shape minus the
/// CUDA fields (the CPU llama.cpp runtime has no runtime phase).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LlmModelDownloadStatusView {
    pub state: ModelDownloadStateView,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
    /// From `/health`'s `llm_model_present` (`false` when the service is
    /// unreachable or predates the feature).
    pub model_present: bool,
    /// From `/health`'s `llm_gpu_build_present`: whether the first-run CUDA
    /// build of the LLM runtime is on disk. `None` = no NVIDIA GPU here (or
    /// unknown) -- the UI only offers "Enable GPU acceleration" on
    /// `Some(false)`.
    pub gpu_build_present: Option<bool>,
}

pub(super) fn map_service_error(err: ServiceError) -> AppError {
    match err {
        ServiceError::Unavailable { detail } => AppError::service_unavailable(detail),
        ServiceError::Auth { message } => AppError::service_unavailable(message),
        ServiceError::Http { status, message } => {
            AppError::service(format!("service error {status}: {message}"))
        }
        ServiceError::Decode { message } => AppError::service(message),
    }
}

/// A meeting that has no transcript has nothing for an LLM job to read.
fn require_transcript(meeting_dir: &Path) -> Result<(), AppError> {
    if meeting_dir.join(vault::TRANSCRIPT_FILE_NAME).is_file() {
        Ok(())
    } else {
        let name = meeting_name_of(meeting_dir);
        Err(AppError::invalid_argument(format!(
            "\"{name}\" has no transcript yet; transcribe it first"
        )))
    }
}

async fn enqueue(state: &AppState, kind: LlmJobKind, input: &Path, output: &Path) -> JobSnapshot {
    state
        .registry
        .read()
        .await
        .enqueue_llm(LlmSubmitRequest {
            kind,
            input_path: input.to_string_lossy().into_owned(),
            output_dir: output.to_string_lossy().into_owned(),
        })
        .await
}

// -- derived-job handlers ---------------------------------------------------

/// `summarize_vault_entry` -- write `<meeting>/summary.md` from the transcript.
pub async fn summarize_vault_entry_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<JobSnapshot, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    require_transcript(&meeting_dir)?;
    Ok(enqueue(state, LlmJobKind::Summarize, &meeting_dir, &meeting_dir).await)
}

/// `export_recording` -- the deterministic per-recording PDF export
/// (Summary -> Transcript) into the meeting folder itself: `export.md`
/// plus the share-named PDF, under stable names, overwritten on re-run
/// (these are regenerable derived documents).
pub async fn export_recording_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<JobSnapshot, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    require_transcript(&meeting_dir)?;
    Ok(enqueue(state, LlmJobKind::Export, &meeting_dir, &meeting_dir).await)
}

// -- GGUF model download ----------------------------------------------------

/// Health-derived fields the download status alone cannot answer:
/// `(model_present, gpu_build_present)`.
async fn llm_health_fields(state: &AppState) -> (bool, Option<bool>) {
    let service = state.service.read().await.clone();
    match service.health().await {
        Ok(health) => (
            health.llm_model_present.unwrap_or(false),
            health.llm_gpu_build_present,
        ),
        Err(_) => (false, None),
    }
}

fn build_llm_view(
    status: ModelDownloadStatus,
    health: (bool, Option<bool>),
) -> LlmModelDownloadStatusView {
    let (model_present, gpu_build_present) = health;
    LlmModelDownloadStatusView {
        state: status.state.into(),
        downloaded_bytes: status.downloaded_bytes,
        total_bytes: status.total_bytes,
        percent: status.percent,
        error_kind: status.error_kind,
        error_message: status.error_message,
        model_present,
        gpu_build_present,
    }
}

/// `llm_model_download_status` handler body.
pub async fn llm_model_download_status_handler(
    state: &AppState,
) -> Result<LlmModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .llm_model_download_status()
        .await
        .map_err(map_service_error)?;
    Ok(build_llm_view(status, llm_health_fields(state).await))
}

/// `start_llm_model_download` handler body.
pub async fn start_llm_model_download_handler(
    state: &AppState,
) -> Result<LlmModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .start_llm_model_download()
        .await
        .map_err(map_service_error)?;
    Ok(build_llm_view(status, llm_health_fields(state).await))
}

/// `cancel_llm_model_download` handler body.
pub async fn cancel_llm_model_download_handler(
    state: &AppState,
) -> Result<LlmModelDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status = service
        .cancel_llm_model_download()
        .await
        .map_err(map_service_error)?;
    Ok(build_llm_view(status, llm_health_fields(state).await))
}

// -- curated model catalog ---------------------------------------------------

/// One curated model's download slot, as the UI renders it -- the plain
/// download fields without the health-derived extras (those live on
/// [`LlmModelsView`], once per listing, not once per row).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LlmModelDownloadView {
    pub state: ModelDownloadStateView,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
}

/// One row of the curated model list.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LlmCatalogModelView {
    pub id: String,
    pub label: String,
    pub file: String,
    pub size_bytes: Option<u64>,
    pub catalog: bool,
    pub present: bool,
    pub active: bool,
    pub download: LlmModelDownloadView,
}

/// `list_llm_models` response: the catalog plus the one machine-level field
/// the rows share (`gpu_build_present`, from `/health`).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LlmModelsView {
    pub active: String,
    pub gpu_build_present: Option<bool>,
    pub models: Vec<LlmCatalogModelView>,
}

fn build_models_view(status: LlmModelsStatus, gpu_build_present: Option<bool>) -> LlmModelsView {
    LlmModelsView {
        active: status.active,
        gpu_build_present,
        models: status
            .models
            .into_iter()
            .map(|row| LlmCatalogModelView {
                id: row.id,
                label: row.label,
                file: row.file,
                size_bytes: row.size_bytes,
                catalog: row.catalog,
                present: row.present,
                active: row.active,
                download: LlmModelDownloadView {
                    state: row.download.state.into(),
                    downloaded_bytes: row.download.downloaded_bytes,
                    total_bytes: row.download.total_bytes,
                    percent: row.download.percent,
                    error_kind: row.download.error_kind,
                    error_message: row.download.error_message,
                },
            })
            .collect(),
    }
}

async fn fetch_models_view(state: &AppState) -> Result<LlmModelsView, AppError> {
    let service = state.service.read().await.clone();
    let status = service.llm_models().await.map_err(map_service_error)?;
    let (_, gpu_build_present) = llm_health_fields(state).await;
    Ok(build_models_view(status, gpu_build_present))
}

/// `list_llm_models` handler body.
pub async fn list_llm_models_handler(state: &AppState) -> Result<LlmModelsView, AppError> {
    fetch_models_view(state).await
}

/// `start_llm_model_download_for` handler body -- starts one catalog
/// model's transfer, then returns the refreshed listing.
pub async fn start_llm_model_download_for_handler(
    state: &AppState,
    model_id: &str,
) -> Result<LlmModelsView, AppError> {
    let service = state.service.read().await.clone();
    service
        .start_llm_model_download_for(model_id)
        .await
        .map_err(map_service_error)?;
    fetch_models_view(state).await
}

/// `cancel_llm_model_download_for` handler body.
pub async fn cancel_llm_model_download_for_handler(
    state: &AppState,
    model_id: &str,
) -> Result<LlmModelsView, AppError> {
    let service = state.service.read().await.clone();
    service
        .cancel_llm_model_download_for(model_id)
        .await
        .map_err(map_service_error)?;
    fetch_models_view(state).await
}

/// `delete_llm_model` handler body. The service enforces its own guards
/// (active model, transfer in flight, LLM job running); the local
/// LLM-job check just answers faster and covers jobs the service has not
/// been handed yet (still pending in this app's registry).
pub async fn delete_llm_model_handler(
    state: &AppState,
    model_id: &str,
) -> Result<LlmModelsView, AppError> {
    if state.registry.read().await.has_active_llm_job().await {
        return Err(AppError::invalid_argument(
            "cannot delete a model while assistant jobs are running",
        ));
    }
    let service = state.service.read().await.clone();
    let status = service
        .delete_llm_model(model_id)
        .await
        .map_err(map_service_error)?;
    let (_, gpu_build_present) = llm_health_fields(state).await;
    Ok(build_models_view(status, gpu_build_present))
}

// -- `#[tauri::command]` wrappers -------------------------------------------

#[tauri::command]
pub async fn summarize_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<JobSnapshot, AppError> {
    summarize_vault_entry_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn export_recording(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<JobSnapshot, AppError> {
    export_recording_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn llm_model_download_status(
    state: tauri::State<'_, AppState>,
) -> Result<LlmModelDownloadStatusView, AppError> {
    llm_model_download_status_handler(&state).await
}

#[tauri::command]
pub async fn start_llm_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<LlmModelDownloadStatusView, AppError> {
    start_llm_model_download_handler(&state).await
}

#[tauri::command]
pub async fn cancel_llm_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<LlmModelDownloadStatusView, AppError> {
    cancel_llm_model_download_handler(&state).await
}

#[tauri::command]
pub async fn list_llm_models(state: tauri::State<'_, AppState>) -> Result<LlmModelsView, AppError> {
    list_llm_models_handler(&state).await
}

#[tauri::command]
pub async fn start_llm_model_download_for(
    state: tauri::State<'_, AppState>,
    model_id: String,
) -> Result<LlmModelsView, AppError> {
    start_llm_model_download_for_handler(&state, &model_id).await
}

#[tauri::command]
pub async fn cancel_llm_model_download_for(
    state: tauri::State<'_, AppState>,
    model_id: String,
) -> Result<LlmModelsView, AppError> {
    cancel_llm_model_download_for_handler(&state, &model_id).await
}

#[tauri::command]
pub async fn delete_llm_model(
    state: tauri::State<'_, AppState>,
    model_id: String,
) -> Result<LlmModelsView, AppError> {
    delete_llm_model_handler(&state, &model_id).await
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex};

    use tempfile::tempdir;

    use crate::commands::{
        list_vault_handler, AppState, Revealer, ServiceStatusSink, ServiceStatusView,
    };
    use crate::config::Settings;
    use crate::error::ErrorKind;
    use crate::service::fake::FakeService;
    use crate::service::TranscriptionService;

    use super::*;

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

    #[derive(Default)]
    struct RecordingRevealer {
        calls: Mutex<Vec<PathBuf>>,
    }

    impl Revealer for RecordingRevealer {
        fn reveal(&self, path: &std::path::Path) -> Result<(), AppError> {
            self.calls
                .lock()
                .expect("revealer mutex poisoned")
                .push(path.to_path_buf());
            Ok(())
        }
    }

    struct NoopSidecar;
    #[async_trait::async_trait]
    impl crate::commands::SidecarController for NoopSidecar {
        async fn spawn_and_await_ready(
            &self,
            _config: &crate::sidecar::SidecarSpawnConfig,
            _timeout: std::time::Duration,
        ) -> Result<crate::sidecar::ReadyLine, crate::sidecar::SidecarError> {
            Err(crate::sidecar::SidecarError::Io {
                message: "no sidecar in tests".to_string(),
            })
        }
        async fn terminate(&self) {}
    }

    fn state_with_root(
        root: PathBuf,
        service: Arc<dyn TranscriptionService>,
        revealer: Arc<dyn Revealer>,
    ) -> AppState {
        let settings = Settings {
            meetings_root: Some(root.to_string_lossy().into_owned()),
            ..Settings::default()
        };
        AppState::new_with(
            root.clone(),
            root.clone(),
            settings,
            root,
            service,
            None,
            false,
            Arc::new(NoopEventSink),
            Arc::new(NoopStatusSink),
            Arc::new(NoopSidecar),
            revealer,
        )
    }

    fn make_meeting(root: &std::path::Path, project: &str, name: &str, with_transcript: bool) {
        let dir = root.join(project).join(name);
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(dir.join("source.mp4"), b"bytes").expect("write source");
        if with_transcript {
            std::fs::write(dir.join("transcript.json"), b"{}").expect("write transcript");
        }
    }

    async fn only_entry_id(state: &AppState) -> String {
        let entries = list_vault_handler(state).await.expect("list vault");
        assert_eq!(entries.len(), 1, "expected exactly one vault entry");
        entries[0].id.clone()
    }

    /// Polls the fake until the worker has forwarded the submission (the
    /// enqueue is asynchronous), then returns it.
    async fn first_llm_submission(fake: &FakeService) -> crate::service::LlmSubmitRequest {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            if let Some(submission) = fake.llm_submissions().into_iter().next() {
                return submission;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "submission never arrived"
            );
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }

    #[test]
    fn summarize_requires_a_transcript() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "ELS", "260101 - Planning", false);
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );
            let id = only_entry_id(&state).await;

            let err = summarize_vault_entry_handler(&state, &id)
                .await
                .expect_err("no transcript must be refused");
            assert_eq!(err.kind(), ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn summarize_submits_a_summarize_job_over_the_meeting_dir() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "ELS", "260101 - Planning", true);
            let fake = Arc::new(FakeService::new());
            let state = state_with_root(
                root.path().to_path_buf(),
                fake.clone(),
                Arc::new(RecordingRevealer::default()),
            );
            let id = only_entry_id(&state).await;

            let snapshot = summarize_vault_entry_handler(&state, &id)
                .await
                .expect("summarize must enqueue");
            assert_eq!(snapshot.job_type, "summarize");

            // The submission reaches the service with input == output ==
            // the meeting dir (poll the fake until the worker has run).
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            loop {
                let submissions = fake.llm_submissions();
                if !submissions.is_empty() {
                    assert_eq!(submissions[0].kind, crate::service::LlmJobKind::Summarize);
                    assert!(submissions[0].input_path.ends_with("260101 - Planning"));
                    assert_eq!(submissions[0].input_path, submissions[0].output_dir);
                    break;
                }
                assert!(
                    std::time::Instant::now() < deadline,
                    "submission never arrived"
                );
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        });
    }

    #[test]
    fn export_recording_lands_in_the_meeting_folder_itself() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "ELS", "260101 - Planning", true);
            let fake = Arc::new(FakeService::new());
            let state = state_with_root(
                root.path().to_path_buf(),
                fake.clone(),
                Arc::new(RecordingRevealer::default()),
            );
            let id = only_entry_id(&state).await;

            let snapshot = export_recording_handler(&state, &id)
                .await
                .expect("export must enqueue");
            assert_eq!(snapshot.job_type, "export");
            let output_dir = snapshot.meeting_dir.expect("output dir recorded");
            assert!(
                output_dir.ends_with("260101 - Planning"),
                "the export output must be the meeting folder itself, got {output_dir:?}"
            );

            let submission = first_llm_submission(&fake).await;
            assert_eq!(submission.kind, crate::service::LlmJobKind::Export);
            assert_eq!(
                submission.input_path, submission.output_dir,
                "input and output are both the meeting folder"
            );
        });
    }

    #[test]
    fn list_llm_models_reports_the_catalog_with_the_default_active() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::with_llm_model_absent()),
                Arc::new(RecordingRevealer::default()),
            );

            let view = list_llm_models_handler(&state).await.expect("list");
            assert_eq!(view.active, "qwen3.5-9b");
            assert_eq!(view.gpu_build_present, Some(true));
            // The catalog is deliberately a single model -- no switching.
            assert_eq!(view.models.len(), 1);
            let active_row = view.models.iter().find(|m| m.active).expect("active row");
            assert_eq!(active_row.id, "qwen3.5-9b");
            assert!(view.models.iter().all(|m| !m.present && m.catalog));
            assert!(view.models.iter().all(|m| m.size_bytes.is_some()));
        });
    }

    #[test]
    fn catalog_download_completes_the_requested_model() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::with_llm_model_absent()),
                Arc::new(RecordingRevealer::default()),
            );

            start_llm_model_download_for_handler(&state, "qwen3.5-9b")
                .await
                .expect("start must succeed");

            // Poll the listing (each poll advances the fake one chunk).
            let mut done = false;
            for _ in 0..10 {
                let view = list_llm_models_handler(&state).await.expect("list");
                let row = view
                    .models
                    .iter()
                    .find(|m| m.id == "qwen3.5-9b")
                    .expect("row");
                if row.present {
                    done = true;
                    break;
                }
            }
            assert!(done, "the requested model's transfer must complete");
        });
    }

    #[test]
    fn delete_llm_model_refuses_the_active_model() {
        run(async {
            let root = tempdir().expect("tempdir");
            // Default fake: the one catalog model present and active --
            // which makes every delete a delete of the active model.
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );

            let err = delete_llm_model_handler(&state, "qwen3.5-9b")
                .await
                .expect_err("deleting the active model must be refused");
            assert_eq!(err.kind(), ErrorKind::Service);
        });
    }

    #[test]
    fn llm_model_download_trio_proxies_the_fake_service() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::with_llm_model_absent()),
                Arc::new(RecordingRevealer::default()),
            );

            let before = llm_model_download_status_handler(&state)
                .await
                .expect("status must succeed");
            assert!(!before.model_present);

            start_llm_model_download_handler(&state)
                .await
                .expect("start must succeed");

            // Poll to completion (the fake advances one chunk per status poll).
            let mut present = false;
            for _ in 0..10 {
                let view = llm_model_download_status_handler(&state)
                    .await
                    .expect("status must succeed");
                if view.model_present {
                    present = true;
                    break;
                }
            }
            assert!(
                present,
                "the fake transfer must complete and flip model_present"
            );
        });
    }
}
