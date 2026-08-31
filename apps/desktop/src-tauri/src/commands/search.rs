//! Hybrid vault search: the `search_vault` command.
//!
//! The service names meetings by vault-root-relative directory; this module
//! maps every hit back to the opaque entry id `list_vault` issued (the
//! id-not-path rule). A hit that resolves to no listed meeting is dropped
//! -- fail closed: a meeting deleted since the last listing simply does not
//! appear, and no path ever crosses the IPC boundary.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::error::AppError;
use crate::paths;
use crate::service::{IndexStatus, SearchQuery};

use super::AppState;

/// Longest accepted query; the service caps at 500 too, this just fails
/// earlier and locally.
const MAX_QUERY_CHARS: usize = 500;

/// Results per search -- fixed here rather than exposed to the UI (YAGNI).
const TOP_K: u32 = 20;

/// Resolves a service hit's vault-relative `meeting_dir` to the exact
/// `PathBuf` shape `vault_index` stores: joined onto the root, then
/// canonicalized and verbatim-stripped exactly as `list_vault_handler`
/// records entries -- a raw join is NOT enough (a root reached through a
/// short-name or symlinked component, e.g. a CI temp dir, canonicalizes
/// differently than it prints). `None` when the directory is gone or
/// escapes the root: the hit is dropped, fail closed.
pub(super) fn resolve_hit_dir(root: &Path, relative: &str) -> Option<PathBuf> {
    let joined = root.join(relative.replace('/', std::path::MAIN_SEPARATOR_STR));
    let canonical = paths::ensure_inside(root, &joined).ok()?;
    Some(paths::strip_verbatim(&canonical))
}

/// One search hit, shaped for the library's search results list.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SearchResultView {
    /// The vault entry id (`list_vault`'s), never a path.
    pub entry_id: String,
    /// `"transcript" | "summary" | "note"` -- which document matched.
    pub kind: String,
    pub meeting_name: String,
    pub project: Option<String>,
    pub snippet: String,
    pub score: f64,
    pub start_sec: Option<f64>,
    pub timestamp: Option<String>,
}

/// One meeting's row of the index-status panel (display-only; the panel's
/// rows are not navigation, so no entry-id mapping happens here).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct IndexMeetingView {
    pub name: String,
    /// `"indexed" | "pending" | "no_transcript"`.
    pub state: String,
    pub chunks: u64,
}

/// The index-status panel's data.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct IndexStatusView {
    pub project: String,
    /// Unix seconds of the last completed index pass, if any.
    pub updated_at_sec: Option<i64>,
    pub indexing: bool,
    pub progress: Option<f64>,
    pub indexed_count: u64,
    pub total_count: u64,
    pub meetings: Vec<IndexMeetingView>,
}

/// `index_status` -- one project's index state for the chat tab's chip and
/// its expandable panel.
pub async fn index_status_handler(
    state: &AppState,
    project: String,
) -> Result<IndexStatusView, AppError> {
    let service = state.service.read().await.clone();
    let status: IndexStatus = service
        .index_status(&project)
        .await
        .map_err(super::llm::map_service_error)?;
    Ok(IndexStatusView {
        project: status.project,
        updated_at_sec: status.updated_at,
        indexing: status.indexing,
        progress: status.progress,
        indexed_count: status.indexed_count,
        total_count: status.total_count,
        meetings: status
            .meetings
            .into_iter()
            .map(|meeting| IndexMeetingView {
                name: meeting.name,
                state: meeting.state,
                chunks: meeting.chunks,
            })
            .collect(),
    })
}

/// `reindex_vault` -- asks the service for an incremental index pass NOW,
/// with the error surfaced (unlike the quiet after-job submissions): this
/// backs the Settings button and the once-per-session catch-up at startup,
/// where "the service refused" is information the operator should see.
pub async fn reindex_vault_handler(state: &AppState) -> Result<(), AppError> {
    let service = state.service.read().await.clone();
    service
        .submit_index()
        .await
        .map(|_job_id| ())
        .map_err(super::llm::map_service_error)
}

/// `search_vault` -- hybrid search over transcripts, summaries and notes.
pub async fn search_vault_handler(
    state: &AppState,
    query: String,
    project: Option<String>,
) -> Result<Vec<SearchResultView>, AppError> {
    let query = query.trim().to_string();
    if query.is_empty() {
        return Ok(Vec::new());
    }
    if query.chars().count() > MAX_QUERY_CHARS {
        return Err(AppError::invalid_argument(format!(
            "search query is too long (over {MAX_QUERY_CHARS} characters)"
        )));
    }

    let service = state.service.read().await.clone();
    let hits = service
        .search(SearchQuery {
            query,
            project,
            top_k: Some(TOP_K),
        })
        .await
        .map_err(super::llm::map_service_error)?;

    // Reverse map: meeting path -> entry id. Hits arrive as vault-root-
    // relative forward-slash paths; joining against the current root and
    // comparing `PathBuf`s makes the separator difference irrelevant.
    let root = PathBuf::from(
        state
            .settings
            .read()
            .await
            .meetings_root
            .clone()
            .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?,
    );
    let index = state.vault_index.read().await;
    let by_path: std::collections::HashMap<&PathBuf, &String> =
        index.iter().map(|(id, path)| (path, id)).collect();

    let mut results = Vec::with_capacity(hits.len());
    for hit in hits {
        let Some(absolute) = resolve_hit_dir(&root, &hit.meeting_dir) else {
            continue; // gone from disk, or outside the current root
        };
        let Some(entry_id) = by_path.get(&absolute) else {
            continue; // not listed (deleted since the last list_vault)
        };
        results.push(SearchResultView {
            entry_id: (*entry_id).clone(),
            kind: hit.kind,
            meeting_name: hit
                .meeting_dir
                .rsplit('/')
                .next()
                .unwrap_or(&hit.meeting_title)
                .to_string(),
            project: (hit.project != "unsorted").then_some(hit.project),
            snippet: hit.snippet,
            score: hit.score,
            start_sec: hit.start_sec,
            timestamp: hit.timestamp,
        });
    }
    Ok(results)
}
