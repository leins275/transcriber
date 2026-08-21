//! FR-14 (containment), NFR-4 (260-char cap), FR-8/FR-10/FR-11 (vault name
//! shaping) and FR-15 (reserved names). Exercised against `vault::paths`
//! only. Owned by T5.

use std::fs;

use chrono::NaiveDate;
use tempfile::tempdir;
use vault::error::VaultError;
use vault::paths::{
    check_len, contained_child, meeting_folder_name, suffixed, unsorted_folder_name, SOURCE_STEM,
    SUMMARY_FILE_NAME, TRANSCRIPT_FILE_NAME, UNSORTED_DIR_NAME,
};

fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

fn empty_dir_listing(root: &std::path::Path) -> Vec<std::ffi::OsString> {
    fs::read_dir(root)
        .expect("root must exist")
        .map(|entry| entry.expect("dir entry").file_name())
        .collect()
}

#[test]
fn escaping_component_combinations_are_all_rejected() {
    let dir = tempdir().expect("tempdir");
    let root = dir.path();

    let cases: Vec<Vec<&str>> = vec![
        vec!["..", "x"],
        vec!["ELS", "..", "..", "evil"],
        vec!["C:\\Windows"],
        vec!["\\\\?\\C:"],
        vec!["\\\\server\\share"],
        vec!["ELS/evil"],
        vec!["ELS\\evil"],
        vec!["."],
        vec![""],
    ];

    for components in &cases {
        let result = contained_child(root, components);
        assert_eq!(
            result,
            Err(VaultError::PathEscapesVault),
            "expected {components:?} to escape the vault root"
        );
    }
}

#[test]
fn legal_components_produce_a_path_under_the_canonicalized_root() {
    let dir = tempdir().expect("tempdir");
    let root = dir.path();

    let result = contained_child(root, &["ELS", "260812 - Security issue"])
        .expect("legal components must be accepted");

    let canonical_root = root.canonicalize().expect("root canonicalizes");
    assert!(
        result.starts_with(&canonical_root),
        "{result:?} must start with the canonicalized root {canonical_root:?}"
    );
    assert_eq!(
        result.file_name().unwrap().to_str().unwrap(),
        "260812 - Security issue"
    );
}

#[test]
fn no_rejecting_call_creates_anything_on_disk() {
    let dir = tempdir().expect("tempdir");
    let root = dir.path();

    let cases: Vec<Vec<&str>> = vec![
        vec!["..", "x"],
        vec!["ELS", "..", "..", "evil"],
        vec!["C:\\Windows"],
        vec!["\\\\?\\C:"],
        vec!["\\\\server\\share"],
        vec!["ELS/evil"],
        vec!["ELS\\evil"],
        vec!["."],
        vec![""],
    ];

    for components in &cases {
        let _ = contained_child(root, components);
    }

    assert!(
        empty_dir_listing(root).is_empty(),
        "the containment check must never create anything, even on rejection"
    );
}

#[test]
fn check_len_rejects_a_destination_over_260_characters() {
    // Build an absolute-looking destination whose length is deliberately
    // controlled, independent of any real filesystem path, since the cap is
    // measured on the full absolute string including the `source.<ext>`
    // leaf (R9) rather than on a relative fragment.
    let long_title = "a".repeat(300);
    let destination =
        std::path::PathBuf::from(format!("C:\\vault\\ELS\\260812 - {long_title}\\source.mp4"));
    let len = destination.as_os_str().to_string_lossy().chars().count();
    assert!(len > 260, "test fixture must exceed 260 chars, got {len}");

    let result = check_len(&destination);

    assert_eq!(result, Err(VaultError::PathTooLong { len, limit: 260 }));
}

#[test]
fn check_len_accepts_a_259_character_destination() {
    // Construct a destination whose full absolute length is exactly 259
    // characters, one under the cap.
    let prefix = "C:\\vault\\ELS\\260812 - \\source.mp4"; // fixed scaffolding
    let scaffold_len = prefix.chars().count();
    let fill = "a".repeat(259 - scaffold_len);
    let destination =
        std::path::PathBuf::from(format!("C:\\vault\\ELS\\260812 - {fill}\\source.mp4"));
    let len = destination.as_os_str().to_string_lossy().chars().count();
    assert_eq!(len, 259, "test fixture must be exactly 259 chars");

    assert_eq!(check_len(&destination), Ok(()));
}

#[test]
fn meeting_folder_name_joins_date_and_title() {
    assert_eq!(
        meeting_folder_name("260812", "Security issue"),
        "260812 - Security issue"
    );
}

#[test]
fn unsorted_folder_name_prefixes_the_ingest_date() {
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), "random meeting"),
        "260821 - random meeting"
    );
}

#[test]
fn unsorted_shaper_repairs_illegal_characters_instead_of_rejecting() {
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), "Q3: review"),
        "260821 - Q3_ review"
    );
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), "bad\\name"),
        "260821 - bad_name"
    );
}

#[test]
fn unsorted_shaper_trims_trailing_dots_and_spaces() {
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), "Weekly sync... "),
        "260821 - Weekly sync"
    );
}

#[test]
fn unsorted_shaper_falls_back_to_recording_for_empty_or_all_illegal_stems() {
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), ""),
        "260821 - recording"
    );
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), ":::"),
        "260821 - recording"
    );
}

#[test]
fn unsorted_shaper_truncates_long_stems_on_a_char_boundary() {
    // Every char here is a multi-byte emoji; truncating at a byte offset
    // instead of a char offset would panic or split a code point.
    let emoji_stem: String = "\u{1F600}".repeat(200);

    let name = unsorted_folder_name(date(2026, 8, 21), &emoji_stem);

    let stem_part = name.strip_prefix("260821 - ").expect("date prefix");
    assert_eq!(stem_part.chars().count(), 120);
    assert!(
        stem_part.chars().all(|c| c == '\u{1F600}'),
        "truncation must not corrupt any character"
    );
}

#[test]
fn suffixed_appends_a_parenthesized_number() {
    assert_eq!(
        suffixed("260812 - Security issue", 2),
        "260812 - Security issue (2)"
    );
    assert_eq!(
        suffixed("260812 - Security issue", 3),
        "260812 - Security issue (3)"
    );
}

#[test]
fn reserved_names_are_exported_and_distinct() {
    assert_eq!(SOURCE_STEM, "source");
    assert_eq!(TRANSCRIPT_FILE_NAME, "transcript.json");
    assert_eq!(SUMMARY_FILE_NAME, "summary.md");
    assert_eq!(UNSORTED_DIR_NAME, "unsorted");
}
