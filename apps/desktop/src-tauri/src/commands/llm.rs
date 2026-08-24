//! The LLM feature's `#[tauri::command]` handlers: derived jobs (summary,
//! action items, facts, per-recording export) and the GGUF model-download
//! trio.
//!
//! Every handler follows the house rules the rest of `commands/` set:
//! meetings are named by the opaque id `list_vault` issued (never a path);
//! every path this module derives is containment-checked against the
//! current meetings root before any filesystem read.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::error::AppError;
use crate::jobs::{self, JobSnapshot};
use crate::service::{LlmJobKind, LlmSubmitRequest, ModelDownloadStatus, ServiceError};
use vault::{ArtifactKind, EXPORTS_DIR_NAME};

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

fn map_service_error(err: ServiceError) -> AppError {
    match err {
        ServiceError::Unavailable { detail } => AppError::service_unavailable(detail),
        ServiceError::Auth { message } => AppError::service_unavailable(message),
        ServiceError::Http { status, message } => {
            AppError::service(format!("service error {status}: {message}"))
        }
        ServiceError::Decode { message } => AppError::service(message),
    }
}

/// The meeting's project directory, or an actionable refusal for an
/// unsorted meeting -- project-level artifacts need a project to live in.
fn require_project_dir(root: &Path, meeting_dir: &Path) -> Result<PathBuf, AppError> {
    let refusal = || {
        AppError::invalid_argument(
            "this recording is not filed under a project yet; assign it a project first",
        )
    };
    let parent = meeting_dir.parent().ok_or_else(refusal)?;
    if parent == root {
        return Err(refusal());
    }
    let name = parent
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(refusal)?;
    // `code::validate` also rejects the reserved `unsorted` word.
    vault::code::validate(name).map_err(|_| refusal())?;
    Ok(parent.to_path_buf())
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

/// `<parent>/<today YYMMDD>` -- the dated folder an export lands in.
/// Re-running on the same day reuses (and overwrites) the same folder:
/// these are regenerable derived documents, and one per day is the history
/// granularity the layout encodes.
fn dated_subdir(parent: &Path) -> PathBuf {
    parent.join(jobs::today_yymmdd())
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

/// `extract_vault_entry` -- extract action items or facts into the
/// project-level artifact directory.
pub async fn extract_vault_entry_handler(
    state: &AppState,
    entry_id: &str,
    kind: &str,
) -> Result<JobSnapshot, AppError> {
    let (job_kind, artifact_kind) = match kind {
        "action_items" => (LlmJobKind::ActionItems, ArtifactKind::ActionItems),
        "facts" => (LlmJobKind::Facts, ArtifactKind::Facts),
        other => {
            return Err(AppError::invalid_argument(format!(
                "unknown extraction kind {other:?}; expected \"action_items\" or \"facts\""
            )))
        }
    };
    let (root, meeting_dir) = resolve_entry(state, entry_id).await?;
    require_transcript(&meeting_dir)?;
    let project_dir = require_project_dir(&root, &meeting_dir)?;
    let output = project_dir.join(artifact_kind.dir_name());
    Ok(enqueue(state, job_kind, &meeting_dir, &output).await)
}

/// `export_recording` -- the deterministic per-recording PDF export
/// (Summary -> Action items -> Facts -> Transcript) into
/// `<meeting>/exports/<YYMMDD>/`.
pub async fn export_recording_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<JobSnapshot, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    require_transcript(&meeting_dir)?;
    let output = dated_subdir(&meeting_dir.join(EXPORTS_DIR_NAME));
    Ok(enqueue(state, LlmJobKind::Export, &meeting_dir, &output).await)
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

// -- `#[tauri::command]` wrappers -------------------------------------------

#[tauri::command]
pub async fn summarize_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<JobSnapshot, AppError> {
    summarize_vault_entry_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn extract_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
    kind: String,
) -> Result<JobSnapshot, AppError> {
    extract_vault_entry_handler(&state, &entry_id, &kind).await
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
    fn extraction_on_an_unsorted_meeting_is_refused_with_an_actionable_message() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path(), "unsorted", "260101 - dropped", true);
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );
            let id = only_entry_id(&state).await;

            let err = extract_vault_entry_handler(&state, &id, "action_items")
                .await
                .expect_err("unsorted must be refused");
            assert_eq!(err.kind(), ErrorKind::InvalidArgument);
            assert!(err.message().contains("project"));
        });
    }

    #[test]
    fn extraction_targets_the_project_level_artifact_directory() {
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

            let snapshot = extract_vault_entry_handler(&state, &id, "facts")
                .await
                .expect("extraction must enqueue");
            assert_eq!(snapshot.job_type, "facts");

            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            loop {
                let submissions = fake.llm_submissions();
                if !submissions.is_empty() {
                    let expected = root.path().join("ELS").join("facts");
                    assert!(
                        submissions[0].output_dir.ends_with(
                            expected
                                .strip_prefix(root.path())
                                .unwrap()
                                .to_string_lossy()
                                .as_ref()
                        ),
                        "output {:?} must be the project facts dir",
                        submissions[0].output_dir
                    );
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
    fn export_recording_lands_in_a_dated_exports_subfolder() {
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
            let meeting_dir = snapshot.meeting_dir.expect("output dir recorded");
            assert!(meeting_dir.contains("exports"));
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
