//! FR-14 (containment), NFR-4/FR-5 (260-char cap), FR-8/FR-10/FR-11 (vault
//! name shaping), FR-3 (the typed meeting folder name and its parser) and
//! FR-15 (reserved names). Exercised against `vault::paths` only. Owned by
//! T5, extended by T3 for the meeting type.

use std::fs;

use chrono::NaiveDate;
use tempfile::tempdir;
use vault::error::VaultError;
use vault::paths::{
    check_len, contained_child, meeting_folder_name, parse_meeting_folder_name, suffixed,
    unsorted_folder_name, SOURCE_STEM, SUMMARY_FILE_NAME, TRANSCRIPT_FILE_NAME, UNSORTED_DIR_NAME,
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
fn check_len_accepts_a_typed_destination_of_exactly_260_characters() {
    // 13 characters of root, `260812 - `, 217 title characters,
    // ` - Standup` and 11 characters of `\source.mp4` make exactly 260 —
    // the last length still allowed.
    let title = "a".repeat(217);
    let folder = meeting_folder_name("260812", &title, Some("Standup"));
    let destination = std::path::PathBuf::from(format!("C:\\vault\\ELS\\{folder}\\source.mp4"));
    assert_eq!(
        destination.as_os_str().to_string_lossy().chars().count(),
        260,
        "test fixture must be exactly 260 chars"
    );

    assert_eq!(check_len(&destination), Ok(()));
}

#[test]
fn a_type_that_pushes_a_fitting_destination_past_the_cap_is_an_error() {
    // The same meeting without a type measures 259 characters and is
    // accepted (see `check_len_accepts_a_259_character_destination`);
    // ` - Standup` adds ten more. An over-long destination is refused with
    // an error — it is never repaired, truncated or re-routed to
    // `unsorted` (FR-5).
    let title = "a".repeat(226);
    let folder = meeting_folder_name("260812", &title, Some("Standup"));
    let destination = std::path::PathBuf::from(format!("C:\\vault\\ELS\\{folder}\\source.mp4"));

    let result = check_len(&destination);

    assert_eq!(
        result,
        Err(VaultError::PathTooLong {
            len: 269,
            limit: 260
        })
    );
}

#[test]
fn meeting_folder_name_joins_date_and_title() {
    assert_eq!(
        meeting_folder_name("260812", "Security issue", None),
        "260812 - Security issue"
    );
}

#[test]
fn meeting_folder_name_appends_the_type_as_a_third_section() {
    assert_eq!(
        meeting_folder_name("260812", "Security issue", Some("Standup")),
        "260812 - Security issue - Standup"
    );
}

#[test]
fn an_untyped_folder_name_parses_back_into_its_date_and_title() {
    let parsed = parse_meeting_folder_name("260812 - Security issue")
        .expect("a two-section folder name is a valid untyped meeting");

    assert_eq!(parsed.date, "260812");
    assert_eq!(parsed.title, "Security issue");
    assert_eq!(parsed.kind, None);
}

#[test]
fn a_typed_folder_name_parses_back_into_its_date_title_and_type() {
    let parsed = parse_meeting_folder_name("260812 - Security issue - Standup")
        .expect("a three-section folder name is a valid typed meeting");

    assert_eq!(parsed.date, "260812");
    assert_eq!(parsed.title, "Security issue");
    assert_eq!(parsed.kind.as_deref(), Some("Standup"));
}

#[test]
fn spaces_around_the_separators_are_optional_when_parsing_a_folder_name() {
    let parsed = parse_meeting_folder_name("260812-Security issue-Standup")
        .expect("compact separators name the same meeting");

    assert_eq!(parsed.date, "260812");
    assert_eq!(parsed.title, "Security issue");
    assert_eq!(parsed.kind.as_deref(), Some("Standup"));
}

#[test]
fn every_shape_of_valid_name_survives_a_build_and_parse_round_trip() {
    let cases: Vec<(&str, &str, Option<&str>, &str)> = vec![
        ("260812", "Security issue", None, "260812 - Security issue"),
        (
            "260812",
            "Security issue",
            Some("Standup"),
            "260812 - Security issue - Standup",
        ),
        (
            "260828",
            "Q&A Сессия с Дмитрием",
            Some("поиск рабочего триггера"),
            "260828 - Q&A Сессия с Дмитрием - поиск рабочего триггера",
        ),
        ("260903", "v2.1 review", None, "260903 - v2.1 review"),
        ("260101", "a", Some("b"), "260101 - a - b"),
        (
            "260724",
            "Weekly sync",
            Some("Sprint review"),
            "260724 - Weekly sync - Sprint review",
        ),
    ];

    for (meeting_date, title, kind, expected_name) in cases {
        let built = meeting_folder_name(meeting_date, title, kind);
        assert_eq!(built, expected_name, "built name for {title:?}");

        let parsed = parse_meeting_folder_name(expected_name)
            .unwrap_or_else(|| panic!("{expected_name:?} must parse back"));
        assert_eq!(parsed.date, meeting_date, "date of {expected_name:?}");
        assert_eq!(parsed.title, title, "title of {expected_name:?}");
        assert_eq!(parsed.kind.as_deref(), kind, "kind of {expected_name:?}");
    }
}

#[test]
fn folder_names_outside_the_two_or_three_section_grammar_do_not_parse() {
    let cases = [
        "260731 - Запись встречи 31.07.2026 11-04-56 - запись",
        "just a folder",
        "26081 - Too short",
        "260812 -",
        "260812 - Title -",
        "260812 - - K",
        "26o812 - Not six digits",
    ];

    for name in cases {
        assert!(
            parse_meeting_folder_name(name).is_none(),
            "{name:?} must not parse as a meeting folder name"
        );
    }
}

#[test]
fn a_tab_beside_a_separator_is_kept_so_the_folder_name_does_not_parse() {
    // Sections are trimmed of ASCII spaces *only* — the rule
    // `classify_filename` and the app's `parseMeetingName` follow as well —
    // so a tab beside the separator stays inside its section and leaves the
    // date seven characters long rather than a `YYMMDD`. A `str::trim()`
    // here would accept both names and put this parser out of step with the
    // other two.
    assert!(parse_meeting_folder_name("260812\t- Security issue").is_none());
    assert!(parse_meeting_folder_name("260812\t- Security issue - Standup").is_none());
}

#[test]
fn unsorted_folder_name_prefixes_the_ingest_date() {
    assert_eq!(
        unsorted_folder_name(date(2026, 8, 21), "random meeting"),
        "260821 - random meeting"
    );
}

#[test]
fn unsorted_folder_name_keeps_every_hyphen_of_a_non_conforming_stem() {
    assert_eq!(
        unsorted_folder_name(
            date(2026, 9, 10),
            "Запись встречи 31.07.2026 11-04-56 - запись"
        ),
        "260910 - Запись встречи 31.07.2026 11-04-56 - запись"
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
