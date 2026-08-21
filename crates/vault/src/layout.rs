//! Vault layout — init, project-folder reuse, collision policy (FR-1, FR-9,
//! FR-11).
//!
//! Owned by T9. Project-directory reuse is case-insensitive and never
//! renames what it finds; collision resolution never creates the meeting
//! directory itself, so rollback has a single owner in `ingest.rs`.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::error::{IoFailure, VaultError};
use crate::paths;
use crate::transfer;

/// The highest numeric collision suffix probed before giving up (FR-11).
const MAX_SUFFIX: u32 = 999;

/// The outcome of probing a meeting-folder name for a collision (FR-11).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Placement {
    /// No folder of this name exists yet; the caller may create it fresh.
    Fresh,
    /// A folder of the given name already holds a `source.*` that is
    /// byte-identical (size and mtime) to the incoming file. The ingest is
    /// a no-op re-drop of that folder — nothing here was written to.
    DuplicateRedrop {
        /// The name of the existing folder the incoming file matches.
        dir: String,
    },
    /// No exact duplicate was found under the base name or any suffix
    /// probed so far; the caller should create a folder with this
    /// numerically suffixed name.
    Suffixed {
        /// The suffixed folder name, e.g. `"<base> (2)"`.
        dir: String,
        /// The suffix number used.
        n: u32,
    },
}

/// Ensures `root` and `<root>/unsorted/` exist, creating them if absent
/// (FR-1). Idempotent: calling this repeatedly changes nothing beyond the
/// first successful call and never touches any other existing child of
/// `root`.
///
/// Returns [`VaultError::RootIsNotADirectory`] if `root` exists but is not
/// a directory, rather than panicking. If `root`'s parent does not exist,
/// the whole chain is created via [`fs::create_dir_all`].
pub fn init(root: &Path) -> Result<(), VaultError> {
    if root.exists() && !root.is_dir() {
        return Err(VaultError::RootIsNotADirectory);
    }
    fs::create_dir_all(root).map_err(|e| io_err(root, &e))?;

    let unsorted = root.join(paths::UNSORTED_DIR_NAME);
    if unsorted.exists() && !unsorted.is_dir() {
        return Err(io_err(&unsorted, &not_a_directory("unsorted")));
    }
    fs::create_dir_all(&unsorted).map_err(|e| io_err(&unsorted, &e))?;

    Ok(())
}

/// Resolves the project directory for `code` under `root`, reusing an
/// existing folder regardless of letter case (FR-9) rather than ever
/// renaming what it finds. If no matching folder exists, creates one with
/// `code`'s own (already-normalized) name.
///
/// Both the reuse and the create path are joined through
/// [`paths::contained_child`] so a hostile `code` cannot escape the vault
/// root (defense in depth alongside FR-14, enforced earlier in the
/// pipeline).
pub fn ensure_project_dir(root: &Path, code: &str) -> Result<PathBuf, VaultError> {
    for entry in fs::read_dir(root).map_err(|e| io_err(root, &e))? {
        let entry = entry.map_err(|e| io_err(root, &e))?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !name.eq_ignore_ascii_case(code) {
            continue;
        }

        let is_dir = entry
            .file_type()
            .map_err(|e| io_err(&entry.path(), &e))?
            .is_dir();
        if is_dir {
            return paths::contained_child(root, &[&name]);
        }
        return Err(io_err(&entry.path(), &not_a_directory(&name)));
    }

    let created = paths::contained_child(root, &[code])?;
    fs::create_dir(&created).map_err(|e| io_err(&created, &e))?;
    Ok(created)
}

/// Probes `parent/<base_name>`, then `parent/<base_name> (2)`,
/// `parent/<base_name> (3)`, … for a collision with `incoming` (FR-11).
///
/// Never creates the meeting directory itself — only reads existing
/// entries — so a single caller (`ingest.rs`) owns directory creation and
/// its rollback. Returns [`VaultError::SuffixLimitExceeded`] if every
/// suffix up to 999 is occupied by a different recording.
pub fn resolve_meeting_dir(
    parent: &Path,
    base_name: &str,
    incoming: &Path,
) -> Result<Placement, VaultError> {
    let base_dir = parent.join(base_name);
    match probe(&base_dir, incoming)? {
        Probe::Free => return Ok(Placement::Fresh),
        Probe::Duplicate => {
            return Ok(Placement::DuplicateRedrop {
                dir: base_name.to_string(),
            })
        }
        Probe::Occupied => {}
    }

    for n in 2..=MAX_SUFFIX {
        let name = paths::suffixed(base_name, n);
        let candidate = parent.join(&name);
        match probe(&candidate, incoming)? {
            Probe::Free => return Ok(Placement::Suffixed { dir: name, n }),
            Probe::Duplicate => return Ok(Placement::DuplicateRedrop { dir: name }),
            Probe::Occupied => continue,
        }
    }

    Err(VaultError::SuffixLimitExceeded)
}

/// The result of probing one candidate meeting-folder path.
enum Probe {
    /// Nothing exists at this path.
    Free,
    /// A folder exists here whose `source.*` is byte-identical to the
    /// incoming file.
    Duplicate,
    /// A folder (or some other entry) exists here that does not match the
    /// incoming file.
    Occupied,
}

/// Inspects one candidate meeting-folder path, comparing its `source.*`
/// (if any) against `incoming` by size and mtime (FR-11, via
/// [`transfer::same_recording`]).
fn probe(candidate_dir: &Path, incoming: &Path) -> Result<Probe, VaultError> {
    if !candidate_dir.exists() {
        return Ok(Probe::Free);
    }
    if !candidate_dir.is_dir() {
        return Ok(Probe::Occupied);
    }

    if let Some(existing_source) = existing_source_path(candidate_dir, incoming) {
        if existing_source.is_file() {
            return match transfer::same_recording(&existing_source, incoming) {
                Ok(true) => Ok(Probe::Duplicate),
                Ok(false) => Ok(Probe::Occupied),
                Err(e) => Err(io_err(&existing_source, &e)),
            };
        }
    }

    Ok(Probe::Occupied)
}

/// Builds the path a `source.*` file would have inside `candidate_dir`,
/// using `incoming`'s own extension — the same extension the eventual
/// `source.<ext>` file will carry.
fn existing_source_path(candidate_dir: &Path, incoming: &Path) -> Option<PathBuf> {
    let ext = incoming.extension()?.to_str()?;
    Some(candidate_dir.join(format!(
        "{}.{}",
        paths::SOURCE_STEM,
        ext.to_ascii_lowercase()
    )))
}

/// Wraps a `std::io::Error` with the path that produced it.
fn io_err(path: &Path, err: &io::Error) -> VaultError {
    VaultError::Io {
        path: path.to_path_buf(),
        source: IoFailure::from(err),
    }
}

/// Builds a typed I/O error for an entry that exists but is not a
/// directory where one was required.
fn not_a_directory(name: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::AlreadyExists,
        format!("an entry named \"{name}\" exists but is not a directory"),
    )
}
