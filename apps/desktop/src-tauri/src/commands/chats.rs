//! Saved project-chat conversations: `<root>/<PROJECT>/chats/<id>.json`.
//!
//! Conversations live in the vault itself, beside the project's meetings
//! (the redesign's "stored in the project" rule) -- so they survive
//! restarts, travel with the vault, and stay readable as plain JSON.
//! `chats` is a reserved project-level directory (`vault::CHATS_DIR_NAME`),
//! excluded from the meeting listing like `reports`.
//!
//! Unlike meetings, chats are addressed by `(project, chat id)` rather than
//! a vault-index id: a chat file is created and named by this module alone,
//! ids are pinned to a hex-uuid shape, and every path is rebuilt from the
//! validated parts -- no caller-supplied path ever reaches the filesystem.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::error::AppError;
use crate::paths;

use super::search::resolve_hit_dir;
use super::AppState;

/// An upper bound on one conversation file; a chat is text, not a vault.
const MAX_CHAT_BYTES: u64 = 4 * 1024 * 1024;

const SCHEMA_VERSION: u32 = 1;

/// One cited source as stored (and shown): named by the meeting's
/// vault-relative directory, never an entry id -- ids are session-scoped
/// and would rot inside a file that outlives the session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatSource {
    pub kind: String,
    /// Vault-root-relative, forward slashes (`ACME/260831 - Title`).
    pub meeting_dir: String,
    pub meeting_name: String,
    #[serde(default)]
    pub timestamp: Option<String>,
    #[serde(default)]
    pub start_sec: Option<f64>,
}

/// A stored source resolved for the UI: `entry_id` present when the meeting
/// is still listed (clickable), absent when it is gone (display-only).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChatSourceView {
    pub entry_id: Option<String>,
    pub kind: String,
    pub meeting_name: String,
    pub timestamp: Option<String>,
    pub start_sec: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatStoredMessage {
    pub role: String,
    pub content: String,
    #[serde(default)]
    pub sources: Vec<ChatSource>,
}

/// A source as the UI sends it for saving: by session entry id (the
/// frontend never holds paths); this module resolves the durable
/// vault-relative directory. An id that no longer resolves stores an empty
/// `meeting_dir` -- the display fields keep the citation legible.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatSourceInput {
    pub entry_id: Option<String>,
    pub kind: String,
    pub meeting_name: String,
    #[serde(default)]
    pub timestamp: Option<String>,
    #[serde(default)]
    pub start_sec: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatMessageInput {
    pub role: String,
    pub content: String,
    #[serde(default)]
    pub sources: Vec<ChatSourceInput>,
}

/// The on-disk shape. Unknown fields are ignored on read so a newer build's
/// files still open in an older one.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct ChatFile {
    #[serde(default)]
    schema_version: u32,
    id: String,
    title: String,
    /// Unix milliseconds; the UI formats.
    created_at_ms: i64,
    updated_at_ms: i64,
    #[serde(default)]
    messages: Vec<ChatStoredMessage>,
}

/// One row of the history list.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChatSummaryView {
    pub id: String,
    pub title: String,
    pub updated_at_ms: i64,
    pub question_count: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChatMessageView {
    pub role: String,
    pub content: String,
    pub sources: Vec<ChatSourceView>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChatConversationView {
    pub id: String,
    pub title: String,
    pub messages: Vec<ChatMessageView>,
}

/// What `save_chat` receives from the UI: `id: None` creates a new file.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatConversationInput {
    pub id: Option<String>,
    pub title: String,
    pub messages: Vec<ChatMessageInput>,
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0)
}

fn validate_chat_id(chat_id: &str) -> Result<(), AppError> {
    let ok = !chat_id.is_empty()
        && chat_id.len() <= 64
        && chat_id
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() || ch == '-');
    if ok {
        Ok(())
    } else {
        Err(AppError::invalid_argument(format!(
            "invalid chat id {chat_id:?}"
        )))
    }
}

/// The project's `chats/` directory, from the validated root + project.
///
/// `project` is untrusted IPC input: it must name an existing directory
/// directly under the meetings root that the listing would treat as a
/// project (not `unsorted`, not a reserved name, no path syntax).
async fn chats_dir(state: &AppState, project: &str) -> Result<PathBuf, AppError> {
    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;
    let root = PathBuf::from(root);

    // One plain directory name: any path syntax, the unsorted bucket and
    // the reserved project-level names are all "not a project".
    let lowered = project.to_lowercase();
    let is_path_syntax = project.is_empty()
        || project == "."
        || project == ".."
        || project.contains(['/', '\\', ':']);
    if is_path_syntax
        || lowered == vault::UNSORTED_DIR_NAME
        || vault::RESERVED_PROJECT_DIR_NAMES
            .iter()
            .any(|reserved| *reserved == lowered)
    {
        return Err(AppError::invalid_argument(format!(
            "not a project: {project:?}"
        )));
    }
    // Containment as defense in depth, existence because the project must
    // already be real -- chats never create one.
    let canonical = paths::ensure_inside(&root, &root.join(project))?;
    let project_dir = paths::strip_verbatim(&canonical);
    if !project_dir.is_dir() {
        return Err(AppError::invalid_argument(format!(
            "unknown project {project:?}"
        )));
    }
    Ok(project_dir.join(vault::CHATS_DIR_NAME))
}

fn read_chat_file(path: &PathBuf) -> Option<ChatFile> {
    let metadata = std::fs::metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_CHAT_BYTES {
        return None;
    }
    let body = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&body).ok()
}

fn write_chat_file(dir: &PathBuf, file: &ChatFile) -> Result<(), AppError> {
    std::fs::create_dir_all(dir)
        .map_err(|err| AppError::io(format!("could not create {}: {err}", dir.display())))?;
    let target = dir.join(format!("{}.json", file.id));
    let temp = dir.join(format!(".{}.json.tmp", file.id));
    let body = serde_json::to_string_pretty(file)
        .map_err(|err| AppError::internal(format!("could not serialize chat: {err}")))?;
    std::fs::write(&temp, body.as_bytes())
        .map_err(|err| AppError::io(format!("could not write {}: {err}", temp.display())))?;
    if let Err(err) = std::fs::rename(&temp, &target) {
        let _ = std::fs::remove_file(&temp);
        return Err(AppError::io(format!(
            "could not replace {}: {err}",
            target.display()
        )));
    }
    Ok(())
}

/// `list_chats` -- the project's saved conversations, newest first.
pub async fn list_chats_handler(
    state: &AppState,
    project: &str,
) -> Result<Vec<ChatSummaryView>, AppError> {
    let dir = chats_dir(state, project).await?;
    tokio::task::spawn_blocking(move || {
        let mut summaries: Vec<ChatSummaryView> = Vec::new();
        let Ok(entries) = std::fs::read_dir(&dir) else {
            return Ok(summaries); // no chats yet: a real state, not an error
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
                continue;
            }
            let Some(file) = read_chat_file(&path) else {
                continue; // unreadable sidecar degrades to "not listed"
            };
            summaries.push(ChatSummaryView {
                question_count: file
                    .messages
                    .iter()
                    .filter(|message| message.role == "user")
                    .count(),
                id: file.id,
                title: file.title,
                updated_at_ms: file.updated_at_ms,
            });
        }
        summaries.sort_by_key(|summary| -summary.updated_at_ms);
        Ok(summaries)
    })
    .await
    .map_err(|join_err| AppError::internal(format!("list_chats task panicked: {join_err}")))?
}

/// `read_chat` -- one conversation, sources resolved to entry ids where the
/// cited meeting is still listed.
pub async fn read_chat_handler(
    state: &AppState,
    project: &str,
    chat_id: &str,
) -> Result<ChatConversationView, AppError> {
    validate_chat_id(chat_id)?;
    let dir = chats_dir(state, project).await?;
    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .map(PathBuf::from)
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;
    let by_path: std::collections::HashMap<PathBuf, String> = state
        .vault_index
        .read()
        .await
        .iter()
        .map(|(id, path)| (path.clone(), id.clone()))
        .collect();

    let path = dir.join(format!("{chat_id}.json"));
    let chat_id = chat_id.to_string();
    tokio::task::spawn_blocking(move || {
        let file = read_chat_file(&path)
            .ok_or_else(|| AppError::invalid_argument(format!("unknown chat {chat_id:?}")))?;
        let messages = file
            .messages
            .into_iter()
            .map(|message| ChatMessageView {
                role: message.role,
                content: message.content,
                sources: message
                    .sources
                    .into_iter()
                    .map(|source| ChatSourceView {
                        entry_id: resolve_hit_dir(&root, &source.meeting_dir)
                            .and_then(|absolute| by_path.get(&absolute).cloned()),
                        kind: source.kind,
                        meeting_name: source.meeting_name,
                        timestamp: source.timestamp,
                        start_sec: source.start_sec,
                    })
                    .collect(),
            })
            .collect();
        Ok(ChatConversationView {
            id: file.id,
            title: file.title,
            messages,
        })
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_chat task panicked: {join_err}")))?
}

/// `save_chat` -- upserts a whole conversation (the UI holds the full
/// transcript of a chat, so a save is by definition wholesale).
pub async fn save_chat_handler(
    state: &AppState,
    project: &str,
    conversation: ChatConversationInput,
) -> Result<ChatSummaryView, AppError> {
    if let Some(id) = conversation.id.as_deref() {
        validate_chat_id(id)?;
    }
    let title = conversation.title.trim().to_string();
    if title.is_empty() {
        return Err(AppError::invalid_argument(
            "a chat needs a title".to_string(),
        ));
    }
    let dir = chats_dir(state, project).await?;

    // Resolve session entry ids into durable vault-relative dirs before the
    // blocking write: ids rot with the session, relative dirs do not.
    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .map(PathBuf::from);
    let index = state.vault_index.read().await.clone();
    let messages: Vec<ChatStoredMessage> = conversation
        .messages
        .into_iter()
        .map(|message| ChatStoredMessage {
            role: message.role,
            content: message.content,
            sources: message
                .sources
                .into_iter()
                .map(|source| {
                    let meeting_dir = source
                        .entry_id
                        .as_deref()
                        .and_then(|id| index.get(id))
                        .zip(root.as_deref())
                        .and_then(|(absolute, root)| absolute.strip_prefix(root).ok())
                        .map(|relative| relative.to_string_lossy().replace('\\', "/"))
                        .unwrap_or_default();
                    ChatSource {
                        kind: source.kind,
                        meeting_dir,
                        meeting_name: source.meeting_name,
                        timestamp: source.timestamp,
                        start_sec: source.start_sec,
                    }
                })
                .collect(),
        })
        .collect();
    let existing_id = conversation.id;

    tokio::task::spawn_blocking(move || {
        let now = now_ms();
        let (id, created_at_ms) = match existing_id {
            Some(id) => {
                let created = read_chat_file(&dir.join(format!("{id}.json")))
                    .map(|existing| existing.created_at_ms)
                    .unwrap_or(now);
                (id, created)
            }
            None => (uuid::Uuid::new_v4().simple().to_string(), now),
        };
        let file = ChatFile {
            schema_version: SCHEMA_VERSION,
            id: id.clone(),
            title: title.clone(),
            created_at_ms,
            updated_at_ms: now,
            messages,
        };
        write_chat_file(&dir, &file)?;
        Ok(ChatSummaryView {
            question_count: file
                .messages
                .iter()
                .filter(|message| message.role == "user")
                .count(),
            id,
            title,
            updated_at_ms: now,
        })
    })
    .await
    .map_err(|join_err| AppError::internal(format!("save_chat task panicked: {join_err}")))?
}

/// `rename_chat` -- retitles a conversation in place.
pub async fn rename_chat_handler(
    state: &AppState,
    project: &str,
    chat_id: &str,
    title: &str,
) -> Result<(), AppError> {
    validate_chat_id(chat_id)?;
    let title = title.trim().to_string();
    if title.is_empty() {
        return Err(AppError::invalid_argument(
            "a chat needs a title".to_string(),
        ));
    }
    let dir = chats_dir(state, project).await?;
    let path = dir.join(format!("{chat_id}.json"));
    let chat_id = chat_id.to_string();
    tokio::task::spawn_blocking(move || {
        let mut file = read_chat_file(&path)
            .ok_or_else(|| AppError::invalid_argument(format!("unknown chat {chat_id:?}")))?;
        file.title = title;
        file.updated_at_ms = now_ms();
        write_chat_file(&dir, &file)
    })
    .await
    .map_err(|join_err| AppError::internal(format!("rename_chat task panicked: {join_err}")))?
}

/// `delete_chat` -- removes one conversation file.
pub async fn delete_chat_handler(
    state: &AppState,
    project: &str,
    chat_id: &str,
) -> Result<(), AppError> {
    validate_chat_id(chat_id)?;
    let dir = chats_dir(state, project).await?;
    let path = dir.join(format!("{chat_id}.json"));
    tokio::task::spawn_blocking(move || {
        std::fs::remove_file(&path)
            .map_err(|err| AppError::io(format!("could not delete {}: {err}", path.display())))
    })
    .await
    .map_err(|join_err| AppError::internal(format!("delete_chat task panicked: {join_err}")))?
}
