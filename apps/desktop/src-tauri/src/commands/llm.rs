//! The LLM feature's `#[tauri::command]` handlers: derived jobs (summary,
//! action items, facts, per-recording export, project-essence report),
//! project-artifact browsing, and the GGUF model-download trio.
//!
//! Every handler follows the house rules the rest of `commands/` set:
//! meetings are named by the opaque id `list_vault` issued (never a path);
//! projects are named by their validated code (never a path); every path
//! this module derives is containment-checked against the current meetings
//! root before any filesystem read; and file contents cross the IPC
//! boundary only behind explicit size caps. Screenshots reach the webview
//! as `data:image/png;base64,...` URLs because the webview deliberately has
//! no filesystem access (CSP `img-src 'self' data:`).

use std::path::{Path, PathBuf};

use base64::Engine as _;
use serde::Serialize;
use serde_json::Value;

use crate::error::AppError;
use crate::jobs::{self, JobSnapshot};
use crate::paths;
use crate::service::{LlmJobKind, LlmSubmitRequest, ModelDownloadStatus, ServiceError};
use vault::{ArtifactKind, EXPORTS_DIR_NAME, REPORTS_DIR_NAME};

use super::meetings::{meeting_name_of, resolve_entry};
use super::model::ModelDownloadStateView;
use super::AppState;

/// Cap on one artifact's markdown crossing the IPC boundary (matches
/// `MAX_SUMMARY_BYTES`' reasoning: these are prose).
const MAX_ARTIFACT_MD_BYTES: u64 = 4 * 1024 * 1024;

/// Cap on one screenshot; PyAV writes PNG frames of a screen recording,
/// which sit far under this.
const MAX_IMAGE_BYTES: u64 = 8 * 1024 * 1024;

/// Cap on the sum of images returned by one `read_artifact` call.
const MAX_TOTAL_IMAGE_BYTES: u64 = 32 * 1024 * 1024;

/// One artifact folder as the project page lists it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ArtifactView {
    pub slug: String,
    pub screenshot_count: usize,
}

/// One artifact opened for reading: its markdown (front matter stripped
/// into `meta`) and its screenshots as data URLs.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ArtifactContentView {
    pub slug: String,
    /// Flat front-matter keys (`title`, `type`/`kind`, `source_meeting`,
    /// `timestamps`, ...), values as the JSON they were written as.
    pub meta: serde_json::Map<String, Value>,
    /// The markdown body, front matter removed.
    pub markdown: String,
    pub images: Vec<ArtifactImageView>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ArtifactImageView {
    pub name: String,
    /// `data:image/png;base64,...`
    pub data_url: String,
}

/// One dated report folder as the project page lists it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ReportView {
    pub name: String,
    pub has_markdown: bool,
    pub has_pdf: bool,
}

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

/// The configured meetings root, or `not_configured`.
async fn meetings_root(state: &AppState) -> Result<PathBuf, AppError> {
    state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .map(PathBuf::from)
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))
}

/// Rejects any project/kind/slug/report-name argument that is not a single,
/// safe path component -- the same lexical rule `vault::paths` applies.
fn require_safe_component(value: &str, what: &str) -> Result<(), AppError> {
    let unsafe_component = value.is_empty()
        || value == "."
        || value == ".."
        || value.contains('/')
        || value.contains('\\')
        || value.contains(':');
    if unsafe_component {
        return Err(AppError::invalid_argument(format!(
            "invalid {what}: {value:?}"
        )));
    }
    Ok(())
}

/// Resolves a validated project code to its directory under the root
/// (case-insensitive, like the rest of the crate), or `invalid_argument`.
async fn resolve_project_dir(
    state: &AppState,
    project: &str,
) -> Result<(PathBuf, PathBuf), AppError> {
    let code = vault::code::validate(project)
        .map_err(|_| AppError::invalid_argument(format!("invalid project code {project:?}")))?;
    let root = meetings_root(state).await?;
    let code = code.as_str().to_string();
    let root_for_scan = root.clone();
    let found = tokio::task::spawn_blocking(move || {
        let entries = std::fs::read_dir(&root_for_scan).ok()?;
        entries
            .flatten()
            .filter(|entry| entry.file_type().is_ok_and(|t| t.is_dir()))
            .find(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| name.eq_ignore_ascii_case(&code))
            })
            .map(|entry| entry.path())
    })
    .await
    .map_err(|join_err| AppError::internal(format!("project scan panicked: {join_err}")))?
    .ok_or_else(|| AppError::invalid_argument(format!("no project {project:?} in the vault")))?;

    let canonical = paths::ensure_inside(&root, &found)?;
    Ok((root, paths::strip_verbatim(&canonical)))
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

/// `<parent>/<today YYMMDD>` -- the dated folder a report/export lands in.
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

/// `export_project_essence` -- the LLM status report over everything the
/// project holds, into `<project>/reports/<YYMMDD>/`.
pub async fn export_project_essence_handler(
    state: &AppState,
    project: &str,
) -> Result<JobSnapshot, AppError> {
    let (_root, project_dir) = resolve_project_dir(state, project).await?;
    let output = dated_subdir(&project_dir.join(REPORTS_DIR_NAME));
    Ok(enqueue(state, LlmJobKind::Report, &project_dir, &output).await)
}

// -- artifact browsing ------------------------------------------------------

fn artifact_kind_from(kind: &str) -> Result<ArtifactKind, AppError> {
    match kind {
        "action_items" => Ok(ArtifactKind::ActionItems),
        "facts" => Ok(ArtifactKind::Facts),
        other => Err(AppError::invalid_argument(format!(
            "unknown artifact kind {other:?}; expected \"action_items\" or \"facts\""
        ))),
    }
}

/// `list_project_artifacts` -- the slugs under `<project>/<kind>/`.
pub async fn list_project_artifacts_handler(
    state: &AppState,
    project: &str,
    kind: &str,
) -> Result<Vec<ArtifactView>, AppError> {
    let artifact_kind = artifact_kind_from(kind)?;
    let (root, _project_dir) = resolve_project_dir(state, project).await?;
    let project = project.to_string();
    let entries = tokio::task::spawn_blocking(move || {
        vault::list_project_artifacts(&root, &project, artifact_kind)
    })
    .await
    .map_err(|join_err| AppError::internal(format!("artifact listing panicked: {join_err}")))?;

    Ok(entries
        .into_iter()
        .map(|entry| ArtifactView {
            slug: entry.slug,
            screenshot_count: entry.screenshot_names.len(),
        })
        .collect())
}

/// Splits a leading `---` front-matter block off `text`; values parse as
/// JSON where possible (they were written as JSON), else stay strings.
fn split_front_matter(text: &str) -> (serde_json::Map<String, Value>, String) {
    let mut meta = serde_json::Map::new();
    if !text.starts_with("---") {
        return (meta, text.to_string());
    }
    let lines: Vec<&str> = text.lines().collect();
    let Some(end) = lines.iter().skip(1).position(|line| *line == "---") else {
        return (meta, text.to_string());
    };
    for line in &lines[1..=end] {
        let Some((key, raw_value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let raw_value = raw_value.trim();
        let value = serde_json::from_str::<Value>(raw_value)
            .unwrap_or_else(|_| Value::String(raw_value.to_string()));
        meta.insert(key.to_string(), value);
    }
    let body = lines[end + 2..].join("\n");
    (meta, body.trim_start_matches('\n').to_string())
}

/// `read_artifact` -- one item's markdown + screenshots, size-capped, in a
/// single IPC round trip.
pub async fn read_artifact_handler(
    state: &AppState,
    project: &str,
    kind: &str,
    slug: &str,
) -> Result<ArtifactContentView, AppError> {
    let artifact_kind = artifact_kind_from(kind)?;
    require_safe_component(slug, "artifact slug")?;
    let (root, project_dir) = resolve_project_dir(state, project).await?;

    let item_dir = project_dir.join(artifact_kind.dir_name()).join(slug);
    let canonical = paths::ensure_inside(&root, &item_dir)?;
    let item_dir = paths::strip_verbatim(&canonical);
    let slug = slug.to_string();

    tokio::task::spawn_blocking(move || {
        let md_path = item_dir.join(format!("{slug}.md"));
        let metadata = std::fs::metadata(&md_path)
            .map_err(|_| AppError::invalid_argument(format!("no artifact {slug:?}")))?;
        if !metadata.is_file() || metadata.len() > MAX_ARTIFACT_MD_BYTES {
            return Err(AppError::vault(format!(
                "artifact {slug:?} is not readable (missing or larger than this app will open)"
            )));
        }
        let text = std::fs::read_to_string(&md_path)
            .map_err(|err| AppError::io(format!("could not read {slug:?}: {err}")))?;
        let (meta, markdown) = split_front_matter(&text);

        let mut images = Vec::new();
        let mut total_bytes: u64 = 0;
        if let Ok(children) = std::fs::read_dir(&item_dir) {
            let mut names: Vec<String> = children
                .flatten()
                .filter(|entry| entry.file_type().is_ok_and(|t| t.is_file()))
                .filter_map(|entry| entry.file_name().to_str().map(str::to_string))
                .filter(|name| name.to_ascii_lowercase().ends_with(".png"))
                .collect();
            names.sort();
            for name in names {
                let path = item_dir.join(&name);
                let Ok(image_meta) = std::fs::metadata(&path) else {
                    continue;
                };
                if image_meta.len() > MAX_IMAGE_BYTES
                    || total_bytes + image_meta.len() > MAX_TOTAL_IMAGE_BYTES
                {
                    continue;
                }
                let Ok(bytes) = std::fs::read(&path) else {
                    continue;
                };
                total_bytes += bytes.len() as u64;
                let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
                images.push(ArtifactImageView {
                    name,
                    data_url: format!("data:image/png;base64,{encoded}"),
                });
            }
        }

        Ok(ArtifactContentView {
            slug,
            meta,
            markdown,
            images,
        })
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_artifact task panicked: {join_err}")))?
}

/// `reveal_artifact` -- opens Explorer selecting the item's markdown file.
pub async fn reveal_artifact_handler(
    state: &AppState,
    project: &str,
    kind: &str,
    slug: &str,
) -> Result<(), AppError> {
    let artifact_kind = artifact_kind_from(kind)?;
    require_safe_component(slug, "artifact slug")?;
    let (root, project_dir) = resolve_project_dir(state, project).await?;
    let md_path = project_dir
        .join(artifact_kind.dir_name())
        .join(slug)
        .join(format!("{slug}.md"));
    let canonical = paths::ensure_inside(&root, &md_path)?;
    let display_path = paths::strip_verbatim(&canonical);
    let revealer = state.revealer.clone();
    tokio::task::spawn_blocking(move || revealer.reveal(&display_path))
        .await
        .map_err(|join_err| AppError::internal(format!("reveal task panicked: {join_err}")))?
}

/// `list_project_reports` -- the dated report folders, newest first.
pub async fn list_project_reports_handler(
    state: &AppState,
    project: &str,
) -> Result<Vec<ReportView>, AppError> {
    let (root, _project_dir) = resolve_project_dir(state, project).await?;
    let project = project.to_string();
    let reports = tokio::task::spawn_blocking(move || vault::list_reports(&root, &project))
        .await
        .map_err(|join_err| AppError::internal(format!("report listing panicked: {join_err}")))?;
    Ok(reports
        .into_iter()
        .map(|report| ReportView {
            name: report.name,
            has_markdown: report.md_path.is_some(),
            has_pdf: report.pdf_path.is_some(),
        })
        .collect())
}

/// `read_report` -- one dated report's markdown.
pub async fn read_report_handler(
    state: &AppState,
    project: &str,
    name: &str,
) -> Result<String, AppError> {
    require_safe_component(name, "report name")?;
    let (root, project_dir) = resolve_project_dir(state, project).await?;
    let md_path = project_dir
        .join(REPORTS_DIR_NAME)
        .join(name)
        .join("report.md");
    let canonical = paths::ensure_inside(&root, &md_path)?;
    let md_path = paths::strip_verbatim(&canonical);
    let name = name.to_string();

    tokio::task::spawn_blocking(move || {
        let metadata = std::fs::metadata(&md_path)
            .map_err(|_| AppError::invalid_argument(format!("no report {name:?}")))?;
        if !metadata.is_file() || metadata.len() > MAX_ARTIFACT_MD_BYTES {
            return Err(AppError::vault(format!(
                "report {name:?} is not readable (missing or larger than this app will open)"
            )));
        }
        std::fs::read_to_string(&md_path)
            .map_err(|err| AppError::io(format!("could not read report {name:?}: {err}")))
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_report task panicked: {join_err}")))?
}

/// `reveal_report` -- opens Explorer selecting the PDF (or the markdown
/// when no PDF was rendered).
pub async fn reveal_report_handler(
    state: &AppState,
    project: &str,
    name: &str,
) -> Result<(), AppError> {
    require_safe_component(name, "report name")?;
    let (root, project_dir) = resolve_project_dir(state, project).await?;
    let report_dir = project_dir.join(REPORTS_DIR_NAME).join(name);
    let pdf = report_dir.join("report.pdf");
    let target = if pdf.is_file() {
        pdf
    } else {
        report_dir.join("report.md")
    };
    let canonical = paths::ensure_inside(&root, &target)?;
    let display_path = paths::strip_verbatim(&canonical);
    let revealer = state.revealer.clone();
    tokio::task::spawn_blocking(move || revealer.reveal(&display_path))
        .await
        .map_err(|join_err| AppError::internal(format!("reveal task panicked: {join_err}")))?
}

// -- GGUF model download ----------------------------------------------------

async fn llm_model_present(state: &AppState) -> bool {
    let service = state.service.read().await.clone();
    service
        .health()
        .await
        .ok()
        .and_then(|health| health.llm_model_present)
        .unwrap_or(false)
}

fn build_llm_view(status: ModelDownloadStatus, model_present: bool) -> LlmModelDownloadStatusView {
    LlmModelDownloadStatusView {
        state: status.state.into(),
        downloaded_bytes: status.downloaded_bytes,
        total_bytes: status.total_bytes,
        percent: status.percent,
        error_kind: status.error_kind,
        error_message: status.error_message,
        model_present,
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
    Ok(build_llm_view(status, llm_model_present(state).await))
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
    Ok(build_llm_view(status, llm_model_present(state).await))
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
    Ok(build_llm_view(status, llm_model_present(state).await))
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
pub async fn export_project_essence(
    state: tauri::State<'_, AppState>,
    project: String,
) -> Result<JobSnapshot, AppError> {
    export_project_essence_handler(&state, &project).await
}

#[tauri::command]
pub async fn list_project_artifacts(
    state: tauri::State<'_, AppState>,
    project: String,
    kind: String,
) -> Result<Vec<ArtifactView>, AppError> {
    list_project_artifacts_handler(&state, &project, &kind).await
}

#[tauri::command]
pub async fn read_artifact(
    state: tauri::State<'_, AppState>,
    project: String,
    kind: String,
    slug: String,
) -> Result<ArtifactContentView, AppError> {
    read_artifact_handler(&state, &project, &kind, &slug).await
}

#[tauri::command]
pub async fn reveal_artifact(
    state: tauri::State<'_, AppState>,
    project: String,
    kind: String,
    slug: String,
) -> Result<(), AppError> {
    reveal_artifact_handler(&state, &project, &kind, &slug).await
}

#[tauri::command]
pub async fn list_project_reports(
    state: tauri::State<'_, AppState>,
    project: String,
) -> Result<Vec<ReportView>, AppError> {
    list_project_reports_handler(&state, &project).await
}

#[tauri::command]
pub async fn read_report(
    state: tauri::State<'_, AppState>,
    project: String,
    name: String,
) -> Result<String, AppError> {
    read_report_handler(&state, &project, &name).await
}

#[tauri::command]
pub async fn reveal_report(
    state: tauri::State<'_, AppState>,
    project: String,
    name: String,
) -> Result<(), AppError> {
    reveal_report_handler(&state, &project, &name).await
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
    fn export_project_essence_requires_an_existing_project() {
        run(async {
            let root = tempdir().expect("tempdir");
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );

            let err = export_project_essence_handler(&state, "NOPE")
                .await
                .expect_err("missing project must be refused");
            assert_eq!(err.kind(), ErrorKind::InvalidArgument);

            let err = export_project_essence_handler(&state, "not a code")
                .await
                .expect_err("an invalid code must be refused");
            assert_eq!(err.kind(), ErrorKind::InvalidArgument);
        });
    }

    #[test]
    fn artifacts_round_trip_with_front_matter_meta_and_data_url_images() {
        run(async {
            let root = tempdir().expect("tempdir");
            let item_dir = root
                .path()
                .join("ELS")
                .join("action items")
                .join("fix-login");
            std::fs::create_dir_all(&item_dir).expect("mkdir");
            std::fs::write(
                item_dir.join("fix-login.md"),
                "---\ntype: \"task\"\ntitle: \"Fix login\"\ntimestamps: [10.0]\n---\n\n# Fix login\n\nBody text.\n",
            )
            .expect("write md");
            std::fs::write(item_dir.join("screenshot-0010.png"), b"\x89PNGdata")
                .expect("write png");

            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );

            let listed = list_project_artifacts_handler(&state, "ELS", "action_items")
                .await
                .expect("list must succeed");
            assert_eq!(listed.len(), 1);
            assert_eq!(listed[0].slug, "fix-login");
            assert_eq!(listed[0].screenshot_count, 1);

            let content = read_artifact_handler(&state, "ELS", "action_items", "fix-login")
                .await
                .expect("read must succeed");
            assert_eq!(content.meta.get("type"), Some(&serde_json::json!("task")));
            assert_eq!(
                content.meta.get("title"),
                Some(&serde_json::json!("Fix login"))
            );
            assert!(content.markdown.starts_with("# Fix login"));
            assert!(
                !content.markdown.contains("---"),
                "front matter is stripped"
            );
            assert_eq!(content.images.len(), 1);
            assert!(content.images[0]
                .data_url
                .starts_with("data:image/png;base64,"));
        });
    }

    #[test]
    fn artifact_slug_arguments_cannot_smuggle_a_path() {
        run(async {
            let root = tempdir().expect("tempdir");
            std::fs::create_dir_all(root.path().join("ELS")).expect("mkdir");
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                Arc::new(RecordingRevealer::default()),
            );

            for bad in ["..", "a/b", "a\\b", "C:evil", ""] {
                let err = read_artifact_handler(&state, "ELS", "facts", bad)
                    .await
                    .expect_err("unsafe slug must be refused");
                assert_eq!(err.kind(), ErrorKind::InvalidArgument, "slug {bad:?}");
            }
        });
    }

    #[test]
    fn reports_list_and_read_and_reveal_prefers_the_pdf() {
        run(async {
            let root = tempdir().expect("tempdir");
            let report_dir = root.path().join("ELS").join("reports").join("260101");
            std::fs::create_dir_all(&report_dir).expect("mkdir");
            std::fs::write(report_dir.join("report.md"), "# Status\n").expect("write md");
            std::fs::write(report_dir.join("report.pdf"), b"%PDF-").expect("write pdf");

            let revealer = Arc::new(RecordingRevealer::default());
            let state = state_with_root(
                root.path().to_path_buf(),
                Arc::new(FakeService::new()),
                revealer.clone(),
            );

            let reports = list_project_reports_handler(&state, "ELS")
                .await
                .expect("list must succeed");
            assert_eq!(reports.len(), 1);
            assert!(reports[0].has_pdf);

            let markdown = read_report_handler(&state, "ELS", "260101")
                .await
                .expect("read must succeed");
            assert_eq!(markdown, "# Status\n");

            reveal_report_handler(&state, "ELS", "260101")
                .await
                .expect("reveal must succeed");
            let calls = revealer.calls.lock().expect("calls").clone();
            assert!(calls[0].ends_with("report.pdf"));
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
