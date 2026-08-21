//! Ingest orchestration, rollback, and the public API surface (FR-13).
//!
//! Owned by T10. Drives the full drop-to-disk sequence documented in the
//! plan's architecture overview: stat the source, classify the filename,
//! verify containment and length of the *joint* destination before any
//! directory is created, resolve project-folder reuse and destination
//! collisions, create exactly the directories this call needs, transfer the
//! file into place, and unwind exactly what this call created if any step
//! after directory creation fails.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use chrono::NaiveDate;

use crate::date;
use crate::error::{IoFailure, Rejection, VaultError};
use crate::layout::{self, Placement};
use crate::parse::{self, Classified};
use crate::paths;
use crate::transfer;

/// A vault rooted at a specific, already-initialized directory on disk
/// (FR-1).
///
/// Construct with [`Vault::open`]; every subsequent [`Vault::ingest`] or
/// [`Vault::ingest_on`] call treats its `source` argument as hostile input —
/// a file-dialog or drag-drop result is not validation — and verifies
/// containment before writing anything (FR-14).
#[derive(Debug)]
pub struct Vault {
    root: PathBuf,
}

impl Vault {
    /// Opens a vault rooted at `root`, creating `root` and `root/unsorted/`
    /// if they do not already exist (FR-1). Idempotent across repeated
    /// calls with the same root.
    pub fn open(root: impl AsRef<Path>) -> Result<Vault, VaultError> {
        let root = root.as_ref();
        layout::init(root)?;
        let canonical_root = root.canonicalize().map_err(|e| io_err(root, &e))?;
        // `canonicalize` returns the Windows extended-length `\\?\` form;
        // strip it here, once, so every path this crate ever hands back to
        // a caller (this field, and everything built by joining onto it)
        // is a plain `C:\...` path usable by F2 and presentable in F3's UI
        // (FR-13). See `paths::simplify_extended_prefix` for why.
        let root = paths::simplify_extended_prefix(canonical_root);
        Ok(Vault { root })
    }

    /// The vault's canonicalized (and, on Windows, extended-length-prefix
    /// stripped) root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Ingests `source`, using today's local date as the "date added" prefix
    /// if the filename turns out to be unsorted (FR-10).
    pub fn ingest(&self, source: impl AsRef<Path>) -> Result<Ingested, VaultError> {
        self.ingest_on(source, date::today_local())
    }

    /// Ingests `source`, injecting `today` as the "date added" prefix for an
    /// unsorted classification instead of reading the system clock.
    ///
    /// This is the clock seam [`Vault::ingest`] calls with
    /// [`crate::date::today_local`]: without it, two unsorted files ingested
    /// on the same real day could never be distinguished in a test (FR-10).
    pub fn ingest_on(
        &self,
        source: impl AsRef<Path>,
        today: NaiveDate,
    ) -> Result<Ingested, VaultError> {
        let source_path = source.as_ref();
        stat_source_for_ingest(source_path)?;

        let file_name = source_path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or(VaultError::SourceMissing)?;

        let classification = parse::classify_filename(file_name)?;

        let mut rollback = Rollback::new();
        match self.place_and_transfer(source_path, classification, today, &mut rollback) {
            Ok(ingested) => Ok(ingested),
            Err(e) => {
                rollback.unwind();
                Err(e)
            }
        }
    }

    /// Builds the destination, resolves collisions, creates exactly the
    /// directories this call needs (remembered in `rollback`), and
    /// transfers `source_path` into place.
    fn place_and_transfer(
        &self,
        source_path: &Path,
        classification: Classified,
        today: NaiveDate,
        rollback: &mut Rollback,
    ) -> Result<Ingested, VaultError> {
        let Destination {
            parent_component,
            base_name,
            ext,
            classification_out,
            project_code,
        } = compute_destination(classification, today);

        // FR-14/NFR-4: verify containment and length of the full, *joint*
        // two-component destination before any directory is created —
        // including before a fresh project folder might be created for a
        // code whose title turns out unusable (over-long or escaping).
        //
        // `contained_child` returns a canonicalized path, which on Windows
        // carries the extended-length `\\?\` prefix; the length must be
        // checked against the plain path this call will actually create
        // (FR-13), not the longer canonical form, or a destination that
        // fits under the 260-character limit in its real, plain form could
        // be rejected as too long.
        let checked = paths::contained_child(&self.root, &[&parent_component, &base_name])?;
        let checked = paths::simplify_extended_prefix(checked);
        let source_file_name = format!("source.{ext}");
        paths::check_len(&checked.join(&source_file_name))?;

        let parent_dir = match &project_code {
            Some(code) => self.ensure_project_dir(code, rollback)?,
            None => self.root.join(paths::UNSORTED_DIR_NAME),
        };

        let placement = layout::resolve_meeting_dir(&parent_dir, &base_name, source_path)?;

        let (meeting_dir, collision) = match placement {
            Placement::Fresh => {
                let dir = parent_dir.join(&base_name);
                fs::create_dir_all(&dir).map_err(|e| io_err(&dir, &e))?;
                rollback.remember_dir(dir.clone());
                (dir, CollisionOutcome::Fresh)
            }
            Placement::Suffixed { dir, n } => {
                let full = parent_dir.join(&dir);
                fs::create_dir_all(&full).map_err(|e| io_err(&full, &e))?;
                rollback.remember_dir(full.clone());
                (full, CollisionOutcome::SuffixedFolder(n))
            }
            Placement::DuplicateRedrop { dir } => {
                // No-op re-drop: the existing recording is byte-identical
                // and stays exactly as it is; nothing is transferred and
                // nothing is created (FR-11).
                let full = parent_dir.join(&dir);
                let existing_source = full.join(&source_file_name);
                return Ok(Ingested {
                    classification: classification_out,
                    meeting_dir: full,
                    source_path: existing_source,
                    collision: CollisionOutcome::DuplicateRedrop,
                });
            }
        };

        let dest = meeting_dir.join(&source_file_name);
        transfer::transfer_into_place(source_path, &dest)?;

        Ok(Ingested {
            classification: classification_out,
            meeting_dir,
            source_path: dest,
            collision,
        })
    }

    /// Resolves the project directory for `code`, remembering it in
    /// `rollback` only if this call is the one that created it (FR-9): a
    /// project folder that already existed beforehand must survive a later
    /// failure untouched.
    fn ensure_project_dir(
        &self,
        code: &str,
        rollback: &mut Rollback,
    ) -> Result<PathBuf, VaultError> {
        let existed_before = self.project_dir_exists(code)?;
        // `layout::ensure_project_dir` resolves through
        // `paths::contained_child`, which returns a canonicalized (and, on
        // Windows, `\\?\`-prefixed) path. Strip that prefix here, at the
        // point the project directory re-enters the ingest pipeline, so
        // every path built by joining the meeting-folder name onto it stays
        // plain (FR-13, see `paths::simplify_extended_prefix`).
        let dir = paths::simplify_extended_prefix(layout::ensure_project_dir(&self.root, code)?);
        if !existed_before {
            rollback.remember_dir(dir.clone());
        }
        Ok(dir)
    }

    /// Whether a project directory matching `code` (case-insensitively)
    /// already exists directly under the vault root.
    fn project_dir_exists(&self, code: &str) -> Result<bool, VaultError> {
        for entry in fs::read_dir(&self.root).map_err(|e| io_err(&self.root, &e))? {
            let entry = entry.map_err(|e| io_err(&self.root, &e))?;
            if entry
                .file_name()
                .to_str()
                .is_some_and(|name| name.eq_ignore_ascii_case(code))
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

/// The result of one completed [`Vault::ingest`] / [`Vault::ingest_on`] call
/// (FR-13): everything the caller needs without re-deriving anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ingested {
    /// Whether the recording matched the naming convention, and the parsed
    /// components or rejection reason.
    pub classification: Classification,
    /// The absolute path to the meeting folder the recording (and any
    /// later artifacts, such as `transcript.json`) live in. Exists at
    /// return time.
    pub meeting_dir: PathBuf,
    /// The absolute path to the recording inside `meeting_dir`
    /// (`source.<ext>`).
    pub source_path: PathBuf,
    /// How a destination-name collision, if any, was resolved (FR-11).
    pub collision: CollisionOutcome,
}

/// Whether an ingested recording matched the naming convention (FR-2/FR-3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Classification {
    /// The filename fully matched `<Project code> - <date> - <Title>.<ext>`.
    Sorted {
        /// The normalized (uppercase) project code.
        project: String,
        /// The verbatim six-character `YYMMDD` date.
        date: String,
        /// The validated, trimmed title.
        title: String,
    },
    /// The filename failed some rule of the convention and was routed to
    /// `unsorted/` instead (FR-10).
    Unsorted {
        /// The first rule the filename failed.
        reason: Rejection,
    },
}

/// How a destination-name collision was resolved (FR-11).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollisionOutcome {
    /// No folder of this name existed yet.
    Fresh,
    /// The incoming file was byte-identical (size and mtime) to the
    /// `source.*` already at this destination; the ingest was a no-op and
    /// nothing was transferred or removed.
    DuplicateRedrop,
    /// A different recording already occupied the base name; this
    /// numerically suffixed folder was created instead.
    SuffixedFolder(u32),
}

/// The pieces [`Vault::place_and_transfer`] needs, computed once up front
/// from a [`Classified`] result and (for an unsorted recording) today's
/// date.
struct Destination {
    /// The single path component directly under the vault root: the
    /// project code, or the reserved `unsorted` name.
    parent_component: String,
    /// The meeting folder's own name: `<date> - <title>` or `<date of
    /// ingest> - <sanitized stem>`.
    base_name: String,
    /// The normalized (lowercase) media extension.
    ext: String,
    /// The [`Classification`] to report back to the caller.
    classification_out: Classification,
    /// `Some(project code)` for a sorted recording, `None` for unsorted —
    /// distinguishes whether [`Vault::ensure_project_dir`] runs at all.
    project_code: Option<String>,
}

/// Computes a [`Destination`] from a filename classification, without
/// touching the filesystem.
fn compute_destination(classification: Classified, today: NaiveDate) -> Destination {
    match classification {
        Classified::Sorted(parsed) => Destination {
            parent_component: parsed.project.clone(),
            base_name: paths::meeting_folder_name(&parsed.date, &parsed.title),
            ext: parsed.ext,
            classification_out: Classification::Sorted {
                project: parsed.project.clone(),
                date: parsed.date,
                title: parsed.title,
            },
            project_code: Some(parsed.project),
        },
        Classified::Unsorted { reason, stem, ext } => Destination {
            parent_component: paths::UNSORTED_DIR_NAME.to_string(),
            base_name: paths::unsorted_folder_name(today, &stem),
            ext,
            classification_out: Classification::Unsorted { reason },
            project_code: None,
        },
    }
}

/// Checks that `source_path` exists and is a regular file, before anything
/// about its name is even considered (architecture step 1) — a missing or
/// wrong-type source is rejected without touching the vault at all.
fn stat_source_for_ingest(source_path: &Path) -> Result<(), VaultError> {
    match fs::metadata(source_path) {
        Ok(meta) if meta.is_file() => Ok(()),
        Ok(_) => Err(VaultError::SourceNotAFile),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(VaultError::SourceMissing),
        Err(e) => Err(io_err(source_path, &e)),
    }
}

/// Wraps a `std::io::Error` with the path that produced it.
fn io_err(path: &Path, err: &io::Error) -> VaultError {
    VaultError::Io {
        path: path.to_path_buf(),
        source: IoFailure::from(err),
    }
}

/// Tracks exactly which directories one [`Vault::ingest_on`] call created,
/// so a failure after directory creation unwinds exactly those and no more
/// (FR-12) — never a directory that already existed beforehand.
///
/// Every directory remembered here was created exclusively by this call, so
/// on unwind it is safe to remove recursively: nothing else could have
/// written into it in the meantime (single-threaded, synchronous call).
struct Rollback {
    created_dirs: Vec<PathBuf>,
}

impl Rollback {
    fn new() -> Self {
        Rollback {
            created_dirs: Vec::new(),
        }
    }

    fn remember_dir(&mut self, dir: PathBuf) {
        self.created_dirs.push(dir);
    }

    /// Removes every remembered directory, most-recently-created first, so
    /// an emptied project folder is only removed after the meeting folder
    /// nested inside it is already gone. Best-effort: a failure to remove
    /// one directory does not stop the others from being attempted.
    fn unwind(&self) {
        for dir in self.created_dirs.iter().rev() {
            let _ = fs::remove_dir_all(dir);
        }
    }
}
