//! Tests for the vault layout module (FR-1, FR-9, FR-11). Owned by T9.
//!
//! All cases use real directories via `tempfile` — no mocked filesystem.

use std::fs;
use std::io::Write;
use std::path::Path;

use tempfile::tempdir;
use vault::error::VaultError;
use vault::layout::{ensure_project_dir, init, resolve_meeting_dir, Placement};

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dir");
    }
    let mut f = fs::File::create(path).expect("create file");
    f.write_all(bytes).expect("write file");
}

// ---------------------------------------------------------------------
// FR-1: init
// ---------------------------------------------------------------------

#[test]
fn init_on_empty_directory_creates_unsorted() {
    let dir = tempdir().expect("tempdir");

    init(dir.path()).expect("init should succeed");

    assert!(dir.path().join("unsorted").is_dir());
}

#[test]
fn init_is_idempotent_and_leaves_existing_children_untouched() {
    let dir = tempdir().expect("tempdir");
    write_file(&dir.path().join("ELS").join("marker.txt"), b"keep me");

    init(dir.path()).expect("first init");
    init(dir.path()).expect("second init");
    init(dir.path()).expect("third init");

    assert!(dir.path().join("unsorted").is_dir());
    let marker = dir.path().join("ELS").join("marker.txt");
    assert_eq!(
        fs::read(&marker).expect("marker must survive repeated init calls"),
        b"keep me"
    );
}

#[test]
fn init_on_file_path_returns_typed_error_not_panic() {
    let dir = tempdir().expect("tempdir");
    let file_path = dir.path().join("not-a-directory");
    write_file(&file_path, b"i am a file");

    let result = init(&file_path);

    assert_eq!(result, Err(VaultError::RootIsNotADirectory));
}

#[test]
fn init_creates_whole_chain_when_parent_is_missing() {
    let dir = tempdir().expect("tempdir");
    let nested_root = dir.path().join("a").join("b").join("root");

    init(&nested_root).expect("init should create the whole chain");

    assert!(nested_root.is_dir());
    assert!(nested_root.join("unsorted").is_dir());
}

// ---------------------------------------------------------------------
// FR-9: ensure_project_dir
// ---------------------------------------------------------------------

#[test]
fn ensure_project_dir_reuses_existing_case_insensitive_folder() {
    let dir = tempdir().expect("tempdir");
    fs::create_dir_all(dir.path().join("els")).unwrap();
    let canonical_root = dir.path().canonicalize().expect("root canonicalizes");

    let result = ensure_project_dir(dir.path(), "ELS").expect("should reuse existing folder");

    assert_eq!(result, canonical_root.join("els"));
    let entries: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(
        entries,
        vec![std::ffi::OsString::from("els")],
        "resolver must reuse the existing entry, not create a case-different sibling"
    );
}

#[test]
fn ensure_project_dir_creates_uppercase_when_absent() {
    let dir = tempdir().expect("tempdir");
    let canonical_root = dir.path().canonicalize().expect("root canonicalizes");

    let result = ensure_project_dir(dir.path(), "ELS").expect("should create fresh folder");

    assert_eq!(result, canonical_root.join("ELS"));
    assert!(dir.path().join("ELS").is_dir());
}

#[test]
fn ensure_project_dir_file_conflict_is_a_typed_error_not_panic() {
    let dir = tempdir().expect("tempdir");
    write_file(&dir.path().join("ELS"), b"i am a file, not a folder");

    let result = ensure_project_dir(dir.path(), "ELS");

    assert!(result.is_err());
}

#[test]
fn ensure_project_dir_never_renames_what_it_finds() {
    let dir = tempdir().expect("tempdir");
    fs::create_dir_all(dir.path().join("Els")).unwrap();
    let canonical_root = dir.path().canonicalize().expect("root canonicalizes");

    let first = ensure_project_dir(dir.path(), "ELS").expect("first call");
    let second = ensure_project_dir(dir.path(), "ELS").expect("second call");

    assert_eq!(first, canonical_root.join("Els"));
    assert_eq!(second, canonical_root.join("Els"));
    let entries: Vec<_> = fs::read_dir(dir.path()).unwrap().collect();
    assert_eq!(entries.len(), 1, "no sibling folder must be created");
}

// ---------------------------------------------------------------------
// FR-11: resolve_meeting_dir
// ---------------------------------------------------------------------

const BASE_NAME: &str = "260812 - Security issue";

#[test]
fn resolve_meeting_dir_is_fresh_when_name_is_free() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"hello world");

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");

    assert_eq!(result, Placement::Fresh);
    assert!(
        !dir.path().join(BASE_NAME).exists(),
        "resolution must not create the meeting directory itself"
    );
}

#[test]
fn resolve_meeting_dir_reports_duplicate_redrop_for_identical_recording() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"hello world");
    let existing = dir.path().join(BASE_NAME).join("source.mp4");
    fs::create_dir_all(existing.parent().unwrap()).unwrap();
    // fs::copy preserves mtime on Windows (R7), so `existing` and `incoming`
    // share both size and mtime.
    fs::copy(&incoming, &existing).expect("seed existing recording");
    let mtime_before = fs::metadata(&existing).unwrap().modified().unwrap();

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");

    assert_eq!(
        result,
        Placement::DuplicateRedrop {
            dir: BASE_NAME.to_string()
        }
    );
    assert_eq!(
        fs::read(&existing).unwrap(),
        b"hello world",
        "existing recording must be byte-for-byte untouched"
    );
    assert_eq!(
        fs::metadata(&existing).unwrap().modified().unwrap(),
        mtime_before,
        "existing recording's mtime must be untouched"
    );
}

#[test]
fn resolve_meeting_dir_suffixes_when_name_taken_by_a_different_recording() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"new content, different size!");
    let existing = dir.path().join(BASE_NAME).join("source.mp4");
    write_file(&existing, b"old");

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");

    assert_eq!(
        result,
        Placement::Suffixed {
            dir: format!("{BASE_NAME} (2)"),
            n: 2,
        }
    );
    assert!(
        !dir.path().join(format!("{BASE_NAME} (2)")).exists(),
        "resolution must not create the suffixed directory itself"
    );
}

#[test]
fn resolve_meeting_dir_skips_a_suffix_slot_already_taken() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"new content, different size!");
    write_file(&dir.path().join(BASE_NAME).join("source.mp4"), b"old-1");
    write_file(
        &dir.path()
            .join(format!("{BASE_NAME} (2)"))
            .join("source.mp4"),
        b"old-2",
    );

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");

    assert_eq!(
        result,
        Placement::Suffixed {
            dir: format!("{BASE_NAME} (3)"),
            n: 3,
        }
    );
}

#[test]
fn resolve_meeting_dir_matches_duplicate_against_a_suffixed_folder() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"hello world");
    write_file(&dir.path().join(BASE_NAME).join("source.mp4"), b"unrelated");
    let suffixed_existing = dir
        .path()
        .join(format!("{BASE_NAME} (2)"))
        .join("source.mp4");
    fs::create_dir_all(suffixed_existing.parent().unwrap()).unwrap();
    fs::copy(&incoming, &suffixed_existing).expect("seed suffixed duplicate");

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");

    assert_eq!(
        result,
        Placement::DuplicateRedrop {
            dir: format!("{BASE_NAME} (2)")
        }
    );
    assert_eq!(fs::read(&suffixed_existing).unwrap(), b"hello world");
}

#[test]
fn resolve_meeting_dir_exceeds_suffix_limit() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"new content, different size!");
    write_file(&dir.path().join(BASE_NAME).join("source.mp4"), b"old-1");
    for n in 2..=999u32 {
        write_file(
            &dir.path()
                .join(format!("{BASE_NAME} ({n})"))
                .join("source.mp4"),
            format!("old-{n}").as_bytes(),
        );
    }

    let result = resolve_meeting_dir(dir.path(), BASE_NAME, &incoming);

    assert_eq!(result, Err(VaultError::SuffixLimitExceeded));
}

#[test]
fn resolve_meeting_dir_never_writes_anything() {
    let dir = tempdir().expect("tempdir");
    let incoming = dir.path().join("incoming.mp4");
    write_file(&incoming, b"hello world");

    resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve");
    resolve_meeting_dir(dir.path(), BASE_NAME, &incoming).expect("resolve again");

    let entries: Vec<_> = fs::read_dir(dir.path()).unwrap().collect();
    // Only the incoming file itself exists; resolution creates nothing.
    assert_eq!(entries.len(), 1);
}
