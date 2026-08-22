//! Per-meeting `#[tauri::command]` handlers: read a transcript, rename or
//! re-file a meeting, delete one.
//!
//! These are the write-side counterpart to `list_vault`/`reveal_vault_entry`
//! in the parent module, and they follow the same rule those two established:
//! **the UI names a meeting by the opaque id `list_vault` handed it, never by
//! a path.** Every handler here resolves that id through
//! [`super::AppState::vault_index`], re-validates the resulting path against
//! the *current* meetings root with [`crate::paths::ensure_inside`], and only
//! then calls into F1 — which applies its own containment gate
//! (`vault::manage::resolve_meeting`) a second time. A caller that fabricates
//! an id gets `invalid_argument`; there is no argument on any of these
//! commands that a path could be smuggled through.
//!
//! Renaming keeps the meeting's id stable and rewrites the index entry to the
//! new path, so a UI holding a list from before the rename can still act on
//! that row without re-fetching. Deleting drops the id, so a stale id fails
//! closed rather than resolving to whatever later occupies the path.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::AppError;
use crate::ingest::vault_error_to_app_error;
use crate::paths;

use super::{AppState, VaultMeetingView};

/// An upper bound on the `transcript.json` this will load into memory. A
/// one-hour Russian meeting is ~550 KB, so 64 MiB is far past any real
/// transcript while still refusing to pull an arbitrarily large file (or a
/// non-transcript an operator dropped into the folder under that name)
/// through the IPC boundary in one allocation.
const MAX_TRANSCRIPT_BYTES: u64 = 64 * 1024 * 1024;

/// One segment of a transcript, as the viewer renders it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TranscriptSegmentView {
    pub id: i64,
    /// Seconds from the start of the recording.
    pub start: f64,
    pub end: f64,
    pub text: String,
}

/// A meeting's transcript, shaped for the in-app viewer.
///
/// Deliberately *not* a pass-through of F2's `transcript.json`: the viewer
/// needs the text, the segment timeline and enough provenance to say how the
/// transcript was produced, and nothing else. Per-segment model diagnostics
/// (`avg_logprob`, `no_speech_prob`, word-level timings) stay on disk rather
/// than crossing the IPC boundary for every one of a thousand segments.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TranscriptView {
    /// Echoed back so the UI can match a response to the row it opened.
    pub entry_id: String,
    pub meeting_name: String,
    /// BCP-47-ish language tag as F2 detected it (`"ru"`, `"en"`, …).
    pub language: Option<String>,
    pub created_at: Option<String>,
    pub duration_sec: Option<f64>,
    pub model: Option<String>,
    pub device: Option<String>,
    /// The full transcript text, exactly as F2 wrote it.
    pub text: String,
    pub segments: Vec<TranscriptSegmentView>,
}

/// The subset of F2's on-disk `transcript.json` this module reads.
///
/// Every field is optional and unknown fields are ignored, on purpose: a
/// transcript written by an older (or newer) build of F2 must still open in
/// the viewer rather than failing the whole command over one field that moved.
#[derive(Debug, Deserialize)]
struct TranscriptFile {
    #[serde(default)]
    created_at: Option<String>,
    #[serde(default)]
    language: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    segments: Vec<TranscriptFileSegment>,
    #[serde(default)]
    source: Option<TranscriptFileSource>,
    #[serde(default)]
    provider: Option<TranscriptFileProvider>,
}

#[derive(Debug, Deserialize)]
struct TranscriptFileSegment {
    #[serde(default)]
    id: i64,
    #[serde(default)]
    start: f64,
    #[serde(default)]
    end: f64,
    #[serde(default)]
    text: String,
}

#[derive(Debug, Deserialize)]
struct TranscriptFileSource {
    #[serde(default)]
    duration_sec: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct TranscriptFileProvider {
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    device: Option<String>,
}

/// Parses a `transcript.json` body into the viewer's shape.
///
/// Pure over the file's bytes so the parsing rules (missing `text`
/// reconstructed from the segments; a malformed body reported as an
/// actionable error rather than a panic) are unit-testable without a
/// filesystem or an `AppState`.
fn parse_transcript(
    entry_id: &str,
    meeting_name: &str,
    body: &str,
) -> Result<TranscriptView, AppError> {
    let parsed: TranscriptFile = serde_json::from_str(body).map_err(|err| {
        AppError::vault(format!(
            "transcript.json for \"{meeting_name}\" could not be read: {err}"
        ))
    })?;

    let segments: Vec<TranscriptSegmentView> = parsed
        .segments
        .into_iter()
        .map(|segment| TranscriptSegmentView {
            id: segment.id,
            start: segment.start,
            end: segment.end,
            text: segment.text,
        })
        .collect();

    // F2 always writes `text`, but a transcript that somehow lacks it is
    // still perfectly readable from its segments — reconstruct rather than
    // show the operator an empty viewer over a file that plainly has content.
    let text = match parsed.text {
        Some(text) if !text.trim().is_empty() => text,
        _ => segments
            .iter()
            .map(|segment| segment.text.trim())
            .filter(|line| !line.is_empty())
            .collect::<Vec<_>>()
            .join(" "),
    };

    Ok(TranscriptView {
        entry_id: entry_id.to_string(),
        meeting_name: meeting_name.to_string(),
        language: parsed.language,
        created_at: parsed.created_at,
        duration_sec: parsed.source.and_then(|source| source.duration_sec),
        model: parsed
            .provider
            .as_ref()
            .and_then(|provider| provider.model.clone()),
        device: parsed
            .provider
            .as_ref()
            .and_then(|provider| provider.device.clone()),
        text,
        segments,
    })
}

/// Resolves a vault entry id to a meeting folder that still lives inside the
/// *currently* configured meetings root.
///
/// Both halves matter: the id lookup is what stops the UI naming an arbitrary
/// path, and the containment re-check is what stops an id issued before the
/// operator changed their meetings root from still resolving into the old one.
async fn resolve_entry(state: &AppState, entry_id: &str) -> Result<(PathBuf, PathBuf), AppError> {
    let target = state
        .vault_index
        .read()
        .await
        .get(entry_id)
        .cloned()
        .ok_or_else(|| AppError::invalid_argument(format!("unknown vault entry id {entry_id}")))?;

    let root = state
        .settings
        .read()
        .await
        .meetings_root
        .clone()
        .ok_or_else(|| AppError::not_configured("no meetings root has been configured yet"))?;
    let root_path = PathBuf::from(&root);

    let canonical = paths::ensure_inside(&root_path, &target)?;
    Ok((root_path, paths::strip_verbatim(&canonical)))
}

/// The meeting folder's own name, for messages and the returned view.
fn meeting_name_of(meeting_dir: &Path) -> String {
    meeting_dir
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Rebuilds the view for one meeting folder after it has moved, re-deriving
/// the same fields `list_vault` reports (project from the parent folder,
/// source/transcript presence from the folder's own contents) so the UI can
/// update that row in place without a full re-listing.
fn view_for(id: &str, root: &Path, meeting_dir: &Path) -> VaultMeetingView {
    let project = meeting_dir
        .parent()
        .filter(|parent| *parent != root)
        .and_then(|parent| parent.file_name())
        .and_then(|name| name.to_str())
        .and_then(|name| vault::code::validate(name).ok())
        .map(|code| code.as_str().to_string());

    VaultMeetingView {
        id: id.to_string(),
        project,
        meeting_name: meeting_name_of(meeting_dir),
        meeting_dir: meeting_dir.to_string_lossy().into_owned(),
        has_source: has_source_file(meeting_dir),
        has_transcript: meeting_dir.join(vault::TRANSCRIPT_FILE_NAME).is_file(),
    }
}

/// Whether the folder holds a `source.*` recording — the same existence-only
/// check `vault::list` makes, repeated here because a single moved folder
/// does not warrant re-listing the whole vault.
fn has_source_file(meeting_dir: &Path) -> bool {
    let Ok(children) = std::fs::read_dir(meeting_dir) else {
        return false;
    };
    children.flatten().any(|entry| {
        entry.file_type().is_ok_and(|file_type| file_type.is_file())
            && entry
                .file_name()
                .to_str()
                .and_then(|name| name.split('.').next())
                .is_some_and(|stem| stem.eq_ignore_ascii_case(vault::SOURCE_STEM))
    })
}

/// `read_transcript` — loads `<meeting>/transcript.json` for the meeting an
/// id names and returns it in the viewer's shape.
///
/// Read-only, and the only command in this crate that returns a file's
/// *contents* rather than a path — which is exactly why the size cap
/// ([`MAX_TRANSCRIPT_BYTES`]) is checked from the file's metadata before a
/// single byte is read.
pub async fn read_transcript_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<TranscriptView, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let meeting_name = meeting_name_of(&meeting_dir);
    let transcript_path = meeting_dir.join(vault::TRANSCRIPT_FILE_NAME);
    let entry_id = entry_id.to_string();

    tokio::task::spawn_blocking(move || {
        let metadata = std::fs::metadata(&transcript_path).map_err(|_| {
            AppError::invalid_argument(format!("\"{meeting_name}\" has no transcript yet"))
        })?;
        if !metadata.is_file() {
            return Err(AppError::invalid_argument(format!(
                "\"{meeting_name}\" has no transcript yet"
            )));
        }
        if metadata.len() > MAX_TRANSCRIPT_BYTES {
            return Err(AppError::vault(format!(
                "transcript.json for \"{meeting_name}\" is {} bytes, larger than this app will open",
                metadata.len()
            )));
        }
        let body = std::fs::read_to_string(&transcript_path).map_err(|err| {
            AppError::io(format!(
                "could not read transcript.json for \"{meeting_name}\": {err}"
            ))
        })?;
        parse_transcript(&entry_id, &meeting_name, &body)
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_transcript task panicked: {join_err}")))?
}

/// `update_vault_entry` — renames and/or re-files an existing meeting.
///
/// `project: None` files the meeting under `unsorted/`; `Some(code)` files it
/// under that project, creating or reusing the folder case-insensitively.
/// Validation of all three parts happens inside F1
/// (`vault::manage::rename_meeting`) against exactly the rules ingest applies
/// to a filename, so the app never grows a second, drifting copy of the
/// naming convention.
///
/// The entry keeps its id: the index is rewritten to the new path so the
/// row the operator just renamed stays actionable.
pub async fn update_vault_entry_handler(
    state: &AppState,
    entry_id: &str,
    project: Option<String>,
    date: &str,
    title: &str,
) -> Result<VaultMeetingView, AppError> {
    let (root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let update = vault::MeetingUpdate {
        project,
        date: date.to_string(),
        title: title.to_string(),
    };

    let moved = {
        let root = root.clone();
        tokio::task::spawn_blocking(move || vault::rename_meeting(&root, &meeting_dir, &update))
            .await
            .map_err(|join_err| {
                AppError::internal(format!("update_vault_entry task panicked: {join_err}"))
            })?
            .map_err(vault_error_to_app_error)?
    };

    // Defense in depth, mirroring `list_vault_handler`: never surface a path
    // F1 returned that does not resolve back inside the configured root.
    let canonical = paths::ensure_inside(&root, &moved)?;
    let moved = paths::strip_verbatim(&canonical);

    state
        .vault_index
        .write()
        .await
        .insert(entry_id.to_string(), moved.clone());

    Ok(view_for(entry_id, &root, &moved))
}

/// `delete_vault_entry` — hands the meeting folder to the OS recycle bin.
///
/// Recoverable by construction (F1 never calls `remove_dir_all`), so the
/// confirmation this needs is a UI concern rather than a second command. The
/// id is dropped from the index afterwards so it fails closed.
pub async fn delete_vault_entry_handler(state: &AppState, entry_id: &str) -> Result<(), AppError> {
    let (root, meeting_dir) = resolve_entry(state, entry_id).await?;

    tokio::task::spawn_blocking(move || vault::delete_meeting(&root, &meeting_dir))
        .await
        .map_err(|join_err| {
            AppError::internal(format!("delete_vault_entry task panicked: {join_err}"))
        })?
        .map_err(vault_error_to_app_error)?;

    state.vault_index.write().await.remove(entry_id);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const BODY: &str = r#"{
        "schema_version": 1,
        "created_at": "2026-08-22T15:29:58.306874+00:00",
        "source": {"path": "C:\\v\\source.mp4", "filename": "source.mp4", "duration_sec": 3625.8},
        "provider": {"name": "local", "model": "large-v3", "device": "cuda", "compute_type": "float16"},
        "language": "ru",
        "language_probability": 0.997,
        "text": " Да, ребят, всем привет.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.5, "text": " Да, ребят,", "avg_logprob": -0.2},
            {"id": 1, "start": 2.5, "end": 4.0, "text": " всем привет.", "avg_logprob": -0.1}
        ],
        "stats": {"elapsed_sec": 120.0, "realtime_factor": 0.03, "cost_usd": null, "currency": null}
    }"#;

    #[test]
    fn parses_provenance_text_and_segments() {
        let view = parse_transcript("entry-1", "260822 - source", BODY).expect("should parse");

        assert_eq!(view.entry_id, "entry-1");
        assert_eq!(view.meeting_name, "260822 - source");
        assert_eq!(view.language.as_deref(), Some("ru"));
        assert_eq!(view.model.as_deref(), Some("large-v3"));
        assert_eq!(view.device.as_deref(), Some("cuda"));
        assert_eq!(view.duration_sec, Some(3625.8));
        assert_eq!(view.text, " Да, ребят, всем привет.");
        assert_eq!(view.segments.len(), 2);
        assert_eq!(view.segments[1].id, 1);
        assert_eq!(view.segments[1].start, 2.5);
        assert_eq!(view.segments[1].text, " всем привет.");
    }

    #[test]
    fn cyrillic_survives_both_the_escaped_and_the_plain_encoding() {
        // Transcripts written before F2 switched to `ensure_ascii=False`
        // carry `\uXXXX` escapes; both forms are valid JSON and must reach
        // the viewer as the same characters.
        let escaped = r#"{"text": "\u0414\u0430", "segments": []}"#;
        let plain = r#"{"text": "Да", "segments": []}"#;

        let from_escaped = parse_transcript("e", "m", escaped).expect("escaped parses");
        let from_plain = parse_transcript("e", "m", plain).expect("plain parses");

        assert_eq!(from_escaped.text, "Да");
        assert_eq!(from_escaped.text, from_plain.text);
    }

    #[test]
    fn a_transcript_missing_its_text_field_is_rebuilt_from_its_segments() {
        let body = r#"{"segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "text": " first"},
            {"id": 1, "start": 1.0, "end": 2.0, "text": " second"}
        ]}"#;

        let view = parse_transcript("e", "m", body).expect("should parse");

        assert_eq!(view.text, "first second");
        assert_eq!(view.segments.len(), 2);
    }

    #[test]
    fn unknown_fields_and_a_missing_provider_do_not_fail_the_read() {
        let body = r#"{"text": "hello", "segments": [], "some_future_field": {"a": 1}}"#;

        let view = parse_transcript("e", "m", body).expect("should parse");

        assert_eq!(view.text, "hello");
        assert_eq!(view.model, None);
        assert_eq!(view.device, None);
        assert_eq!(view.duration_sec, None);
    }

    #[test]
    fn a_malformed_transcript_is_an_actionable_error_naming_the_meeting() {
        let err =
            parse_transcript("e", "260822 - source", "{not json").expect_err("should not parse");

        assert!(
            err.message().contains("260822 - source"),
            "message was {:?}",
            err.message()
        );
    }

    #[test]
    fn view_for_reads_the_project_from_the_parent_folder_and_capitalizes_it() {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path();
        let meeting = root.join("els").join("260812 - Security issue");
        std::fs::create_dir_all(&meeting).expect("create meeting");
        std::fs::write(meeting.join("source.mp4"), b"bytes").expect("write source");

        let view = view_for("entry-1", root, &meeting);

        assert_eq!(view.project.as_deref(), Some("ELS"));
        assert_eq!(view.meeting_name, "260812 - Security issue");
        assert!(view.has_source);
        assert!(!view.has_transcript);
    }

    #[test]
    fn view_for_reports_no_project_for_an_unsorted_meeting() {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path();
        let meeting = root.join("unsorted").join("260822 - source");
        std::fs::create_dir_all(&meeting).expect("create meeting");
        std::fs::write(meeting.join("transcript.json"), b"{}").expect("write transcript");

        let view = view_for("entry-1", root, &meeting);

        assert_eq!(view.project, None);
        assert!(!view.has_source);
        assert!(view.has_transcript);
    }
}
