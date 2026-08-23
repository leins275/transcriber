//! Tests for ingest orchestration, rollback, and the public API surface
//! (FR-8 through FR-15). Owned by T10.
//!
//! All cases use real directories via `tempfile` — no mocked filesystem.
//! The vault root and the "dropped file" staging area are always two
//! separate temp directories, so `list_files(vault.root())` only ever sees
//! what the vault itself created.

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs;
use std::io::Write;
use std::path::Path;
#[cfg(windows)]
use std::path::PathBuf;
#[cfg(windows)]
use std::process::Command;

use chrono::NaiveDate;
use tempfile::tempdir;
use vault::{Classification, CollisionOutcome, Rejection, Vault, VaultError};

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dir");
    }
    let mut f = fs::File::create(path).expect("create file");
    f.write_all(bytes).expect("write file");
}

/// Every *file* (not directory) under `root`, as a `root`-relative,
/// forward-slash path, so assertions about "nothing else exists" are exact
/// and platform-independent.
fn list_files(root: &Path) -> BTreeSet<String> {
    fn walk(dir: &Path, root: &Path, out: &mut BTreeSet<String>) {
        for entry in fs::read_dir(dir).expect("read_dir") {
            let entry = entry.expect("dir entry");
            let path = entry.path();
            if path.is_dir() {
                walk(&path, root, out);
            } else {
                let rel = path.strip_prefix(root).expect("strip root prefix");
                out.insert(rel.to_string_lossy().replace('\\', "/"));
            }
        }
    }
    let mut out = BTreeSet::new();
    walk(root, root, &mut out);
    out
}

fn list_dir_names(dir: &Path) -> BTreeSet<OsString> {
    fs::read_dir(dir)
        .expect("read_dir")
        .map(|e| e.expect("dir entry").file_name())
        .collect()
}

fn today() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 21).expect("valid date")
}

// ---------------------------------------------------------------------
// FR-8/FR-9: sorted destination
// ---------------------------------------------------------------------

#[test]
fn fr08_sorted_ingest_creates_exact_destination_and_nothing_else() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video bytes");

    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert_eq!(
        ingested.classification,
        Classification::Sorted {
            project: "ELS".to_string(),
            date: "260812".to_string(),
            title: "Security issue".to_string(),
        }
    );
    assert_eq!(ingested.collision, CollisionOutcome::Fresh);
    assert!(ingested.meeting_dir.is_absolute());
    assert!(ingested.meeting_dir.is_dir());
    assert!(ingested.source_path.is_absolute());
    assert!(ingested.source_path.is_file());

    let files = list_files(vault.root());
    assert_eq!(
        files,
        BTreeSet::from(["ELS/260812 - Security issue/source.mp4".to_string()])
    );
}

#[test]
fn fr07_uppercase_extension_ingests_and_normalizes_to_lowercase() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.MP4");
    write_file(&source, b"video");

    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert!(ingested.source_path.ends_with("source.mp4"));
    assert!(ingested.source_path.is_file());
}

#[test]
fn fr07_unsupported_extension_creates_nothing() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.exe");
    write_file(&source, b"not a recording");

    let result = vault.ingest_on(&source, today());

    assert_eq!(
        result,
        Err(VaultError::UnsupportedMediaType {
            ext: "exe".to_string()
        })
    );
    assert!(list_files(vault.root()).is_empty());
    assert!(
        !vault.root().join("ELS").exists(),
        "no project folder for an unsupported extension"
    );
    assert!(source.is_file(), "the original .exe is left in place");
}

#[test]
fn fr09_reuses_existing_case_insensitive_project_folder() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    fs::create_dir_all(vault_dir.path().join("els")).expect("pre-create lowercase project dir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert!(ingested.meeting_dir.starts_with(vault.root().join("els")));
    let entries = list_dir_names(vault.root());
    assert!(
        !entries.contains(&OsString::from("ELS")),
        "must not create a case-different sibling: {entries:?}"
    );
}

// ---------------------------------------------------------------------
// FR-10: unsorted
// ---------------------------------------------------------------------

#[test]
fn fr10_unsorted_lands_under_unsorted_with_injected_date_and_is_writable() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("random meeting.mp4");
    write_file(&source, b"video");

    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert_eq!(
        ingested.classification,
        Classification::Unsorted {
            reason: Rejection::MissingSeparator
        }
    );
    assert_eq!(
        ingested.meeting_dir,
        vault
            .root()
            .join("unsorted")
            .join("260821 - random meeting")
    );
    assert!(
        !vault.root().join("ELS").exists(),
        "an unsorted file must never land in a project folder"
    );

    // F2 could write transcript.json into the meeting folder.
    let transcript = ingested.meeting_dir.join("transcript.json");
    write_file(&transcript, b"{}");
    assert!(transcript.is_file());
}

#[test]
fn fr10_two_unsorted_files_are_distinguishable_by_injected_date() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source1 = staging_dir.path().join("meeting one.mp4");
    write_file(&source1, b"video 1");
    let day1 = NaiveDate::from_ymd_opt(2026, 8, 20).unwrap();
    let ingested1 = vault.ingest_on(&source1, day1).expect("ingest 1");

    let source2 = staging_dir.path().join("meeting two.mp4");
    write_file(&source2, b"video 2");
    let day2 = NaiveDate::from_ymd_opt(2026, 8, 21).unwrap();
    let ingested2 = vault.ingest_on(&source2, day2).expect("ingest 2");

    assert_eq!(
        ingested1.meeting_dir.file_name().unwrap().to_str().unwrap(),
        "260820 - meeting one"
    );
    assert_eq!(
        ingested2.meeting_dir.file_name().unwrap().to_str().unwrap(),
        "260821 - meeting two"
    );
}

// ---------------------------------------------------------------------
// FR-11: collisions
// ---------------------------------------------------------------------

#[test]
fn fr11_duplicate_redrop_is_a_noop() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"identical content");
    let first = vault.ingest_on(&source, today()).expect("first ingest");
    assert_eq!(first.collision, CollisionOutcome::Fresh);
    assert!(!source.exists(), "original moved away on first ingest");

    // Recreate an identical re-drop at the same staging path: copying from
    // the vault's own placed file preserves size and mtime (R7), so this is
    // byte-identical to what's already there.
    fs::copy(&first.source_path, &source).expect("seed identical re-drop");

    let second = vault.ingest_on(&source, today()).expect("second ingest");

    assert_eq!(second.collision, CollisionOutcome::DuplicateRedrop);
    assert_eq!(second.meeting_dir, first.meeting_dir);
    assert_eq!(
        fs::read(&first.source_path).unwrap(),
        b"identical content",
        "existing recording must be untouched by a duplicate re-drop"
    );
    assert!(
        source.exists(),
        "a no-op re-drop must not touch the incoming file"
    );
    assert_eq!(
        list_files(vault.root()),
        BTreeSet::from(["ELS/260812 - Security issue/source.mp4".to_string()]),
        "still exactly one source.mp4"
    );
}

#[test]
fn fr11_different_file_same_name_gets_suffixed_folder() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source1 = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source1, b"first recording content");
    let first = vault.ingest_on(&source1, today()).expect("first ingest");

    let source2 = staging_dir
        .path()
        .join("retake")
        .join("ELS - 260812 - Security issue.mp4");
    write_file(&source2, b"different recording content, different size!");
    let second = vault.ingest_on(&source2, today()).expect("second ingest");

    assert_eq!(second.collision, CollisionOutcome::SuffixedFolder(2));
    assert_eq!(
        second.meeting_dir.file_name().unwrap().to_str().unwrap(),
        "260812 - Security issue (2)"
    );
    assert_eq!(
        fs::read(&first.source_path).unwrap(),
        b"first recording content"
    );
    assert_eq!(
        fs::read(&second.source_path).unwrap(),
        b"different recording content, different size!"
    );
}

// ---------------------------------------------------------------------
// FR-12: transfer semantics and rollback
// ---------------------------------------------------------------------

#[test]
fn fr12_original_is_absent_after_success() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    vault.ingest_on(&source, today()).expect("ingest");

    assert!(!source.exists());
}

#[test]
fn fr12_missing_source_returns_source_missing_and_creates_nothing() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    // Never created.
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");

    let result = vault.ingest_on(&source, today());

    assert_eq!(result, Err(VaultError::SourceMissing));
    assert!(list_files(vault.root()).is_empty());
    assert!(!vault.root().join("ELS").exists());
}

#[test]
fn fr12_directory_as_source_returns_source_not_a_file() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    fs::create_dir_all(&source).expect("create directory at source path");

    let result = vault.ingest_on(&source, today());

    assert_eq!(result, Err(VaultError::SourceNotAFile));
    assert!(list_files(vault.root()).is_empty());
}

/// Denies the current user "add file"/"write data" rights (but not "add
/// subdirectory") on `path`, with inheritance, so that:
///  - creating a *subdirectory* under `path` still succeeds (matching
///    `Placement::Fresh`'s guarantee that the meeting directory doesn't
///    exist yet), while
///  - the freshly created subdirectory inherits the same deny, so writing
///    the `source.<ext>` *file* inside it genuinely fails.
///
/// This reproduces a real, deterministic "destination made unwritable"
/// failure (FR-12) without fighting the collision-avoidance policy, which
/// would otherwise route around any single pre-seeded obstacle by trying
/// the next numeric suffix. Restores the original permissions on drop so
/// `tempfile`'s own cleanup can proceed.
#[cfg(windows)]
struct DenyFileCreationGuard {
    path: PathBuf,
    user_spec: String,
}

#[cfg(windows)]
impl DenyFileCreationGuard {
    fn new(path: &Path) -> Self {
        let output = Command::new("whoami")
            .output()
            .expect("whoami must be available on Windows");
        assert!(output.status.success(), "whoami must succeed");
        let user_spec = String::from_utf8(output.stdout)
            .expect("whoami output must be UTF-8")
            .trim()
            .to_string();

        let status = Command::new("icacls")
            .arg(path)
            .arg("/deny")
            .arg(format!("{user_spec}:(OI)(CI)(WD)"))
            .status()
            .expect("icacls must be available on Windows");
        assert!(status.success(), "icacls /deny must succeed");

        DenyFileCreationGuard {
            path: path.to_path_buf(),
            user_spec,
        }
    }
}

#[cfg(windows)]
impl Drop for DenyFileCreationGuard {
    fn drop(&mut self) {
        let _ = Command::new("icacls")
            .arg(&self.path)
            .arg("/remove:d")
            .arg(&self.user_spec)
            .status();
    }
}

// Windows-only mechanism (icacls inheritance is what lets subdirectory
// creation succeed while the file write inside genuinely fails); the
// rollback logic it exercises is platform-independent and stays covered by
// the Windows CI run.
#[cfg(windows)]
#[test]
fn fr12_failed_transfer_removes_only_the_meeting_directory_it_created() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    // A project folder that already exists beforehand must survive any
    // later failure untouched (FR-12).
    let project_dir = vault.root().join("ELS");
    fs::create_dir_all(&project_dir).expect("pre-create project folder");

    let guard = DenyFileCreationGuard::new(&project_dir);

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    let result = vault.ingest_on(&source, today());

    assert!(
        result.is_err(),
        "expected the transfer to fail under a denied destination: {result:?}"
    );

    drop(guard); // restore permissions before inspecting/cleaning up

    let meeting_dir = project_dir.join("260812 - Security issue");
    assert!(
        !meeting_dir.exists(),
        "the meeting folder this call created must be rolled back"
    );
    assert!(
        project_dir.is_dir(),
        "the pre-existing project folder must survive"
    );
    assert!(
        source.is_file(),
        "the original file must remain intact on failure"
    );
    assert!(
        list_files(vault.root()).is_empty(),
        "no source.* may exist anywhere in the vault"
    );
}

// ---------------------------------------------------------------------
// FR-13: result shape
// ---------------------------------------------------------------------

#[test]
fn fr13_result_fields_are_absolute_and_populated_correctly() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let sorted_source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&sorted_source, b"video");
    let sorted = vault
        .ingest_on(&sorted_source, today())
        .expect("sorted ingest");
    assert!(sorted.meeting_dir.is_absolute());
    assert!(sorted.source_path.is_absolute());
    assert_eq!(sorted.collision, CollisionOutcome::Fresh);
    match sorted.classification {
        Classification::Sorted {
            project,
            date,
            title,
        } => {
            assert_eq!(project, "ELS");
            assert_eq!(date, "260812");
            assert_eq!(title, "Security issue");
        }
        Classification::Unsorted { .. } => panic!("expected sorted classification"),
    }

    let unsorted_source = staging_dir.path().join("random meeting.mp4");
    write_file(&unsorted_source, b"video");
    let unsorted = vault
        .ingest_on(&unsorted_source, today())
        .expect("unsorted ingest");
    assert!(unsorted.meeting_dir.is_absolute());
    match unsorted.classification {
        Classification::Unsorted { reason } => assert_eq!(reason, Rejection::MissingSeparator),
        Classification::Sorted { .. } => panic!("expected unsorted classification"),
    }
}

#[test]
fn fr13_returned_paths_are_plain_and_never_carry_the_extended_length_prefix() {
    // `std::fs::canonicalize` returns the Windows extended-length `\\?\`
    // form. That prefix suppresses Win32 path normalization and is
    // rejected or mangled by a large fraction of downstream tooling
    // (ffmpeg, many Python path idioms, anything that round-trips through
    // `os.path.normpath` or a shell) — and it is unpresentable in a UI. F3
    // passes the meeting-folder path straight to F2 as an argument (FR-13),
    // so every path this crate hands back must be the plain form.
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    assert!(
        !vault.root().to_string_lossy().starts_with(r"\\?\"),
        "Vault::root() carried the extended-length prefix: {}",
        vault.root().display()
    );

    let sorted_source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&sorted_source, b"video");
    let sorted = vault
        .ingest_on(&sorted_source, today())
        .expect("sorted ingest");
    assert!(
        !sorted.meeting_dir.to_string_lossy().starts_with(r"\\?\"),
        "sorted meeting_dir carried the extended-length prefix: {}",
        sorted.meeting_dir.display()
    );
    assert!(
        !sorted.source_path.to_string_lossy().starts_with(r"\\?\"),
        "sorted source_path carried the extended-length prefix: {}",
        sorted.source_path.display()
    );

    let unsorted_source = staging_dir.path().join("random meeting.mp4");
    write_file(&unsorted_source, b"video");
    let unsorted = vault
        .ingest_on(&unsorted_source, today())
        .expect("unsorted ingest");
    assert!(
        !unsorted.meeting_dir.to_string_lossy().starts_with(r"\\?\"),
        "unsorted meeting_dir carried the extended-length prefix: {}",
        unsorted.meeting_dir.display()
    );
    assert!(
        !unsorted.source_path.to_string_lossy().starts_with(r"\\?\"),
        "unsorted source_path carried the extended-length prefix: {}",
        unsorted.source_path.display()
    );
}

// ---------------------------------------------------------------------
// FR-14: containment
// ---------------------------------------------------------------------

#[test]
fn fr14_dotdot_style_names_stay_contained_and_never_escape_the_root() {
    // The deeper defense-in-depth cases from FR-14's acceptance bullet — a
    // literal `\\?\C:` or an embedded `\` inside a component — cannot be
    // constructed as a real, single Windows file name at all (the OS itself
    // treats `\` as a path separator), so they are covered as pure-string
    // tests in `tests/code.rs` (T4) and `tests/parse_filename.rs` (T8)
    // instead. Here we exercise the achievable, real-file case: names that
    // are legal single path components but start with `.` characters.
    // The vault root lives inside an *exclusively owned* parent directory
    // (not directly under the shared system temp dir), so that comparing
    // the parent's contents before/after isn't racy against sibling tests
    // concurrently creating their own tempdirs under the same system temp
    // directory.
    let outer_dir = tempdir().expect("outer tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault_root = outer_dir.path().join("vaultroot");
    let vault = Vault::open(&vault_root).expect("open vault");
    let parent_before = list_dir_names(outer_dir.path());

    for name in [
        ".. - 260812 - x.mp4",
        "... - 260812 - x.mp4",
        ".ELS - 260812 - x.mp4",
    ] {
        let source = staging_dir.path().join(name);
        write_file(&source, b"video");
        // An outright rejection is equally acceptable (FR-14); only a
        // successful placement needs the containment assertion.
        if let Ok(ingested) = vault.ingest_on(&source, today()) {
            assert!(
                ingested.meeting_dir.starts_with(vault.root()),
                "meeting_dir for {name:?} must stay inside the vault root"
            );
        }
    }

    assert_eq!(
        list_dir_names(outer_dir.path()),
        parent_before,
        "nothing may ever be created outside the vault root"
    );
}

#[test]
fn nfr4_path_too_long_creates_nothing_before_any_directory() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");

    // Build an artificially deep root so the *destination* path is long
    // even though the incoming file's own (shallow) staging path stays
    // short (R9: the 260-char budget shrinks with the root's own depth).
    let mut deep_root = vault_dir.path().to_path_buf();
    for i in 0..20 {
        deep_root = deep_root.join(format!("segment-{i:02}-abcdefghij"));
    }
    fs::create_dir_all(&deep_root).expect("create deep root chain");
    let vault = Vault::open(&deep_root).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    let result = vault.ingest_on(&source, today());

    assert!(
        matches!(result, Err(VaultError::PathTooLong { .. })),
        "expected PathTooLong, got {result:?}"
    );
    assert!(
        !vault.root().join("ELS").exists(),
        "no project directory may be created before the length check passes"
    );
}

// ---------------------------------------------------------------------
// FR-15: reserved names
// ---------------------------------------------------------------------

#[test]
fn fr15_summary_md_never_exists_after_any_ingest() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let sorted = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&sorted, b"video");
    vault.ingest_on(&sorted, today()).expect("sorted ingest");

    let unsorted = staging_dir.path().join("random meeting.mp4");
    write_file(&unsorted, b"video");
    vault
        .ingest_on(&unsorted, today())
        .expect("unsorted ingest");

    assert!(
        !list_files(vault.root())
            .iter()
            .any(|f| f.ends_with("summary.md")),
        "summary.md must never exist"
    );
}
