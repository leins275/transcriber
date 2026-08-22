//! Post-ingest meeting management — rename, re-file, delete.
//!
//! **Scope note:** `specs/meeting-vault-layout/spec.md`'s "Out of scope"
//! section excluded "renaming or re-filing existing vault content" from that
//! feature's MVP, the same way it excluded listing (see [`crate::list`]).
//! The operator has since extended the scope again: a recording that landed
//! in `unsorted/` because its filename did not follow the convention must be
//! fixable from inside the app, without the operator hunting through
//! Explorer for the folder. This module is that extension, and it is the
//! only place in this crate that moves or removes content that is *already*
//! in the vault.
//!
//! ## What is protected
//!
//! Every entry point starts at [`resolve_meeting`], which refuses anything
//! that is not an existing directory sitting exactly two levels below the
//! vault root, under either `unsorted/` or a folder whose name is a valid
//! project code. That single gate is what stops a caller from renaming (or
//! trashing) a project folder, the `unsorted/` folder, the vault root, or
//! any path outside the vault at all — including via a symlink, since the
//! comparison is made on canonicalized paths.
//!
//! ## Rename rejects where ingest re-files
//!
//! At ingest a filename that fails the convention is never an error: the
//! recording is routed to `unsorted/` so that every media file lands
//! somewhere (FR-10). A rename is different — there is an operator on the
//! other end of it who asked for a specific name — so a bad project code,
//! date or title comes back as [`VaultError::InvalidMeetingName`] carrying
//! the offending [`crate::error::Rejection`], instead of quietly filing their meeting
//! somewhere they did not choose.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::code;
use crate::date;
use crate::error::{IoFailure, VaultError};
use crate::layout;
use crate::paths::{self, UNSORTED_DIR_NAME};
use crate::title;

/// The highest numeric collision suffix probed before giving up, mirroring
/// `layout`'s own limit (FR-11).
const MAX_SUFFIX: u32 = 999;

/// A requested new identity for an existing meeting folder.
///
/// All three fields are always supplied — a rename is expressed as the
/// complete target name rather than a patch — so a caller that is only
/// changing the project still passes the meeting's current date and title
/// back. That keeps this type free of "unchanged" sentinels and makes the
/// resulting folder name a pure function of the request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MeetingUpdate {
    /// The target project code, or `None` to file the meeting under
    /// `unsorted/`. Validated and uppercased by [`crate::code::validate`],
    /// so `els` and `ELS` both name the same project.
    pub project: Option<String>,
    /// The target `YYMMDD` date, validated against a real calendar date and
    /// then used verbatim in the folder name (FR-5).
    pub date: String,
    /// The target title, held to the same rules a sorted filename's title
    /// is (FR-6): never repaired, only accepted or reported.
    pub title: String,
}

/// A meeting folder that has passed [`resolve_meeting`]'s checks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedMeeting {
    /// The canonicalized vault root (extended-length prefix stripped).
    pub root: PathBuf,
    /// The canonicalized meeting folder (extended-length prefix stripped).
    pub meeting_dir: PathBuf,
    /// The meeting's current project code, normalized to uppercase, or
    /// `None` when it currently sits under `unsorted/`.
    pub project: Option<String>,
    /// The meeting folder's own current name.
    pub meeting_name: String,
}

/// Validates that `meeting_dir` really is a meeting folder inside the vault
/// at `root`, and returns both paths canonicalized.
///
/// This is the single security gate for this module (see the module docs).
/// A path that does not exist, is not a directory, is not exactly two levels
/// below the root, or whose parent is neither `unsorted/` nor a valid
/// project code, is [`VaultError::NotAMeetingDirectory`] — never a partial
/// success the caller might act on.
pub fn resolve_meeting(root: &Path, meeting_dir: &Path) -> Result<ResolvedMeeting, VaultError> {
    let canonical_root =
        paths::simplify_extended_prefix(root.canonicalize().map_err(|e| io_err(root, &e))?);
    let canonical_meeting = paths::simplify_extended_prefix(
        meeting_dir
            .canonicalize()
            .map_err(|_| VaultError::NotAMeetingDirectory)?,
    );

    if !canonical_meeting.is_dir() {
        return Err(VaultError::NotAMeetingDirectory);
    }

    let parent = canonical_meeting
        .parent()
        .ok_or(VaultError::NotAMeetingDirectory)?;
    if parent.parent() != Some(canonical_root.as_path()) {
        return Err(VaultError::NotAMeetingDirectory);
    }

    let parent_name = parent
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(VaultError::NotAMeetingDirectory)?;
    let project = if parent_name.eq_ignore_ascii_case(UNSORTED_DIR_NAME) {
        None
    } else {
        Some(
            code::validate(parent_name)
                .map_err(|_| VaultError::NotAMeetingDirectory)?
                .as_str()
                .to_string(),
        )
    };

    let meeting_name = canonical_meeting
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(VaultError::NotAMeetingDirectory)?
        .to_string();

    Ok(ResolvedMeeting {
        root: canonical_root,
        meeting_dir: canonical_meeting,
        project,
        meeting_name,
    })
}

/// Renames and/or re-files an existing meeting folder, returning its new
/// path.
///
/// The whole move is one [`fs::rename`] of the folder itself, so the
/// meeting's contents (`source.*`, `transcript.json`, anything else the
/// operator put beside them) travel together and no partial state is
/// observable: either the folder is at its old path or at its new one.
///
/// * A request that resolves to the folder's current path is a no-op that
///   returns that path — renaming a meeting to the name it already has is
///   not an error, and in particular does not go through the collision
///   suffixing below.
/// * A destination name that is already taken is suffixed `(2)`, `(3)`, …
///   exactly as ingest does (FR-11) rather than overwriting or refusing —
///   two genuinely different meetings may honestly share a date and title.
/// * The 260-character cap is checked against the longest path this move
///   would produce, including the meeting's deepest existing child, before
///   anything is moved (NFR-4).
///
/// Moving the last meeting out of a project folder leaves that folder empty;
/// it is removed on a best-effort basis so a project the operator has
/// emptied stops appearing in listings. `unsorted/` is never removed — it is
/// part of the vault's fixed shape (FR-1) — and a failure to remove an empty
/// project folder is deliberately ignored, since the rename it follows has
/// already succeeded.
pub fn rename_meeting(
    root: &Path,
    meeting_dir: &Path,
    update: &MeetingUpdate,
) -> Result<PathBuf, VaultError> {
    let resolved = resolve_meeting(root, meeting_dir)?;

    let project = match update.project.as_deref() {
        Some(raw) => Some(
            code::validate(raw)
                .map_err(|reason| VaultError::InvalidMeetingName { reason })?
                .as_str()
                .to_string(),
        ),
        None => None,
    };
    let valid_date =
        date::validate(&update.date).map_err(|reason| VaultError::InvalidMeetingName { reason })?;
    let valid_title = title::validate(&update.title)
        .map_err(|reason| VaultError::InvalidMeetingName { reason })?;

    let parent = match project.as_deref() {
        Some(code) => layout::ensure_project_dir(&resolved.root, code)?,
        None => {
            let unsorted = paths::contained_child(&resolved.root, &[UNSORTED_DIR_NAME])?;
            fs::create_dir_all(&unsorted).map_err(|e| io_err(&unsorted, &e))?;
            paths::simplify_extended_prefix(unsorted)
        }
    };
    let parent = paths::simplify_extended_prefix(parent);

    let base_name = paths::meeting_folder_name(valid_date.as_str(), &valid_title);
    let target = free_destination(&parent, &base_name, &resolved.meeting_dir)?;
    if target == resolved.meeting_dir {
        return Ok(target);
    }

    check_move_length(&resolved.meeting_dir, &target)?;

    fs::rename(&resolved.meeting_dir, &target).map_err(|e| io_err(&resolved.meeting_dir, &e))?;

    prune_empty_project_dir(&resolved.root, &resolved.meeting_dir);

    Ok(target)
}

/// Moves an existing meeting folder, with everything inside it, to the OS
/// recycle bin.
///
/// Recoverable by design: deleting a meeting from the app must not be the
/// one action in this crate the operator cannot undo, so this never calls
/// [`fs::remove_dir_all`]. If the platform has no recycle bin available (or
/// refuses the item — a network share, a removable volume with no bin), the
/// folder is left exactly where it was and the reason comes back as
/// [`VaultError::TrashUnavailable`].
///
/// Like [`rename_meeting`], a project folder left empty by the deletion is
/// removed on a best-effort basis; `unsorted/` never is.
pub fn delete_meeting(root: &Path, meeting_dir: &Path) -> Result<(), VaultError> {
    let resolved = resolve_meeting(root, meeting_dir)?;

    trash::delete(&resolved.meeting_dir).map_err(|e| VaultError::TrashUnavailable {
        message: e.to_string(),
    })?;

    prune_empty_project_dir(&resolved.root, &resolved.meeting_dir);

    Ok(())
}

/// Picks the first free path for `base_name` under `parent`, probing
/// `<base>`, `<base> (2)`, `<base> (3)`, … up to [`MAX_SUFFIX`].
///
/// `current` — the folder being moved — counts as free at every candidate:
/// renaming a meeting to the name it already has (or changing only its
/// letter case on a case-insensitive filesystem) must land back on itself
/// rather than being suffixed away from its own name.
fn free_destination(parent: &Path, base_name: &str, current: &Path) -> Result<PathBuf, VaultError> {
    let base = parent.join(base_name);
    if is_free(&base, current) {
        return Ok(base);
    }

    for n in 2..=MAX_SUFFIX {
        let candidate = parent.join(paths::suffixed(base_name, n));
        if is_free(&candidate, current) {
            return Ok(candidate);
        }
    }

    Err(VaultError::SuffixLimitExceeded)
}

/// Whether `candidate` can be moved onto: either nothing is there, or what
/// is there is the very folder being moved.
fn is_free(candidate: &Path, current: &Path) -> bool {
    if !candidate.exists() {
        return true;
    }
    match candidate.canonicalize() {
        Ok(canonical) => paths::simplify_extended_prefix(canonical) == current,
        Err(_) => false,
    }
}

/// Checks the 260-character cap against the *deepest* path this move would
/// produce (NFR-4), not just the folder itself: a meeting folder that fits
/// under the cap can still hold a `transcript.json` whose full path does
/// not, and discovering that only when the next write fails would leave the
/// operator with a meeting they cannot transcribe again.
fn check_move_length(source_dir: &Path, target_dir: &Path) -> Result<(), VaultError> {
    paths::check_len(target_dir)?;

    let Ok(children) = fs::read_dir(source_dir) else {
        return Ok(());
    };
    for child in children.flatten() {
        paths::check_len(&target_dir.join(child.file_name()))?;
    }
    Ok(())
}

/// Removes the project folder a meeting has just left, if that folder is now
/// empty. Best-effort by design (see [`rename_meeting`]'s docs): every
/// failure path here is silent, because the move it follows has already
/// succeeded and must not be reported as a failure.
fn prune_empty_project_dir(root: &Path, vacated_meeting_dir: &Path) {
    let Some(parent) = vacated_meeting_dir.parent() else {
        return;
    };
    if parent == root {
        return;
    }
    let is_unsorted = parent
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(UNSORTED_DIR_NAME));
    if is_unsorted {
        return;
    }
    // `remove_dir` (never `remove_dir_all`) — it fails, harmlessly, the
    // moment the folder still holds anything at all.
    let _ = fs::remove_dir(parent);
}

/// Wraps a `std::io::Error` with the path that produced it.
fn io_err(path: &Path, err: &io::Error) -> VaultError {
    VaultError::Io {
        path: path.to_path_buf(),
        source: IoFailure::from(err),
    }
}

/// Convenience re-export point for callers matching on why a rename was
/// refused; `Rejection` itself lives in [`crate::error`].
pub use crate::error::Rejection as MeetingNameRejection;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::Rejection;
    use tempfile::{tempdir, TempDir};

    fn vault_with_meeting(parent: &str, name: &str) -> (TempDir, PathBuf) {
        let dir = tempdir().expect("tempdir");
        let meeting = dir.path().join(parent).join(name);
        fs::create_dir_all(&meeting).expect("create meeting dir");
        fs::write(meeting.join("source.mp4"), b"recording bytes").expect("write source");
        (dir, meeting)
    }

    fn update(project: Option<&str>, date: &str, title: &str) -> MeetingUpdate {
        MeetingUpdate {
            project: project.map(str::to_owned),
            date: date.to_string(),
            title: title.to_string(),
        }
    }

    #[test]
    fn resolves_an_unsorted_meeting_with_no_project() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");

        let resolved = resolve_meeting(dir.path(), &meeting).expect("should resolve");

        assert_eq!(resolved.project, None);
        assert_eq!(resolved.meeting_name, "260822 - source");
    }

    #[test]
    fn resolves_a_lowercase_project_folder_to_an_uppercase_code() {
        let (dir, meeting) = vault_with_meeting("els", "260812 - Security issue");

        let resolved = resolve_meeting(dir.path(), &meeting).expect("should resolve");

        assert_eq!(resolved.project.as_deref(), Some("ELS"));
    }

    #[test]
    fn refuses_a_project_folder_the_vault_root_and_a_path_outside_it() {
        let (dir, _) = vault_with_meeting("ELS", "260812 - Security issue");
        let outside = tempdir().expect("tempdir");

        for candidate in [
            dir.path().join("ELS"),
            dir.path().to_path_buf(),
            dir.path().join("unsorted"),
            outside.path().to_path_buf(),
            dir.path().join("ELS").join("does-not-exist"),
        ] {
            assert_eq!(
                resolve_meeting(dir.path(), &candidate),
                Err(VaultError::NotAMeetingDirectory),
                "for {}",
                candidate.display()
            );
        }
    }

    #[test]
    fn refuses_a_file_and_a_folder_three_levels_deep() {
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");
        let nested = meeting.join("nested");
        fs::create_dir_all(&nested).expect("create nested dir");

        assert_eq!(
            resolve_meeting(dir.path(), &meeting.join("source.mp4")),
            Err(VaultError::NotAMeetingDirectory)
        );
        assert_eq!(
            resolve_meeting(dir.path(), &nested),
            Err(VaultError::NotAMeetingDirectory)
        );
    }

    #[test]
    fn files_an_unsorted_meeting_under_a_project_and_takes_its_contents_along() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260814", "Weekly sync"),
        )
        .expect("should rename");

        assert!(moved.ends_with("260814 - Weekly sync"));
        assert_eq!(moved.parent().and_then(|p| p.file_name()).unwrap(), "ELS");
        assert!(moved.join("source.mp4").is_file());
        assert!(!meeting.exists());
    }

    #[test]
    fn accepts_a_lowercase_project_code_and_capitalizes_the_folder() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("els"), "260814", "Weekly sync"),
        )
        .expect("should rename");

        assert_eq!(moved.parent().and_then(|p| p.file_name()).unwrap(), "ELS");
    }

    #[test]
    fn reuses_an_existing_project_folder_whatever_its_case() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");
        fs::create_dir_all(dir.path().join("els")).expect("pre-create lowercase project dir");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260814", "Weekly sync"),
        )
        .expect("should rename");

        assert_eq!(moved.parent().and_then(|p| p.file_name()).unwrap(), "els");
    }

    #[test]
    fn moves_a_sorted_meeting_back_to_unsorted() {
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(None, "260812", "Security issue"),
        )
        .expect("should rename");

        assert_eq!(
            moved.parent().and_then(|p| p.file_name()).unwrap(),
            "unsorted"
        );
        assert!(moved.join("source.mp4").is_file());
    }

    #[test]
    fn renaming_a_meeting_to_its_current_name_is_a_no_op() {
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260812", "Security issue"),
        )
        .expect("should rename");

        assert_eq!(
            moved,
            meeting
                .canonicalize()
                .map(paths::simplify_extended_prefix)
                .unwrap()
        );
        assert!(moved.join("source.mp4").is_file());
    }

    #[test]
    fn a_taken_destination_name_is_suffixed_rather_than_overwritten() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");
        let occupied = dir.path().join("ELS").join("260814 - Weekly sync");
        fs::create_dir_all(&occupied).expect("create occupying meeting");
        fs::write(occupied.join("source.mp4"), b"someone else's recording").expect("write");

        let moved = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260814", "Weekly sync"),
        )
        .expect("should rename");

        assert!(moved.ends_with("260814 - Weekly sync (2)"));
        assert_eq!(
            fs::read(occupied.join("source.mp4")).expect("occupier survives"),
            b"someone else's recording"
        );
    }

    #[test]
    fn an_invalid_project_date_or_title_is_reported_not_silently_re_filed() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");

        for (request, expected) in [
            (
                update(Some("1ELS"), "260814", "Weekly sync"),
                Rejection::InvalidProjectCode,
            ),
            (
                update(Some("unsorted"), "260814", "Weekly sync"),
                Rejection::ReservedProjectCode,
            ),
            (
                update(Some("ELS"), "260230", "Weekly sync"),
                Rejection::DateNotACalendarDate,
            ),
            (
                update(Some("ELS"), "26081", "Weekly sync"),
                Rejection::DateNotSixDigits,
            ),
            (
                update(Some("ELS"), "260814", "Q3: review"),
                Rejection::IllegalTitleCharacter(':'),
            ),
            (update(Some("ELS"), "260814", "   "), Rejection::EmptyTitle),
        ] {
            assert_eq!(
                rename_meeting(dir.path(), &meeting, &request),
                Err(VaultError::InvalidMeetingName { reason: expected }),
                "for {request:?}"
            );
        }

        // Nothing moved: every rejection happens before the rename.
        assert!(meeting.join("source.mp4").is_file());
    }

    #[test]
    fn a_title_that_would_escape_the_vault_is_rejected_before_anything_moves() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");

        let result = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260814", "..\\..\\evil"),
        );

        assert_eq!(
            result,
            Err(VaultError::InvalidMeetingName {
                reason: Rejection::IllegalTitleCharacter('\\')
            })
        );
        assert!(meeting.join("source.mp4").is_file());
        assert!(!dir.path().join("ELS").exists());
    }

    #[test]
    fn a_destination_over_the_length_cap_is_refused_and_moves_nothing() {
        let (dir, meeting) = vault_with_meeting("unsorted", "260822 - source");
        let long_title = "x".repeat(250);

        let result = rename_meeting(
            dir.path(),
            &meeting,
            &update(Some("ELS"), "260814", &long_title),
        );

        assert!(
            matches!(result, Err(VaultError::PathTooLong { .. })),
            "got {result:?}"
        );
        assert!(meeting.join("source.mp4").is_file());
    }

    #[test]
    fn emptying_a_project_folder_removes_it_but_never_unsorted() {
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");

        rename_meeting(
            dir.path(),
            &meeting,
            &update(None, "260812", "Security issue"),
        )
        .expect("should rename");

        assert!(!dir.path().join("ELS").exists(), "empty project is pruned");
        assert!(dir.path().join("unsorted").is_dir(), "unsorted survives");
    }

    #[test]
    fn a_project_folder_with_other_meetings_left_in_it_survives() {
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");
        fs::create_dir_all(dir.path().join("ELS").join("260901 - Another"))
            .expect("create sibling meeting");

        rename_meeting(
            dir.path(),
            &meeting,
            &update(None, "260812", "Security issue"),
        )
        .expect("should rename");

        assert!(dir.path().join("ELS").join("260901 - Another").is_dir());
    }

    #[test]
    fn delete_moves_the_meeting_out_of_the_vault_and_prunes_its_project() {
        // This really does hand a folder to the OS recycle bin -- the point
        // of `delete_meeting` is that it is recoverable, and a fake here
        // would verify nothing about that. The item left behind is a
        // tempdir-named meeting folder holding 15 bytes.
        let (dir, meeting) = vault_with_meeting("ELS", "260812 - Security issue");

        delete_meeting(dir.path(), &meeting).expect("should delete");

        assert!(!meeting.exists());
        assert!(!dir.path().join("ELS").exists(), "empty project is pruned");
    }

    #[test]
    fn delete_refuses_anything_that_is_not_a_meeting_folder() {
        let (dir, _) = vault_with_meeting("ELS", "260812 - Security issue");

        for candidate in [dir.path().to_path_buf(), dir.path().join("ELS")] {
            assert_eq!(
                delete_meeting(dir.path(), &candidate),
                Err(VaultError::NotAMeetingDirectory),
                "for {}",
                candidate.display()
            );
            assert!(candidate.exists(), "nothing was removed");
        }
    }
}
