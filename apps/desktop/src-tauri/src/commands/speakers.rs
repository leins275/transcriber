//! Speaker identification: the `#[tauri::command]` handlers behind the
//! Settings page's Speakers row (the pyannote/torch runtime fetch, the
//! pinned model fetch, the prerequisite status), the per-meeting "Identify
//! speakers" job, and the one-shot backfill over every meeting the operator
//! labelled by hand while it had no diarization.
//!
//! Recognition itself needs no new state on this side: when a new recording
//! is diarized, the service joins each sibling meeting's `speakers.json` to
//! the voice embeddings stored in its `transcript.json` and pre-names the
//! voices it recognizes. What an installed app lacked was everything
//! upstream of that -- the runtime, the gated models, the switch, and
//! embeddings for the meetings already labelled. This module is that
//! plumbing; every handler follows the house rules the rest of `commands/`
//! set (meetings named by their opaque id, never a path; service errors
//! mapped through `llm::map_service_error`).

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::llm::{map_service_error, require_transcript};
use super::meetings::{meeting_name_of, read_speaker_labels, resolve_entry, source_file_in};
use super::model::ModelDownloadStateView;
use super::AppState;
use crate::error::AppError;
use crate::jobs::JobSnapshot;
use crate::service::{DiarizationStatus, LlmJobKind, LlmSubmitRequest, ModelDownloadStatus};

/// The IPC view of `GET /v1/diarization/status`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DiarizationStatusView {
    pub runtime_present: bool,
    pub model_present: bool,
    pub token_present: bool,
    pub enabled: bool,
    pub gpu_present: bool,
    pub runtime_total_bytes: u64,
}

impl From<DiarizationStatus> for DiarizationStatusView {
    fn from(status: DiarizationStatus) -> Self {
        DiarizationStatusView {
            runtime_present: status.runtime_present,
            model_present: status.model_present,
            token_present: status.token_present,
            enabled: status.enabled,
            gpu_present: status.gpu_present,
            runtime_total_bytes: status.runtime_total_bytes,
        }
    }
}

/// One of the two download slots' status -- the plain download fields (the
/// presence flags live on [`DiarizationStatusView`], one fetch away).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DiarizationDownloadStatusView {
    pub state: ModelDownloadStateView,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    pub error_kind: Option<String>,
    pub error_message: Option<String>,
}

fn download_view(status: ModelDownloadStatus) -> DiarizationDownloadStatusView {
    DiarizationDownloadStatusView {
        state: status.state.into(),
        downloaded_bytes: status.downloaded_bytes,
        total_bytes: status.total_bytes,
        percent: status.percent,
        error_kind: status.error_kind,
        error_message: status.error_message,
    }
}

// -- status + download slots ---------------------------------------------------

pub async fn diarization_status_handler(
    state: &AppState,
) -> Result<DiarizationStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .diarization_status()
        .await
        .map(DiarizationStatusView::from)
        .map_err(map_service_error)
}

pub async fn diarization_runtime_download_status_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .diarization_runtime_download_status()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

pub async fn start_diarization_runtime_download_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .start_diarization_runtime_download()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

pub async fn cancel_diarization_runtime_download_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .cancel_diarization_runtime_download()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

pub async fn diarization_model_download_status_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .diarization_model_download_status()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

pub async fn start_diarization_model_download_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .start_diarization_model_download()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

pub async fn cancel_diarization_model_download_handler(
    state: &AppState,
) -> Result<DiarizationDownloadStatusView, AppError> {
    let service = state.service.read().await.clone();
    service
        .cancel_diarization_model_download()
        .await
        .map(download_view)
        .map_err(map_service_error)
}

// -- the jobs ------------------------------------------------------------------

async fn enqueue_diarize(state: &AppState, meeting_dir: &Path) -> JobSnapshot {
    let dir = meeting_dir.to_string_lossy().into_owned();
    state
        .registry
        .read()
        .await
        .enqueue_llm(LlmSubmitRequest {
            kind: LlmJobKind::Diarize,
            input_path: dir.clone(),
            output_dir: dir,
        })
        .await
}

/// `diarize_vault_entry` -- identify the speakers in one already-transcribed
/// meeting. Needs both its transcript (the labels go into it) and its
/// recording (the voices come out of it); refuses otherwise, before any
/// job exists.
pub async fn diarize_vault_entry_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<JobSnapshot, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    require_transcript(&meeting_dir)?;
    let has_source = {
        let meeting_dir = meeting_dir.clone();
        tokio::task::spawn_blocking(move || source_file_in(&meeting_dir).is_some())
            .await
            .map_err(|join_err| {
                AppError::internal(format!("diarize_vault_entry task panicked: {join_err}"))
            })?
    };
    if !has_source {
        let name = meeting_name_of(&meeting_dir);
        return Err(AppError::invalid_argument(format!(
            "\"{name}\" has no recording to identify speakers in"
        )));
    }
    Ok(enqueue_diarize(state, &meeting_dir).await)
}

/// Enough of `transcript.json` to tell whether a diarization pass ever ran
/// over it: the block is present (succeeded *or* failed) or absent.
#[derive(Deserialize)]
struct TranscriptPeek {
    #[serde(default)]
    diarization: Option<serde::de::IgnoredAny>,
}

/// Same cap as the transcript reader in `meetings.rs`.
const MAX_TRANSCRIPT_BYTES: u64 = 64 * 1024 * 1024;

fn has_diarization_block(meeting_dir: &Path) -> bool {
    let path = meeting_dir.join(vault::TRANSCRIPT_FILE_NAME);
    let Ok(metadata) = std::fs::metadata(&path) else {
        return false;
    };
    if !metadata.is_file() || metadata.len() > MAX_TRANSCRIPT_BYTES {
        return false;
    }
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return false;
    };
    serde_json::from_str::<TranscriptPeek>(&raw)
        .map(|peek| peek.diarization.is_some())
        .unwrap_or(false)
}

/// The meetings the backfill targets: transcribed, recording still on
/// disk, at least one hand-made speaker label, and no diarization pass
/// yet -- oldest first, so the project's voice memory grows in the order
/// the meetings happened. Pure over the filesystem; blocking.
pub fn labelled_undiarized_meetings(root: &Path) -> Vec<PathBuf> {
    let mut candidates: Vec<(String, PathBuf)> = vault::list_meetings(root)
        .into_iter()
        .filter(|entry| entry.has_transcript && entry.has_source)
        .filter(|entry| {
            read_speaker_labels(&entry.meeting_dir)
                .assignments
                .values()
                .any(|name| !name.trim().is_empty())
        })
        .filter(|entry| !has_diarization_block(&entry.meeting_dir))
        .map(|entry| (entry.meeting_name.clone(), entry.meeting_dir))
        .collect();
    candidates.sort_by(|a, b| a.0.cmp(&b.0));
    candidates.into_iter().map(|(_, dir)| dir).collect()
}

/// `diarize_labelled_meetings` -- queue one `diarize` job per meeting the
/// operator labelled by hand before diarization existed on this machine,
/// so those labels become voice memory for every future recording in the
/// same project. Answers how many were queued; each job then reports
/// through the ordinary job feed. Meetings with a job already in flight
/// are skipped rather than double-queued.
pub async fn diarize_labelled_meetings_handler(state: &AppState) -> Result<u32, AppError> {
    let root = {
        let settings = state.settings.read().await;
        settings
            .meetings_root
            .clone()
            .map(PathBuf::from)
            .ok_or_else(|| AppError::not_configured("meetings root is not configured"))?
    };
    let candidates = tokio::task::spawn_blocking(move || labelled_undiarized_meetings(&root))
        .await
        .map_err(|join_err| {
            AppError::internal(format!(
                "diarize_labelled_meetings task panicked: {join_err}"
            ))
        })?;

    let mut queued = 0u32;
    for meeting_dir in candidates {
        if state
            .registry
            .read()
            .await
            .has_active_job_for(&meeting_dir)
            .await
        {
            continue;
        }
        enqueue_diarize(state, &meeting_dir).await;
        queued += 1;
    }
    Ok(queued)
}

// -- `#[tauri::command]` wrappers -------------------------------------------

#[tauri::command]
pub async fn diarization_status(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationStatusView, AppError> {
    diarization_status_handler(&state).await
}

#[tauri::command]
pub async fn diarization_runtime_download_status(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    diarization_runtime_download_status_handler(&state).await
}

#[tauri::command]
pub async fn start_diarization_runtime_download(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    start_diarization_runtime_download_handler(&state).await
}

#[tauri::command]
pub async fn cancel_diarization_runtime_download(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    cancel_diarization_runtime_download_handler(&state).await
}

#[tauri::command]
pub async fn diarization_model_download_status(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    diarization_model_download_status_handler(&state).await
}

#[tauri::command]
pub async fn start_diarization_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    start_diarization_model_download_handler(&state).await
}

#[tauri::command]
pub async fn cancel_diarization_model_download(
    state: tauri::State<'_, AppState>,
) -> Result<DiarizationDownloadStatusView, AppError> {
    cancel_diarization_model_download_handler(&state).await
}

#[tauri::command]
pub async fn diarize_vault_entry(
    state: tauri::State<'_, AppState>,
    entry_id: String,
) -> Result<JobSnapshot, AppError> {
    diarize_vault_entry_handler(&state, &entry_id).await
}

#[tauri::command]
pub async fn diarize_labelled_meetings(state: tauri::State<'_, AppState>) -> Result<u32, AppError> {
    diarize_labelled_meetings_handler(&state).await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One vault meeting folder in the state the backfill decides on:
    /// `(has a recording, transcript.json body, speakers.json body)`.
    fn write_meeting(
        root: &Path,
        name: &str,
        source: bool,
        transcript: Option<&str>,
        speakers: Option<&str>,
    ) -> PathBuf {
        let dir = root.join("ACME").join(name);
        std::fs::create_dir_all(&dir).unwrap();
        if source {
            std::fs::write(dir.join("source.wav"), b"audio").unwrap();
        }
        if let Some(body) = transcript {
            std::fs::write(dir.join(vault::TRANSCRIPT_FILE_NAME), body).unwrap();
        }
        if let Some(body) = speakers {
            std::fs::write(dir.join("speakers.json"), body).unwrap();
        }
        dir
    }

    const UNDIARIZED: &str = r#"{"schema_version":1,"segments":[{"id":0,"text":"hi"}]}"#;
    const DIARIZED: &str =
        r#"{"schema_version":1,"segments":[],"diarization":{"status":"succeeded","model":"m"}}"#;
    const FAILED_PASS: &str =
        r#"{"schema_version":1,"segments":[],"diarization":{"status":"failed","model":"m"}}"#;
    const LABELLED: &str = r#"{"schema_version":1,"assignments":{"0":"Anna"}}"#;
    const BLANK_LABELS: &str = r#"{"schema_version":1,"assignments":{"0":"  "}}"#;

    #[test]
    fn the_backfill_targets_hand_labelled_undiarized_meetings_oldest_first() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        let newer = write_meeting(
            root,
            "260902 - Later",
            true,
            Some(UNDIARIZED),
            Some(LABELLED),
        );
        let older = write_meeting(
            root,
            "260830 - Earlier",
            true,
            Some(UNDIARIZED),
            Some(LABELLED),
        );
        // Every reason to skip, one per meeting.
        write_meeting(
            root,
            "260831 - Diarized",
            true,
            Some(DIARIZED),
            Some(LABELLED),
        );
        write_meeting(
            root,
            "260831 - Pass failed",
            true,
            Some(FAILED_PASS),
            Some(LABELLED),
        );
        write_meeting(root, "260831 - Unlabelled", true, Some(UNDIARIZED), None);
        write_meeting(
            root,
            "260831 - Blank labels",
            true,
            Some(UNDIARIZED),
            Some(BLANK_LABELS),
        );
        write_meeting(
            root,
            "260831 - No recording",
            false,
            Some(UNDIARIZED),
            Some(LABELLED),
        );
        write_meeting(root, "260831 - No transcript", true, None, Some(LABELLED));

        let targets = labelled_undiarized_meetings(root);

        assert_eq!(targets, vec![older, newer]);
    }

    #[test]
    fn an_unreadable_transcript_counts_as_undiarized() {
        let root = tempfile::tempdir().unwrap();
        let dir = write_meeting(
            root.path(),
            "260901 - Broken",
            true,
            Some("not json"),
            Some(LABELLED),
        );
        // `vault::list_meetings` reports the transcript as present (the
        // file exists); the peek cannot prove a pass ran, so the meeting
        // stays a candidate and the service decides what to do with it.
        assert_eq!(labelled_undiarized_meetings(root.path()), vec![dir]);
    }
}
