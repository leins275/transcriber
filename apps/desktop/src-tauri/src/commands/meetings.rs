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

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::AppError;
use crate::ingest::vault_error_to_app_error;
use crate::jobs::JobSnapshot;
use crate::paths;
use crate::service::ServiceError;

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
    /// `segment id -> speaker name`, from the meeting's `speakers.json`.
    /// Empty for a transcript nobody has labelled — a real state, not an
    /// error. Sent with the transcript rather than fetched separately: the
    /// reading view cannot render a segment without knowing who said it,
    /// so two round trips would only buy a visible relabelling flicker.
    pub speakers: HashMap<String, String>,
    /// Where `transcript.json` actually is, for the reading view's footer.
    pub transcript_path: String,
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
    /// F2's diarization label ("Speaker 1", ...), when the transcription ran
    /// with speaker classification on. Absent otherwise.
    #[serde(default)]
    speaker: Option<String>,
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

    // Diarized labels ship inside the transcript itself; they seed the
    // viewer's speaker map so an auto-classified transcript opens already
    // labelled. The handler overlays `speakers.json` on top afterwards —
    // an operator's hand-applied label always outranks the model's.
    let mut diarized: HashMap<String, String> = HashMap::new();
    let segments: Vec<TranscriptSegmentView> = parsed
        .segments
        .into_iter()
        .map(|segment| {
            if let Some(speaker) = segment.speaker.filter(|name| !name.trim().is_empty()) {
                diarized.insert(segment.id.to_string(), speaker);
            }
            TranscriptSegmentView {
                id: segment.id,
                start: segment.start,
                end: segment.end,
                text: segment.text,
            }
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
        // Seeded with the transcript's own diarized labels; the handler
        // overlays the meeting folder's `speakers.json` on top.
        speakers: diarized,
        transcript_path: String::new(),
    })
}

/// Resolves a vault entry id to a meeting folder that still lives inside the
/// *currently* configured meetings root.
///
/// Both halves matter: the id lookup is what stops the UI naming an arbitrary
/// path, and the containment re-check is what stops an id issued before the
/// operator changed their meetings root from still resolving into the old one.
pub(super) async fn resolve_entry(
    state: &AppState,
    entry_id: &str,
) -> Result<(PathBuf, PathBuf), AppError> {
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
pub(super) fn meeting_name_of(meeting_dir: &Path) -> String {
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
        let mut view = parse_transcript(&entry_id, &meeting_name, &body)?;
        // Manual labels overlay (and outrank) the diarized ones the parser
        // seeded: an operator's correction must never be undone by what the
        // model guessed, while segments the operator never touched keep
        // their automatic label.
        for (segment_id, name) in read_speaker_labels(&meeting_dir).assignments {
            view.speakers.insert(segment_id, name);
        }
        view.transcript_path = transcript_path.to_string_lossy().into_owned();
        Ok(view)
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

    // Refuse to move the folder while any job is still working on (or into)
    // it: a running transcription or LLM job holds the old path, and the
    // rename would either fail it mid-flight or have it write its output
    // into a re-created stale folder next to the renamed one.
    let registry = state.registry.read().await.clone();
    if registry.has_active_job_for(&meeting_dir).await {
        let meeting_name = meeting_name_of(&meeting_dir);
        return Err(AppError::invalid_argument(format!(
            "\"{meeting_name}\" has a job still running; wait for it to finish \
             (or cancel it) before renaming"
        )));
    }

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

/// The file speaker labels are kept in, beside `transcript.json` in the
/// meeting folder.
///
/// A sidecar rather than a field inside `transcript.json`, for one reason:
/// `transcript.json` is F2's artifact, written whole by
/// `transcript.write_atomic` every time a recording is transcribed. Labels
/// the operator applied by hand must survive a re-transcription, and the
/// only way to guarantee that is to keep them in a file F2 never touches.
const SPEAKERS_FILE_NAME: &str = "speakers.json";

/// An upper bound on the labels file. One entry per segment of a very long
/// meeting is still tens of kilobytes; a megabyte means something other
/// than labels is in that file.
const MAX_SPEAKERS_BYTES: u64 = 1024 * 1024;

/// The schema version written into `speakers.json`.
const SPEAKERS_SCHEMA_VERSION: u32 = 1;

/// Speaker labels for one meeting: a segment id to speaker-name map.
///
/// Keyed by segment id rather than by turn, deliberately. Turns are a
/// *display* grouping the UI derives from pauses between segments, and that
/// grouping changes the moment the recording is transcribed again with
/// different segmentation. An assignment stored per segment survives that;
/// one stored per turn index would silently reattach itself to whatever
/// speech later occupies that position.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SpeakerLabels {
    /// `segment id -> speaker name`. A segment with no entry is unassigned,
    /// which is a real state and not an error: a transcript nobody has
    /// labelled yet has none of these.
    #[serde(default)]
    pub assignments: HashMap<String, String>,
}

/// `speakers.json`'s on-disk shape.
#[derive(Debug, Deserialize, Serialize)]
struct SpeakersFile {
    #[serde(default)]
    schema_version: u32,
    #[serde(default)]
    assignments: HashMap<String, String>,
}

/// Reads a meeting's speaker labels, or an empty set when the file is
/// absent.
///
/// A missing file is the normal state, never an error. A *malformed* one is
/// also not an error here: labels are an annotation on top of a transcript
/// that is perfectly readable without them, so a corrupted sidecar degrades
/// to "unlabelled" rather than making the transcript unopenable.
fn read_speaker_labels(meeting_dir: &Path) -> SpeakerLabels {
    let path = meeting_dir.join(SPEAKERS_FILE_NAME);
    let Ok(metadata) = std::fs::metadata(&path) else {
        return SpeakerLabels {
            assignments: HashMap::new(),
        };
    };
    if !metadata.is_file() || metadata.len() > MAX_SPEAKERS_BYTES {
        return SpeakerLabels {
            assignments: HashMap::new(),
        };
    }
    let Ok(body) = std::fs::read_to_string(&path) else {
        return SpeakerLabels {
            assignments: HashMap::new(),
        };
    };
    match serde_json::from_str::<SpeakersFile>(&body) {
        Ok(parsed) => SpeakerLabels {
            assignments: parsed.assignments,
        },
        Err(_) => SpeakerLabels {
            assignments: HashMap::new(),
        },
    }
}

/// Writes `speakers.json` atomically: a temp file in the same directory,
/// then a rename, so a reader never observes a half-written map and a
/// failure mid-write leaves the previous labels intact. Mirrors F2's own
/// `transcript.write_atomic`.
fn write_speaker_labels(meeting_dir: &Path, labels: &SpeakerLabels) -> Result<(), AppError> {
    let file = SpeakersFile {
        schema_version: SPEAKERS_SCHEMA_VERSION,
        assignments: labels.assignments.clone(),
    };
    let body = serde_json::to_string_pretty(&file)
        .map_err(|err| AppError::internal(format!("could not serialize speaker labels: {err}")))?;

    let target = meeting_dir.join(SPEAKERS_FILE_NAME);
    let temp = meeting_dir.join(format!(".{SPEAKERS_FILE_NAME}.tmp"));
    std::fs::write(&temp, body.as_bytes())
        .map_err(|err| AppError::io(format!("could not write {}: {err}", temp.display())))?;
    if let Err(err) = std::fs::rename(&temp, &target) {
        // Leave nothing behind on the failure path.
        let _ = std::fs::remove_file(&temp);
        return Err(AppError::io(format!(
            "could not replace {}: {err}",
            target.display()
        )));
    }
    Ok(())
}

/// `set_speaker_labels` — replaces a meeting's speaker labels wholesale.
///
/// Wholesale rather than per-segment, because renaming a speaker in the UI
/// is by definition an edit to every segment that speaker holds ("renames
/// every segment"), and a sequence of per-segment calls would make that one
/// operation observably non-atomic.
pub async fn set_speaker_labels_handler(
    state: &AppState,
    entry_id: &str,
    assignments: HashMap<String, String>,
) -> Result<(), AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let labels = SpeakerLabels { assignments };

    tokio::task::spawn_blocking(move || write_speaker_labels(&meeting_dir, &labels))
        .await
        .map_err(|join_err| {
            AppError::internal(format!("set_speaker_labels task panicked: {join_err}"))
        })?
}

/// A meeting's `summary.md`, if anything has written one.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SummaryView {
    pub entry_id: String,
    /// The absolute path the summary would live at, present or not — shown
    /// so the operator knows exactly where to put one.
    pub path: String,
    /// `None` when no summary exists yet.
    pub markdown: Option<String>,
}

/// An upper bound on `summary.md`. A summary of a meeting is prose; a file
/// larger than this is not one.
const MAX_SUMMARY_BYTES: u64 = 4 * 1024 * 1024;

/// `read_summary` — returns `summary.md` from the meeting folder.
///
/// Nothing in this app *writes* a summary: generating one needs a language
/// model, which is a feature in its own right and does not exist yet.
/// `summary.md` has been a reserved name in the vault contract from the
/// start (`vault::SUMMARY_FILE_NAME`), so reading it costs nothing and
/// means a summary written by hand — or by whatever eventually generates
/// them — is readable in the app the day it appears, rather than after
/// another round of UI work.
pub async fn read_summary_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<SummaryView, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let path = meeting_dir.join(vault::SUMMARY_FILE_NAME);
    let entry_id = entry_id.to_string();

    tokio::task::spawn_blocking(move || {
        let display_path = path.to_string_lossy().into_owned();
        let markdown = match std::fs::metadata(&path) {
            Ok(metadata) if metadata.is_file() && metadata.len() <= MAX_SUMMARY_BYTES => {
                std::fs::read_to_string(&path).ok()
            }
            _ => None,
        };
        Ok(SummaryView {
            entry_id,
            path: display_path,
            markdown,
        })
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_summary task panicked: {join_err}")))?
}

/// A meeting's `note.md`, if the operator has written one.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NoteView {
    pub entry_id: String,
    /// The absolute path the note lives at, present or not — shown so the
    /// operator knows exactly where their note is filed.
    pub path: String,
    /// `None` when no note exists yet.
    pub markdown: Option<String>,
}

/// An upper bound on `note.md`, matching [`MAX_SUMMARY_BYTES`]: a meeting
/// note is prose; a file larger than this is not one.
const MAX_NOTE_BYTES: u64 = 4 * 1024 * 1024;

/// `read_note` — returns `note.md` from the meeting folder.
pub async fn read_note_handler(state: &AppState, entry_id: &str) -> Result<NoteView, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let path = meeting_dir.join(vault::NOTE_FILE_NAME);
    let entry_id = entry_id.to_string();

    tokio::task::spawn_blocking(move || {
        let display_path = path.to_string_lossy().into_owned();
        let markdown = match std::fs::metadata(&path) {
            Ok(metadata) if metadata.is_file() && metadata.len() <= MAX_NOTE_BYTES => {
                std::fs::read_to_string(&path).ok()
            }
            _ => None,
        };
        Ok(NoteView {
            entry_id,
            path: display_path,
            markdown,
        })
    })
    .await
    .map_err(|join_err| AppError::internal(format!("read_note task panicked: {join_err}")))?
}

/// Writes `note.md` atomically: a temp file in the same directory, then a
/// rename, so a reader never observes a half-written note and a failure
/// mid-write leaves the previous note intact. Same discipline as
/// [`write_speaker_labels`].
fn write_note(meeting_dir: &Path, markdown: &str) -> Result<(), AppError> {
    let target = meeting_dir.join(vault::NOTE_FILE_NAME);
    let temp = meeting_dir.join(format!(".{}.tmp", vault::NOTE_FILE_NAME));
    std::fs::write(&temp, markdown.as_bytes())
        .map_err(|err| AppError::io(format!("could not write {}: {err}", temp.display())))?;
    if let Err(err) = std::fs::rename(&temp, &target) {
        // Leave nothing behind on the failure path.
        let _ = std::fs::remove_file(&temp);
        return Err(AppError::io(format!(
            "could not replace {}: {err}",
            target.display()
        )));
    }
    Ok(())
}

/// `write_note` — replaces a meeting's `note.md` wholesale.
///
/// Wholesale because the editor holds the full draft; an empty string
/// writes an empty file rather than deleting it — no delete-on-empty
/// magic, the operator can remove the file in the explorer if they want it
/// gone.
pub async fn write_note_handler(
    state: &AppState,
    entry_id: &str,
    markdown: String,
) -> Result<(), AppError> {
    if markdown.len() as u64 > MAX_NOTE_BYTES {
        return Err(AppError::invalid_argument(
            "the note is too large to save (4 MB limit)".to_string(),
        ));
    }
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;

    tokio::task::spawn_blocking(move || write_note(&meeting_dir, &markdown))
        .await
        .map_err(|join_err| {
            AppError::internal(format!("write_note task panicked: {join_err}"))
        })??;

    // The note just changed; let the search index catch up. Fire-and-forget
    // like the post-job re-index in `jobs.rs`: a service that does not know
    // the job type (or is down) answers an error this deliberately drops.
    let service = state.service.read().await.clone();
    tokio::spawn(async move {
        let _ = service.submit_index().await;
    });
    Ok(())
}

/// `list_project_speaker_names` — the distinct speaker names the operator
/// has assigned across all of this meeting's project siblings.
///
/// This is the project-level speaker memory: name "Speaker 1 = Даниил" in
/// one meeting, and the next meeting in the project offers "Даниил" as a
/// suggestion instead of asking for the name again. Only `speakers.json`
/// assignments count — they are operator-confirmed names, unlike raw
/// diarization labels. Best-effort like the vault listing: an unreadable
/// sibling contributes nothing rather than failing the call.
pub async fn list_project_speaker_names_handler(
    state: &AppState,
    entry_id: &str,
) -> Result<Vec<String>, AppError> {
    let (_root, meeting_dir) = resolve_entry(state, entry_id).await?;

    tokio::task::spawn_blocking(move || {
        let Some(project_dir) = meeting_dir.parent().map(Path::to_path_buf) else {
            return Ok(Vec::new());
        };
        let mut names: Vec<String> = Vec::new();
        if let Ok(entries) = std::fs::read_dir(&project_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if !path.is_dir() {
                    continue;
                }
                for name in read_speaker_labels(&path).assignments.into_values() {
                    let trimmed = name.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    if !names.iter().any(|known| known == trimmed) {
                        names.push(trimmed.to_string());
                    }
                }
            }
        }
        names.sort_by_key(|name| name.to_lowercase());
        Ok(names)
    })
    .await
    .map_err(|join_err| {
        AppError::internal(format!(
            "list_project_speaker_names task panicked: {join_err}"
        ))
    })?
}

/// `transcribe_vault_entry` — runs transcription over a recording that is
/// already filed in the vault.
///
/// The gap this closes: a recording filed while the service was down (FR-13
/// keeps the file safe and reports the transcription as failed) had no way
/// back into the pipeline short of dropping the file again, which would
/// file a *second* copy under a suffixed name. This re-submits the one
/// already there.
///
/// Refuses a meeting with no `source.*` rather than submitting a path F2
/// would fail to open: an empty meeting folder is a real state (an operator
/// can delete a recording and keep its transcript), and the honest answer
/// is that there is nothing to transcribe.
pub async fn transcribe_vault_entry_handler(
    state: &AppState,
    entry_id: &str,
    language: Option<String>,
) -> Result<JobSnapshot, AppError> {
    // IPC arguments are untrusted, so the language is checked here rather
    // than left for F2 to reject: the pinned contract is exactly
    // `"ru" | "en" | null`, and anything else fails before a job exists.
    let language = validate_language(language)?;

    let (root, meeting_dir) = resolve_entry(state, entry_id).await?;
    let meeting_name = meeting_name_of(&meeting_dir);

    let source = {
        let meeting_dir = meeting_dir.clone();
        tokio::task::spawn_blocking(move || source_file_in(&meeting_dir))
            .await
            .map_err(|join_err| {
                AppError::internal(format!("transcribe_vault_entry task panicked: {join_err}"))
            })?
    }
    .ok_or_else(|| {
        AppError::invalid_argument(format!("\"{meeting_name}\" has no recording to transcribe"))
    })?;

    // The same project/unsorted classification `list_vault` reports, so the
    // job row reads identically to one that arrived by drop.
    let classification = view_for(entry_id, &root, &meeting_dir)
        .project
        .map(|_| "sorted".to_string())
        .or_else(|| Some("unsorted".to_string()));

    Ok(state
        .registry
        .read()
        .await
        .enqueue_filed(meeting_dir, source, classification, language)
        .await)
}

/// The languages a transcribe job may be pinned to. `None` (the UI's Auto
/// default) is not a language: it hands the choice to F2's constrained
/// detection, which is why it is deliberately absent from this list rather
/// than spelled as an empty string.
const SUPPORTED_LANGUAGES: [&str; 2] = ["ru", "en"];

/// Gate on the pinned IPC contract `{ language: "ru" | "en" | null }`.
/// Rejects every other value -- including `""` and differently-cased
/// spellings -- so a malformed argument can never reach the wire.
fn validate_language(language: Option<String>) -> Result<Option<String>, AppError> {
    match language {
        None => Ok(None),
        Some(value) if SUPPORTED_LANGUAGES.contains(&value.as_str()) => Ok(Some(value)),
        Some(value) => Err(AppError::invalid_argument(format!(
            "unsupported transcription language {value:?} (expected one of {} or none)",
            SUPPORTED_LANGUAGES.join(", ")
        ))),
    }
}

/// The `source.<ext>` file inside a meeting folder, if there is one — the
/// same existence rule `vault::list` uses to decide `has_source`, resolved
/// to the actual path this time rather than a boolean.
fn source_file_in(meeting_dir: &Path) -> Option<PathBuf> {
    let children = std::fs::read_dir(meeting_dir).ok()?;
    children.flatten().find_map(|entry| {
        if !entry.file_type().ok()?.is_file() {
            return None;
        }
        let name = entry.file_name();
        let stem = name.to_str()?.split('.').next()?;
        if stem.eq_ignore_ascii_case(vault::SOURCE_STEM) {
            Some(entry.path())
        } else {
            None
        }
    })
}

/// `cancel_job` — asks the service to stop a job this app submitted.
///
/// Returns whether there was anything to cancel. A job that has not reached
/// the service yet, or has already finished, answers `false` rather than
/// failing: the operator clicked Cancel on something already past
/// cancelling, and that is information, not an error.
pub async fn cancel_job_handler(state: &AppState, job_id: &str) -> Result<bool, AppError> {
    state
        .registry
        .read()
        .await
        .cancel(job_id)
        .await
        .map_err(|err| match err {
            ServiceError::Unavailable { .. } => AppError::service_unavailable(err.to_string()),
            other => AppError::service(other.to_string()),
        })
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::Duration;

    use tempfile::tempdir;

    use crate::commands::{list_vault_handler, Revealer, ServiceStatusSink, ServiceStatusView};
    use crate::config::Settings;
    use crate::error::ErrorKind;
    use crate::service::fake::FakeService;
    use crate::service::TranscriptionService;

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
    fn diarized_speaker_labels_seed_the_speaker_map() {
        let body = r#"{"text": "hi", "segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "text": " hi", "speaker": "Speaker 1"},
            {"id": 1, "start": 1.0, "end": 2.0, "text": " there", "speaker": "Speaker 2"},
            {"id": 2, "start": 2.0, "end": 3.0, "text": " hm"}
        ]}"#;

        let view = parse_transcript("e", "m", body).expect("should parse");

        assert_eq!(
            view.speakers.get("0").map(String::as_str),
            Some("Speaker 1")
        );
        assert_eq!(
            view.speakers.get("1").map(String::as_str),
            Some("Speaker 2")
        );
        assert!(
            !view.speakers.contains_key("2"),
            "a segment diarization could not attribute stays unassigned"
        );
    }

    #[test]
    fn a_transcript_without_diarization_seeds_no_labels() {
        let view = parse_transcript("e", "m", BODY).expect("should parse");
        assert!(view.speakers.is_empty());
    }

    #[test]
    fn a_blank_diarized_label_is_ignored() {
        let body = r#"{"segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "text": " hi", "speaker": "  "}
        ]}"#;

        let view = parse_transcript("e", "m", body).expect("should parse");

        assert!(view.speakers.is_empty());
    }

    #[test]
    fn manual_labels_overlay_and_outrank_diarized_ones() {
        // Mirrors the handler's merge: the parser seeds the diarized labels,
        // then `speakers.json` assignments are inserted over them.
        let body = r#"{"segments": [
            {"id": 0, "start": 0.0, "end": 1.0, "text": " hi", "speaker": "Speaker 1"},
            {"id": 1, "start": 1.0, "end": 2.0, "text": " there", "speaker": "Speaker 2"}
        ]}"#;
        let dir = tempfile::tempdir().expect("tempdir");
        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([("0".to_string(), "Maxim".to_string())]),
            },
        )
        .expect("write labels");

        let mut view = parse_transcript("e", "m", body).expect("should parse");
        for (segment_id, name) in read_speaker_labels(dir.path()).assignments {
            view.speakers.insert(segment_id, name);
        }

        assert_eq!(view.speakers.get("0").map(String::as_str), Some("Maxim"));
        assert_eq!(
            view.speakers.get("1").map(String::as_str),
            Some("Speaker 2")
        );
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

    // -- speaker labels ----------------------------------------------------

    #[test]
    fn a_meeting_with_no_speakers_file_reads_as_unlabelled() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(read_speaker_labels(dir.path()).assignments.is_empty());
    }

    #[test]
    fn labels_round_trip_through_the_sidecar() {
        let dir = tempfile::tempdir().expect("tempdir");
        let labels = SpeakerLabels {
            assignments: HashMap::from([
                ("0".to_string(), "Maxim".to_string()),
                ("1".to_string(), "Speaker 2".to_string()),
            ]),
        };

        write_speaker_labels(dir.path(), &labels).expect("write labels");

        assert_eq!(read_speaker_labels(dir.path()), labels);
    }

    #[test]
    fn labels_are_written_beside_the_transcript_not_into_it() {
        // The whole reason for a sidecar: F2 rewrites transcript.json whole
        // on every re-transcription, and hand-applied labels have to survive
        // that.
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("transcript.json"), br#"{"segments":[]}"#)
            .expect("write transcript");

        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([("0".to_string(), "Maxim".to_string())]),
            },
        )
        .expect("write labels");

        assert_eq!(
            std::fs::read_to_string(dir.path().join("transcript.json")).expect("read transcript"),
            r#"{"segments":[]}"#,
            "transcript.json must be untouched"
        );
        assert!(dir.path().join("speakers.json").is_file());
    }

    #[test]
    fn a_malformed_speakers_file_degrades_to_unlabelled_rather_than_failing() {
        // Labels annotate a transcript that reads perfectly well without
        // them, so a corrupt sidecar must never make the transcript
        // unopenable.
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("speakers.json"), b"{not json").expect("write junk");

        assert!(read_speaker_labels(dir.path()).assignments.is_empty());
    }

    #[test]
    fn writing_labels_leaves_no_temp_file_behind() {
        let dir = tempfile::tempdir().expect("tempdir");

        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([("0".to_string(), "Maxim".to_string())]),
            },
        )
        .expect("write labels");

        let names: Vec<String> = std::fs::read_dir(dir.path())
            .expect("read dir")
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, vec!["speakers.json".to_string()]);
    }

    #[test]
    fn rewriting_labels_replaces_them_wholesale() {
        // Renaming a speaker is an edit to every segment that speaker holds,
        // so the write is a replacement, not a merge -- an assignment the
        // operator cleared must actually disappear.
        let dir = tempfile::tempdir().expect("tempdir");
        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([
                    ("0".to_string(), "Maxim".to_string()),
                    ("1".to_string(), "Speaker 2".to_string()),
                ]),
            },
        )
        .expect("first write");

        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([("0".to_string(), "Maxim".to_string())]),
            },
        )
        .expect("second write");

        let read_back = read_speaker_labels(dir.path());
        assert_eq!(read_back.assignments.len(), 1);
        assert_eq!(
            read_back.assignments.get("0").map(String::as_str),
            Some("Maxim")
        );
    }

    // -- summary -----------------------------------------------------------

    #[test]
    fn a_transcript_carries_its_own_path_and_labels() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("transcript.json"), BODY).expect("write transcript");
        write_speaker_labels(
            dir.path(),
            &SpeakerLabels {
                assignments: HashMap::from([("0".to_string(), "Maxim".to_string())]),
            },
        )
        .expect("write labels");

        let labels = read_speaker_labels(dir.path());

        assert_eq!(
            labels.assignments.get("0").map(String::as_str),
            Some("Maxim")
        );
    }

    // -- transcribe_vault_entry language (FR-5) ----------------------------

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

    struct NoopRevealer;
    impl Revealer for NoopRevealer {
        fn reveal(&self, _path: &Path) -> Result<(), AppError> {
            Ok(())
        }
    }

    struct NoopSidecar;
    #[async_trait::async_trait]
    impl crate::commands::SidecarController for NoopSidecar {
        async fn spawn_and_await_ready(
            &self,
            _config: &crate::sidecar::SidecarSpawnConfig,
            _timeout: Duration,
        ) -> Result<crate::sidecar::ReadyLine, crate::sidecar::SidecarError> {
            Err(crate::sidecar::SidecarError::Io {
                message: "no sidecar in tests".to_string(),
            })
        }
        async fn terminate(&self) {}
    }

    fn state_with_root(root: PathBuf, service: Arc<dyn TranscriptionService>) -> AppState {
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
            Arc::new(NoopRevealer),
        )
    }

    fn make_meeting(root: &Path) {
        let dir = root.join("ELS").join("260812 - Security issue");
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(dir.join("source.mp4"), b"bytes").expect("write source");
    }

    async fn only_entry_id(state: &AppState) -> String {
        let entries = list_vault_handler(state).await.expect("list vault");
        assert_eq!(entries.len(), 1, "expected exactly one vault entry");
        entries[0].id.clone()
    }

    #[test]
    fn transcribe_rejects_a_language_outside_ru_en_and_enqueues_nothing() {
        // IPC arguments are untrusted: anything but the pinned
        // `"ru" | "en" | null` contract is refused *before* a job exists,
        // rather than being forwarded for F2 to reject.
        run(async {
            for bogus in ["de", "", "EN", "ru "] {
                let root = tempdir().expect("tempdir");
                make_meeting(root.path());
                let state =
                    state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
                let id = only_entry_id(&state).await;

                let err = match transcribe_vault_entry_handler(&state, &id, Some(bogus.to_string()))
                    .await
                {
                    Ok(_) => panic!("language {bogus:?} must be refused"),
                    Err(err) => err,
                };

                assert_eq!(err.kind(), ErrorKind::InvalidArgument, "language {bogus:?}");
                assert!(
                    state.registry.read().await.list().await.is_empty(),
                    "language {bogus:?} must not enqueue anything"
                );
            }
        });
    }

    #[test]
    fn transcribe_accepts_ru_en_and_auto_and_carries_the_choice_to_the_service() {
        run(async {
            for expected in [Some("ru"), Some("en"), None] {
                let root = tempdir().expect("tempdir");
                make_meeting(root.path());
                let service = Arc::new(FakeService::new());
                let state = state_with_root(root.path().to_path_buf(), service.clone());
                let id = only_entry_id(&state).await;

                transcribe_vault_entry_handler(
                    &state,
                    &id,
                    expected.map(std::string::ToString::to_string),
                )
                .await
                .unwrap_or_else(|err| panic!("language {expected:?} must be accepted: {err:?}"));

                let start = std::time::Instant::now();
                let submission = loop {
                    if let Some(first) = service.submissions().into_iter().next() {
                        break first;
                    }
                    if start.elapsed() > Duration::from_secs(5) {
                        panic!("language {expected:?} was never submitted");
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                };

                assert_eq!(submission.language.as_deref(), expected);
            }
        });
    }

    // -- update_vault_entry vs. in-flight jobs -----------------------------

    #[test]
    fn update_vault_entry_is_refused_while_a_job_is_running_for_the_meeting() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let id = only_entry_id(&state).await;

            // A re-transcription of this very meeting. The production poll
            // interval (1.5s) keeps it comfortably non-terminal while the
            // rename below arrives.
            transcribe_vault_entry_handler(&state, &id, None)
                .await
                .expect("transcribe enqueues");

            let err = update_vault_entry_handler(
                &state,
                &id,
                Some("ELS".to_string()),
                "260812",
                "Renamed while busy",
            )
            .await
            .expect_err("a rename during an active job must be refused");

            assert_eq!(err.kind(), ErrorKind::InvalidArgument);
            assert!(
                err.message().contains("still running"),
                "the refusal must say why: {}",
                err.message()
            );
            // Nothing moved: the original folder is untouched.
            assert!(root
                .path()
                .join("ELS")
                .join("260812 - Security issue")
                .join("source.mp4")
                .is_file());
        });
    }

    // -- note.md -----------------------------------------------------------

    #[test]
    fn a_note_round_trips_and_leaves_no_temp_file_behind() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let id = only_entry_id(&state).await;

            write_note_handler(&state, &id, "# Заметка\n\nDetails here.".to_string())
                .await
                .expect("write note");

            let view = read_note_handler(&state, &id).await.expect("read note");
            assert_eq!(view.markdown.as_deref(), Some("# Заметка\n\nDetails here."));
            assert!(view.path.ends_with("note.md"), "path was {}", view.path);

            let meeting_dir = root.path().join("ELS").join("260812 - Security issue");
            assert!(
                !meeting_dir.join(".note.md.tmp").exists(),
                "the temp file must be gone after a successful write"
            );
        });
    }

    #[test]
    fn reading_a_missing_note_answers_none_but_still_names_the_path() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let id = only_entry_id(&state).await;

            let view = read_note_handler(&state, &id).await.expect("read note");

            assert_eq!(view.markdown, None);
            assert!(view.path.ends_with("note.md"), "path was {}", view.path);
        });
    }

    #[test]
    fn an_empty_note_writes_an_empty_file_rather_than_deleting() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let id = only_entry_id(&state).await;

            write_note_handler(&state, &id, "draft".to_string())
                .await
                .expect("write note");
            write_note_handler(&state, &id, String::new())
                .await
                .expect("write empty note");

            let view = read_note_handler(&state, &id).await.expect("read note");
            assert_eq!(view.markdown.as_deref(), Some(""));
        });
    }

    // -- search_vault hit mapping -------------------------------------------

    /// Answers canned hits from `search()`; the three required trait
    /// methods answer `Unavailable`, which this test never calls.
    struct CannedSearchService {
        hits: Vec<crate::service::SearchHit>,
    }

    #[async_trait::async_trait]
    impl TranscriptionService for CannedSearchService {
        async fn health(&self) -> Result<crate::service::ServiceHealth, ServiceError> {
            Err(ServiceError::Unavailable {
                detail: "canned".to_string(),
            })
        }
        async fn submit(
            &self,
            _req: crate::service::SubmitRequest,
        ) -> Result<String, ServiceError> {
            Err(ServiceError::Unavailable {
                detail: "canned".to_string(),
            })
        }
        async fn status(&self, _job_id: &str) -> Result<crate::service::JobStatus, ServiceError> {
            Err(ServiceError::Unavailable {
                detail: "canned".to_string(),
            })
        }
        async fn search(
            &self,
            _query: crate::service::SearchQuery,
        ) -> Result<Vec<crate::service::SearchHit>, ServiceError> {
            Ok(self.hits.clone())
        }
    }

    fn hit(meeting_dir: &str, title: &str) -> crate::service::SearchHit {
        crate::service::SearchHit {
            kind: "transcript".to_string(),
            project: "ELS".to_string(),
            meeting_dir: meeting_dir.to_string(),
            meeting_title: title.to_string(),
            snippet: "…the matching line…".to_string(),
            score: 0.5,
            start_sec: Some(12.0),
            timestamp: Some("0:12".to_string()),
        }
    }

    #[test]
    fn search_hits_map_to_entry_ids_and_unresolvable_hits_are_dropped() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let service = Arc::new(CannedSearchService {
                hits: vec![
                    hit("ELS/260812 - Security issue", "Security issue"),
                    // A meeting the vault listing has never seen: fail closed.
                    hit("ELS/999999 - Ghost", "Ghost"),
                ],
            });
            let state = state_with_root(root.path().to_path_buf(), service);
            let id = only_entry_id(&state).await; // seeds vault_index

            let results =
                crate::commands::search::search_vault_handler(&state, "security".to_string(), None)
                    .await
                    .expect("search");

            assert_eq!(results.len(), 1, "the ghost hit must be dropped");
            assert_eq!(results[0].entry_id, id);
            assert_eq!(results[0].meeting_name, "260812 - Security issue");
            assert_eq!(results[0].project.as_deref(), Some("ELS"));
            assert_eq!(results[0].timestamp.as_deref(), Some("0:12"));
        });
    }

    #[test]
    fn a_blank_search_query_answers_empty_without_touching_the_service() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));

            let results =
                crate::commands::search::search_vault_handler(&state, "   ".to_string(), None)
                    .await
                    .expect("search");

            assert!(results.is_empty());
        });
    }

    // -- project-level speaker memory ---------------------------------------

    #[test]
    fn project_speaker_names_gather_across_sibling_meetings_deduped_and_sorted() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            // Two sibling meetings in the same project, with overlapping and
            // differently-trimmed names; one unreadable speakers.json that
            // must degrade to nothing.
            let sibling_a = root.path().join("ELS").join("260813 - Follow-up");
            let sibling_b = root.path().join("ELS").join("260814 - Retro");
            std::fs::create_dir_all(&sibling_a).expect("mkdir");
            std::fs::create_dir_all(&sibling_b).expect("mkdir");
            write_speaker_labels(
                &sibling_a,
                &SpeakerLabels {
                    assignments: HashMap::from([
                        ("0".to_string(), "Даниил".to_string()),
                        ("1".to_string(), " maxim ".to_string()),
                    ]),
                },
            )
            .expect("labels a");
            write_speaker_labels(
                &sibling_b,
                &SpeakerLabels {
                    assignments: HashMap::from([
                        ("0".to_string(), "Даниил".to_string()),
                        ("1".to_string(), "Anna".to_string()),
                        ("2".to_string(), "   ".to_string()),
                    ]),
                },
            )
            .expect("labels b");
            std::fs::write(
                root.path()
                    .join("ELS")
                    .join("260812 - Security issue")
                    .join("speakers.json"),
                "{not json",
            )
            .expect("write junk");

            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let entries = list_vault_handler(&state).await.expect("list vault");
            let id = entries
                .iter()
                .find(|entry| entry.meeting_name == "260812 - Security issue")
                .expect("the seeded meeting is listed")
                .id
                .clone();

            let names = list_project_speaker_names_handler(&state, &id)
                .await
                .expect("list names");

            // Deduped, trimmed, blank dropped, sorted case-insensitively;
            // the malformed sibling contributed nothing and failed nothing.
            assert_eq!(names, vec!["Anna", "maxim", "Даниил"]);
        });
    }

    #[test]
    fn an_oversized_note_is_refused_before_touching_the_vault() {
        run(async {
            let root = tempdir().expect("tempdir");
            make_meeting(root.path());
            let state = state_with_root(root.path().to_path_buf(), Arc::new(FakeService::new()));
            let id = only_entry_id(&state).await;

            let oversized = "x".repeat(usize::try_from(MAX_NOTE_BYTES).expect("cap fits") + 1);
            let err = write_note_handler(&state, &id, oversized)
                .await
                .expect_err("an oversized note must be refused");

            assert_eq!(err.kind(), ErrorKind::InvalidArgument);
            let meeting_dir = root.path().join("ELS").join("260812 - Security issue");
            assert!(!meeting_dir.join("note.md").exists(), "nothing was written");
        });
    }
}
