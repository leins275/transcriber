//! The project chat: `chat_stream` (SSE forwarded over a Tauri ipc
//! channel) and `cancel_chat`.
//!
//! A `tauri::ipc::Channel` is scoped to one command invocation -- exactly
//! the lifetime of one chat turn -- which is why it beats window events
//! here (no correlation ids, no global ordering questions). Source hits
//! are mapped to entry ids through the same reverse lookup `search_vault`
//! uses; a hit that resolves to no listed meeting is dropped.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use tauri::ipc::Channel;

use crate::error::AppError;
use crate::service::{ChatEvent, ChatMessage, ChatRequest};

use super::search::SearchResultView;
use super::AppState;

/// One prior turn, as the frontend sends it.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatMessageArg {
    pub role: String,
    pub content: String,
}

/// One chat event, as the frontend's channel receives it.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ChatEventView {
    Delta { text: String },
    Sources { sources: Vec<SearchResultView> },
    Done { finish_reason: String },
    Error { message: String },
}

/// `chat_stream` -- one streamed answer from the local LLM over the
/// project's materials. Resolves (the command returns) when the stream is
/// over; the events arrive on `on_event` while it runs.
pub async fn chat_stream_handler(
    state: &AppState,
    messages: Vec<ChatMessageArg>,
    project: Option<String>,
    on_event: Channel<ChatEventView>,
) -> Result<(), AppError> {
    if messages.is_empty() {
        return Err(AppError::invalid_argument(
            "a chat needs at least one message".to_string(),
        ));
    }
    for message in &messages {
        if message.role != "user" && message.role != "assistant" {
            return Err(AppError::invalid_argument(format!(
                "unknown chat role {:?}",
                message.role
            )));
        }
    }

    let request = ChatRequest {
        messages: messages
            .into_iter()
            .map(|message| ChatMessage {
                role: message.role,
                content: message.content,
            })
            .collect(),
        project,
    };

    // Stashing a new sender DROPS the previous one, which fires its
    // receiver: a new question cancels the stream it supersedes.
    let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel::<()>();
    {
        let mut slot = state
            .chat_cancel
            .lock()
            .expect("chat cancel mutex poisoned");
        *slot = Some(cancel_tx);
    }

    // The reverse map for citation hits, captured before the stream starts
    // (the vault cannot change under a running chat in any way that
    // matters: a rename mid-answer just drops that citation).
    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .map(PathBuf::from);
    let by_path: std::collections::HashMap<PathBuf, String> = state
        .vault_index
        .read()
        .await
        .iter()
        .map(|(id, path)| (path.clone(), id.clone()))
        .collect();

    let service = state.service.read().await.clone();
    let forward = move |event: ChatEvent| {
        let view = match event {
            ChatEvent::Delta { text } => ChatEventView::Delta { text },
            ChatEvent::Done { finish_reason } => ChatEventView::Done { finish_reason },
            ChatEvent::Error { message } => ChatEventView::Error { message },
            ChatEvent::Sources { sources } => ChatEventView::Sources {
                sources: sources
                    .into_iter()
                    .filter_map(|hit| {
                        let root = root.as_ref()?;
                        let absolute =
                            root.join(hit.meeting_dir.replace('/', std::path::MAIN_SEPARATOR_STR));
                        let entry_id = by_path.get(&absolute)?.clone();
                        Some(SearchResultView {
                            entry_id,
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
                        })
                    })
                    .collect(),
            },
        };
        // A send failure only means the webview navigated away; the stream
        // is being cancelled through the slot anyway.
        let _ = on_event.send(view);
    };

    let result = service
        .chat_stream(request, Box::new(forward), cancel_rx)
        .await
        .map_err(super::llm::map_service_error);

    // Clear the slot -- but only if it still holds *this* turn's sender;
    // a superseding turn already replaced it and must keep its own.
    {
        let mut slot = state
            .chat_cancel
            .lock()
            .expect("chat cancel mutex poisoned");
        if slot.as_ref().is_some_and(|sender| sender.is_closed()) {
            // `is_closed` is true once our receiver is gone (the stream
            // ended); a fresh superseding sender's receiver is still alive.
            *slot = None;
        }
    }
    result
}

/// `cancel_chat` -- stops the in-flight chat turn, if any.
pub async fn cancel_chat_handler(state: &AppState) -> Result<(), AppError> {
    let sender = {
        let mut slot = state
            .chat_cancel
            .lock()
            .expect("chat cancel mutex poisoned");
        slot.take()
    };
    if let Some(sender) = sender {
        // Either delivers or the stream already ended; both are fine.
        let _ = sender.send(());
    }
    Ok(())
}
