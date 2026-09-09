//! The per-project speaker roster: `<root>/<PROJECT>/roster.json`.
//!
//! A roster is a *name list plus a mode*, nothing else: `open` keeps the
//! free-text speaker box the app has always had, `roster` turns the name
//! controls into a pick-list over the listed names. It carries no voice
//! data and no segment ids, so it is emphatically not the per-project
//! voice store the project rejected -- recognition stays the sibling scan
//! in the service.
//!
//! Deliberately a *file* at project level, beside the reserved `chats/`
//! directory: `vault::list_meetings`, the Python indexer and the MCP
//! server all consider only directories there, so the roster needs no
//! listing exclusion anywhere and cannot surface as a bogus meeting.
//!
//! House rules, same as `chats.rs`: `project` is untrusted IPC input
//! validated by the shared [`super::chats::project_dir`] (which never
//! creates a project), the read is capped and degrades to the open roster
//! rather than failing the call, and the write is a temp file plus a
//! rename so a torn write can never replace a good roster.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::AppError;

use super::AppState;

/// The on-disk schema version, written on every save.
const SCHEMA_VERSION: u32 = 1;

/// An upper bound on one roster file. A roster is at most a few hundred
/// short names; anything larger is not a roster this build wrote, so it is
/// read as the open roster rather than parsed.
const MAX_ROSTER_BYTES: u64 = 64 * 1024;

/// The longest a single roster name may be, in characters. A speaker name
/// is a person's name, not a paragraph.
const MAX_NAME_CHARS: usize = 200;

/// The most names one project's roster may hold.
const MAX_NAMES: usize = 500;

/// Which names the app's speaker controls may offer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RosterMode {
    /// Today's behaviour: any name may be typed, the project's existing
    /// names are offered as hints.
    Open,
    /// Only the roster's names may be picked.
    Roster,
}

/// The roster as the UI reads it back -- always normalized, so the editor
/// never shows a list the next save would silently rewrite.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectRosterView {
    pub mode: RosterMode,
    pub names: Vec<String>,
}

/// The roster as the UI sends it. Names arrive as the operator typed them
/// and are normalized here before anything is written.
#[derive(Debug, Clone, Deserialize)]
pub struct ProjectRosterInput {
    pub mode: RosterMode,
    pub names: Vec<String>,
}

/// The on-disk shape. Unknown fields are ignored on read, so a file a
/// newer build wrote still opens in an older one.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct RosterFile {
    #[serde(default)]
    schema_version: u32,
    mode: RosterMode,
    #[serde(default)]
    names: Vec<String>,
}

impl ProjectRosterView {
    /// The roster of a project that has never had one -- and the answer to
    /// every unreadable file (degradation over failure: a broken roster
    /// must never lock the operator out of naming a speaker).
    fn open() -> Self {
        Self {
            mode: RosterMode::Open,
            names: Vec::new(),
        }
    }
}

/// Trimmed, blanks dropped, case-insensitive duplicates collapsed onto the
/// first spelling, original order preserved.
///
/// Mirrored verbatim by `apps/desktop/src/lib/roster.ts`: both sides of
/// the IPC boundary must agree, or the editor shows one list and the
/// backend stores another.
fn normalize_names(names: &[String]) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    let mut normalized: Vec<String> = Vec::new();
    for name in names {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            continue;
        }
        let key = trimmed.to_lowercase();
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        normalized.push(trimmed.to_string());
    }
    normalized
}

/// Rejects a roster no operator could have meant: an over-long name or a
/// cast of hundreds. Runs before the write, so a refused save leaves the
/// stored roster exactly as it was.
fn validate_names(names: &[String]) -> Result<(), AppError> {
    if names.len() > MAX_NAMES {
        return Err(AppError::invalid_argument(format!(
            "a roster holds at most {MAX_NAMES} names, got {}",
            names.len()
        )));
    }
    for name in names {
        let length = name.trim().chars().count();
        if length > MAX_NAME_CHARS {
            return Err(AppError::invalid_argument(format!(
                "a roster name is at most {MAX_NAME_CHARS} characters, got {length}"
            )));
        }
    }
    Ok(())
}

fn roster_path(project_dir: &Path) -> PathBuf {
    project_dir.join(vault::ROSTER_FILE_NAME)
}

/// Reads the roster file, or `None` for every reason a file cannot be
/// trusted: absent, not a file, over the cap, unreadable, unparseable.
fn read_roster_file(path: &Path) -> Option<RosterFile> {
    let metadata = std::fs::metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_ROSTER_BYTES {
        return None;
    }
    let body = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&body).ok()
}

/// Temp file plus rename, inside the project directory that
/// [`super::chats::project_dir`] already proved exists -- nothing here
/// creates a directory, so a save can never bring a project into being.
fn write_roster_file(project_dir: &Path, file: &RosterFile) -> Result<(), AppError> {
    let target = roster_path(project_dir);
    let temp = project_dir.join(format!(".{}.tmp", vault::ROSTER_FILE_NAME));
    let body = serde_json::to_string_pretty(file)
        .map_err(|err| AppError::internal(format!("could not serialize the roster: {err}")))?;
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

/// `read_project_roster` -- the project's roster, or the open roster when
/// there is none to read.
pub async fn read_project_roster_handler(
    state: &AppState,
    project: &str,
) -> Result<ProjectRosterView, AppError> {
    let dir = super::chats::project_dir(state, project).await?;
    tokio::task::spawn_blocking(move || {
        let Some(file) = read_roster_file(&roster_path(&dir)) else {
            return ProjectRosterView::open();
        };
        ProjectRosterView {
            mode: file.mode,
            // A hand-edited file gets the same normalization a saved one
            // does, so the UI is never handed a list with duplicates.
            names: normalize_names(&file.names),
        }
    })
    .await
    .map_err(|join_err| {
        AppError::internal(format!("read_project_roster task panicked: {join_err}"))
    })
}

/// `save_project_roster` -- replaces the project's roster wholesale (the
/// editor holds the whole list, so a save is by definition wholesale) and
/// returns what was stored.
pub async fn save_project_roster_handler(
    state: &AppState,
    project: &str,
    roster: ProjectRosterInput,
) -> Result<ProjectRosterView, AppError> {
    let dir = super::chats::project_dir(state, project).await?;
    validate_names(&roster.names)?;
    let names = normalize_names(&roster.names);
    let mode = roster.mode;

    tokio::task::spawn_blocking(move || {
        write_roster_file(
            &dir,
            &RosterFile {
                schema_version: SCHEMA_VERSION,
                mode,
                names: names.clone(),
            },
        )?;
        Ok(ProjectRosterView { mode, names })
    })
    .await
    .map_err(|join_err| {
        AppError::internal(format!("save_project_roster task panicked: {join_err}"))
    })?
}

// -- the roster as a bound on speaker identification ------------------------

/// The number of people the project's roster says may speak in
/// `meeting_dir`, or `None` when the roster puts no bound on it.
///
/// `Some(n)` only for a meeting filed in a real project whose roster is in
/// [`RosterMode::Roster`] and lists at least one name, counted after the
/// same normalization the editor applies -- so the cap the service gets is
/// exactly the pick-list the operator sees. Everything else answers
/// `None`, which is the pre-roster behaviour: a meeting under `unsorted`
/// (never a project, even if someone plants a `roster.json` there), a
/// meeting that is not one level under the root, an `open` roster, a
/// strict roster with no names, and every unreadable or unparseable file
/// (degradation over failure -- a broken roster must not stop a job).
///
/// Blocking (it reads a file), and called by `jobs.rs` from
/// `spawn_blocking` at the moment a job is submitted, so a backfill queued
/// before a roster edit still carries the roster as it stands when its own
/// turn comes.
pub fn roster_speaker_cap(root: &Path, meeting_dir: &Path) -> Option<u32> {
    let canonical_root = crate::paths::canonicalize_existing(root).ok()?;
    // Containment, 8.3 short names and junctions are all this function's
    // problem exactly once: `ensure_inside` already resolves both sides the
    // way every other vault path in this crate is resolved.
    let canonical_meeting = crate::paths::ensure_inside(root, meeting_dir).ok()?;
    let relative = canonical_meeting.strip_prefix(&canonical_root).ok()?;

    // `<root>/<PROJECT>/<meeting>` and nothing else: two components, the
    // first of which is a plain directory name.
    let mut components = relative.components();
    let project = match components.next()? {
        std::path::Component::Normal(name) => name.to_owned(),
        _ => return None,
    };
    components.next()?;
    if components.next().is_some() {
        return None;
    }
    if project
        .to_string_lossy()
        .eq_ignore_ascii_case(vault::UNSORTED_DIR_NAME)
    {
        return None;
    }

    let file = read_roster_file(&roster_path(&canonical_root.join(&project)))?;
    if file.mode != RosterMode::Roster {
        return None;
    }
    let names = normalize_names(&file.names);
    u32::try_from(names.len()).ok().filter(|count| *count >= 1)
}

// -- `#[tauri::command]` wrappers -------------------------------------------

#[tauri::command]
pub async fn read_project_roster(
    state: tauri::State<'_, AppState>,
    project: String,
) -> Result<ProjectRosterView, AppError> {
    read_project_roster_handler(&state, &project).await
}

#[tauri::command]
pub async fn save_project_roster(
    state: tauri::State<'_, AppState>,
    project: String,
    roster: ProjectRosterInput,
) -> Result<ProjectRosterView, AppError> {
    save_project_roster_handler(&state, &project, roster).await
}
