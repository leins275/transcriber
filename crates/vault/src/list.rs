//! Read-only vault listing.
//!
//! **Scope note:** `specs/meeting-vault-layout/spec.md`'s "Out of scope"
//! section (line 136) explicitly excluded "browsing, listing, searching,
//! renaming or re-filing existing vault content" from that feature's MVP.
//! The operator has since extended that scope for the vault-browser feature
//! (a persistent list of already-ingested meetings on startup) — this module
//! is that extension. It adds nothing to the write path: every function here
//! only reads directory entries and checks file existence, never creates,
//! writes, renames or deletes anything.
//!
//! [`list_meetings`] scans exactly two levels below `root` —
//! `<root>/<PROJECT>/<meeting>` and `<root>/unsorted/<meeting>` — and goes no
//! further: it does not recurse into a meeting folder's own contents beyond
//! checking whether `source.*`/`transcript.json` exist there, and it never
//! reads a file's bytes. A junk entry at either level (a stray file where a
//! directory was expected, an unreadable directory, a name that is not valid
//! Unicode, a root-level directory whose name is not the reserved `unsorted`
//! word and does not match the project-code pattern) is skipped rather than
//! aborting the whole call — there is no `Result` here at all, on purpose:
//! a listing is inherently best-effort against a directory tree that is
//! never fully under this crate's control (an operator can always drop a
//! stray file or folder into the vault by hand).

use std::fs::{self, DirEntry};
use std::path::{Path, PathBuf};

use chrono::NaiveDate;

use crate::code;
use crate::date;
use crate::paths::{SOURCE_STEM, TRANSCRIPT_FILE_NAME, UNSORTED_DIR_NAME};

/// One meeting folder found while listing the vault.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MeetingEntry {
    /// The project code this meeting sits under, normalized to uppercase
    /// (the folder on disk may be spelled in any case), or `None` for a
    /// meeting under `unsorted/` — the sorted/unsorted classification this
    /// listing carries. See [`MeetingEntry::is_sorted`] for the same thing as a
    /// predicate.
    pub project: Option<String>,
    /// The meeting folder's own name, exactly as it is on disk (`<date> -
    /// <title>` for a sorted meeting, `<date of ingest> - <stem>` for an
    /// unsorted one — see `crate::paths::meeting_folder_name` /
    /// `unsorted_folder_name`).
    pub meeting_name: String,
    /// The absolute path to the meeting folder.
    pub meeting_dir: PathBuf,
    /// Whether a `source.*` recording exists directly inside `meeting_dir`
    /// (an existence check only — the file's contents are never read).
    pub has_source: bool,
    /// Whether `transcript.json` exists directly inside `meeting_dir` (an
    /// existence check only).
    pub has_transcript: bool,
}

impl MeetingEntry {
    /// Whether this meeting sits under a real project rather than
    /// `unsorted/` — the same classification as `self.project.is_some()`,
    /// spelled as a predicate for a caller that wants a boolean rather than
    /// matching on `project` itself.
    pub fn is_sorted(&self) -> bool {
        self.project.is_some()
    }
}

/// Lists every meeting folder currently in the vault rooted at `root`.
///
/// Read-only: never creates, writes, renames or deletes anything, and never
/// recurses beyond the two levels the module docs describe. Ordered newest
/// date first, by parsing the leading `YYMMDD` of the meeting folder's own
/// name (the same six-character convention `crate::date` validates
/// elsewhere in this crate) as a real calendar date; an entry whose name
/// does not start with a parseable date sorts after every dated entry,
/// among themselves in name order — see [`sort_key`] for exactly why dated
/// entries are considered more informative than a non-conforming name.
///
/// Returns an empty `Vec` (never an error) if `root` does not exist or is
/// not a readable directory — a caller that has not yet configured or
/// created a meetings root sees an empty vault, not a crash.
pub fn list_meetings(root: &Path) -> Vec<MeetingEntry> {
    let mut entries = Vec::new();

    let Ok(top_level) = fs::read_dir(root) else {
        return entries;
    };

    for top_entry in top_level.flatten() {
        let Some((name, is_dir)) = dir_name(&top_entry) else {
            continue;
        };
        if !is_dir {
            continue;
        }

        if name.eq_ignore_ascii_case(UNSORTED_DIR_NAME) {
            collect_meetings(&top_entry.path(), None, &mut entries);
        } else if let Ok(project) = code::validate(&name) {
            // A junk root-level directory (not `unsorted/`, not a valid
            // project code — e.g. a stray folder an operator created by
            // hand) is silently skipped rather than treated as a project.
            //
            // The *normalized* code is reported, not the folder's own
            // spelling: project-folder reuse is case-insensitive
            // (`layout::ensure_project_dir`), so a vault can legitimately
            // hold `els/` while every filename says `ELS`. Listing the
            // uppercase code keeps one project from appearing as two.
            collect_meetings(
                &top_entry.path(),
                Some(project.as_str().to_string()),
                &mut entries,
            );
        }
    }

    entries.sort_by(
        |a, b| match (sort_key(&a.meeting_name), sort_key(&b.meeting_name)) {
            (Some(da), Some(db)) => db.cmp(&da),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => a.meeting_name.cmp(&b.meeting_name),
        },
    );

    entries
}

/// Reads one project-or-`unsorted` folder's direct children, appending a
/// [`MeetingEntry`] for every subdirectory found (FR-scope note above: no
/// recursion past this level).
fn collect_meetings(parent: &Path, project: Option<String>, out: &mut Vec<MeetingEntry>) {
    let Ok(children) = fs::read_dir(parent) else {
        return;
    };

    for child in children.flatten() {
        let Some((meeting_name, is_dir)) = dir_name(&child) else {
            continue;
        };
        if !is_dir {
            continue;
        }

        let meeting_dir = child.path();
        out.push(MeetingEntry {
            project: project.clone(),
            has_transcript: meeting_dir.join(TRANSCRIPT_FILE_NAME).is_file(),
            has_source: has_source_file(&meeting_dir),
            meeting_name,
            meeting_dir,
        });
    }
}

/// A `DirEntry`'s file name (as valid UTF-8) and whether it is a directory,
/// or `None` for anything this listing cannot safely classify (a name that
/// is not valid Unicode, or a `file_type()` call that itself failed —
/// tolerated here rather than propagated, per the module docs).
fn dir_name(entry: &DirEntry) -> Option<(String, bool)> {
    let name = entry.file_name().to_str()?.to_owned();
    let is_dir = entry.file_type().ok()?.is_dir();
    Some((name, is_dir))
}

/// Whether `meeting_dir` directly contains a file named `source.<anything>`
/// (case-insensitively on the stem) — an existence check over the
/// directory's own entries, never a read of any file's contents.
fn has_source_file(meeting_dir: &Path) -> bool {
    let Ok(children) = fs::read_dir(meeting_dir) else {
        return false;
    };
    children.flatten().any(|entry| {
        let Ok(file_type) = entry.file_type() else {
            return false;
        };
        if !file_type.is_file() {
            return false;
        }
        entry
            .file_name()
            .to_str()
            .and_then(|name| name.split('.').next())
            .is_some_and(|stem| stem.eq_ignore_ascii_case(SOURCE_STEM))
    })
}

/// Parses the leading six characters of `meeting_name` as a `YYMMDD` date
/// (the same convention `crate::date::validate` enforces at ingest time),
/// for sort purposes only. `None` for anything that does not parse — a
/// truncated name, non-digit characters, or six digits that do not denote a
/// real calendar date (e.g. hand-renamed junk) — which [`list_meetings`]
/// sorts after every dated entry rather than erroring the whole call.
fn sort_key(meeting_name: &str) -> Option<NaiveDate> {
    let prefix = meeting_name.get(0..6)?;
    date::validate(prefix).ok().map(|valid| valid.date())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn make_meeting(dir: &Path, with_source: bool, with_transcript: bool) {
        fs::create_dir_all(dir).expect("create meeting dir");
        if with_source {
            fs::write(dir.join("source.mp4"), b"recording bytes").expect("write source");
        }
        if with_transcript {
            fs::write(dir.join("transcript.json"), b"{}").expect("write transcript");
        }
    }

    #[test]
    fn empty_or_missing_root_yields_an_empty_list_not_an_error() {
        let dir = tempdir().expect("tempdir");
        let missing = dir.path().join("does-not-exist");

        assert!(list_meetings(&missing).is_empty());
        assert!(list_meetings(dir.path()).is_empty());
    }

    #[test]
    fn lists_a_sorted_meeting_with_project_and_flags() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("ELS").join("260812 - Security issue"),
            true,
            true,
        );

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        let entry = &entries[0];
        assert_eq!(entry.project.as_deref(), Some("ELS"));
        assert!(entry.is_sorted());
        assert_eq!(entry.meeting_name, "260812 - Security issue");
        assert!(entry
            .meeting_dir
            .ends_with("ELS/260812 - Security issue".replace('/', std::path::MAIN_SEPARATOR_STR)));
        assert!(entry.has_source);
        assert!(entry.has_transcript);
    }

    #[test]
    fn lists_an_unsorted_meeting_with_no_project() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("unsorted").join("260812 - random meeting"),
            true,
            false,
        );

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].project, None);
        assert!(!entries[0].is_sorted());
        assert!(entries[0].has_source);
        assert!(!entries[0].has_transcript);
    }

    #[test]
    fn missing_transcript_is_reported_false_not_an_error() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("ELS").join("260812 - No transcript yet"),
            true,
            false,
        );

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert!(!entries[0].has_transcript);
    }

    #[test]
    fn newest_date_sorts_first_across_projects() {
        let dir = tempdir().expect("tempdir");
        make_meeting(&dir.path().join("ELS").join("260101 - Oldest"), true, true);
        make_meeting(&dir.path().join("ABC").join("260812 - Newest"), true, true);
        make_meeting(
            &dir.path().join("unsorted").join("260601 - Middle"),
            true,
            true,
        );

        let entries = list_meetings(dir.path());

        assert_eq!(
            entries
                .iter()
                .map(|e| e.meeting_name.as_str())
                .collect::<Vec<_>>(),
            vec!["260812 - Newest", "260601 - Middle", "260101 - Oldest"]
        );
    }

    #[test]
    fn a_meeting_folder_whose_name_has_no_parseable_date_sorts_after_dated_entries() {
        let dir = tempdir().expect("tempdir");
        make_meeting(&dir.path().join("ELS").join("260101 - Dated"), true, true);
        make_meeting(
            &dir.path().join("ELS").join("not a date at all"),
            true,
            true,
        );

        let entries = list_meetings(dir.path());

        assert_eq!(
            entries
                .iter()
                .map(|e| e.meeting_name.as_str())
                .collect::<Vec<_>>(),
            vec!["260101 - Dated", "not a date at all"]
        );
    }

    #[test]
    fn a_junk_root_level_directory_that_is_not_a_valid_project_code_is_skipped() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("ELS").join("260101 - Real project"),
            true,
            true,
        );
        // Not `unsorted`, and does not match `^[A-Z][A-Z0-9]{1,9}$`.
        fs::create_dir_all(dir.path().join("not-a-project-code")).expect("create junk root dir");
        fs::write(
            dir.path()
                .join("not-a-project-code")
                .join("260101 - Junk meeting"),
            b"whatever",
        )
        .ok();

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].project.as_deref(), Some("ELS"));
    }

    #[test]
    fn a_stray_file_at_the_meeting_level_is_skipped_not_treated_as_a_meeting() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("ELS").join("260101 - Real meeting"),
            true,
            true,
        );
        fs::write(dir.path().join("ELS").join("stray-file.txt"), b"junk").expect("write junk file");

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].meeting_name, "260101 - Real meeting");
    }

    #[test]
    fn a_stray_file_at_the_root_level_is_skipped() {
        let dir = tempdir().expect("tempdir");
        make_meeting(
            &dir.path().join("ELS").join("260101 - Real meeting"),
            true,
            true,
        );
        fs::write(dir.path().join("root-level-junk.txt"), b"junk").expect("write junk file");

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
    }

    #[test]
    fn has_source_recognizes_any_supported_extension_case_insensitively() {
        let dir = tempdir().expect("tempdir");
        let meeting_dir = dir.path().join("ELS").join("260101 - Odd extension case");
        fs::create_dir_all(&meeting_dir).expect("create meeting dir");
        fs::write(meeting_dir.join("SOURCE.WAV"), b"recording bytes").expect("write source");

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert!(entries[0].has_source);
    }

    #[test]
    fn a_meeting_with_no_source_and_no_transcript_is_still_listed() {
        let dir = tempdir().expect("tempdir");
        fs::create_dir_all(dir.path().join("ELS").join("260101 - Empty shell"))
            .expect("create meeting dir");

        let entries = list_meetings(dir.path());

        assert_eq!(entries.len(), 1);
        assert!(!entries[0].has_source);
        assert!(!entries[0].has_transcript);
    }

    #[test]
    fn does_not_recurse_beyond_the_meeting_folder() {
        let dir = tempdir().expect("tempdir");
        let meeting_dir = dir.path().join("ELS").join("260101 - Has a subfolder");
        fs::create_dir_all(meeting_dir.join("nested")).expect("create nested dir");
        fs::write(meeting_dir.join("nested").join("source.mp4"), b"deep")
            .expect("write nested file");

        let entries = list_meetings(dir.path());

        // Exactly one meeting entry -- the nested subfolder is never itself
        // listed as a meeting, and its `source.mp4` (one level too deep)
        // must not satisfy `has_source` for the parent meeting.
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].has_source);
    }
}
