//! Acceptance sweep (T11) — the traceability artifact for
//! `specs/meeting-vault-layout/spec.md`'s "Acceptance criteria" section.
//!
//! Every bullet under that heading is either exercised by a test in this
//! file, written against the crate's **public** API only (`vault::Vault`,
//! `vault::classify_filename`, …) over a real `tempfile` vault, the way F3
//! will call it — or the test function's doc comment names the earlier
//! task's test file that already covers it in depth, so nothing here
//! duplicates the exhaustive edge-case matrices in `tests/date.rs`,
//! `tests/title.rs`, `tests/code.rs`, `tests/media.rs`, `tests/paths.rs`,
//! `tests/parse_filename.rs`, `tests/parse_fuzz.rs`, `tests/layout.rs`,
//! `tests/transfer.rs`, `tests/appdata.rs` and `tests/ingest.rs`.
//!
//! Two bullets have no single earlier owner and are covered here for the
//! first time: `full_session_sorted_then_unsorted_then_duplicate_redrop`
//! (a realistic sequence of drops asserted against the exact resulting
//! tree) and `fr05_260229_is_rejected_2026_is_not_a_leap_year` (R3: the
//! spec's own acceptance bullet is factually wrong about 2026, and that
//! deviation is recorded as a passing test rather than buried in prose).

use std::collections::BTreeSet;
use std::fs;
use std::io::Write;
use std::path::Path;

use chrono::NaiveDate;
use tempfile::tempdir;
use vault::{Classification, Classified, CollisionOutcome, Rejection, Vault, VaultError};

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dir");
    }
    let mut f = fs::File::create(path).expect("create file");
    f.write_all(bytes).expect("write file");
}

/// Every *file* under `root`, as a `root`-relative, forward-slash path.
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

fn today() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 21).expect("valid date")
}

// ---------------------------------------------------------------------
// FR-1: vault init
// ---------------------------------------------------------------------

/// Deep matrix (parent-missing chain, never-touches-existing-children) in
/// `tests/layout.rs` (`init_on_empty_directory_creates_unsorted`,
/// `init_is_idempotent_and_leaves_existing_children_untouched`,
/// `init_creates_whole_chain_when_parent_is_missing`).
#[test]
fn fr01_init_is_idempotent_and_a_file_root_is_a_typed_error_not_a_panic() {
    let vault_dir = tempdir().expect("vault tempdir");

    let vault = Vault::open(vault_dir.path()).expect("first open creates unsorted/");
    assert!(vault.root().join("unsorted").is_dir());

    // Calling it again (a fresh `Vault::open`) changes nothing and still
    // succeeds.
    Vault::open(vault_dir.path()).expect("second open is a no-op");
    Vault::open(vault_dir.path()).expect("third open is a no-op");
    assert!(vault.root().join("unsorted").is_dir());

    let file_root = vault_dir.path().join("not-a-directory.txt");
    write_file(&file_root, b"i am a file, not a vault root");
    let err = Vault::open(&file_root).expect_err("a file root must be a typed error, not a panic");
    assert_eq!(err, VaultError::RootIsNotADirectory);
}

// ---------------------------------------------------------------------
// FR-2/FR-3: the pure parser
// ---------------------------------------------------------------------

/// Deep matrix in `tests/parse_filename.rs` (every listed case plus the
/// "first failing rule wins" and timing bullets).
#[test]
fn fr02_fr03_pure_parser_splits_on_first_two_separators_with_no_filesystem() {
    // No tempdir anywhere in this test — the parser touches no filesystem
    // at all (FR-2's own acceptance bullet).
    match vault::classify_filename("ELS - 260812 - Security issue.mp4").unwrap() {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.project, "ELS");
            assert_eq!(parsed.date, "260812");
            assert_eq!(parsed.title, "Security issue");
            assert_eq!(parsed.ext, "mp4");
        }
        other => panic!("expected Sorted, got {other:?}"),
    }

    match vault::classify_filename("GIS - 260724 - Client demo.mp4").unwrap() {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.project, "GIS");
            assert_eq!(parsed.date, "260724");
            assert_eq!(parsed.title, "Client demo");
        }
        other => panic!("expected Sorted, got {other:?}"),
    }

    // Split on the first two separators only: the title may itself contain
    // " - ".
    match vault::classify_filename("ELS - 260812 - Security - issue - part 2.mp4").unwrap() {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.title, "Security - issue - part 2");
        }
        other => panic!("expected Sorted, got {other:?}"),
    }

    for name in ["recording_final(1).mp4", "ELS-260812-Security.mp4"] {
        match vault::classify_filename(name).unwrap() {
            Classified::Unsorted { reason, .. } => {
                assert_eq!(reason, Rejection::MissingSeparator, "for {name:?}")
            }
            other => panic!("expected Unsorted for {name:?}, got {other:?}"),
        }
    }
}

// ---------------------------------------------------------------------
// FR-4: project code validation
// ---------------------------------------------------------------------

/// Deep matrix in `tests/code.rs` (length bounds, embedded space, leading
/// digit, path-escape attempts as an invalid code) and
/// `tests/parse_filename.rs::lowercase_project_code_is_sorted_and_capitalized`
/// (case-insensitive matching supersedes R4 — see `code.rs`'s module docs).
#[test]
fn fr04_spaced_or_empty_project_code_is_unsorted() {
    for (name, expected) in [
        ("EL S - 260812 - x.mp4", Rejection::InvalidProjectCode),
        ("1ELS - 260812 - x.mp4", Rejection::InvalidProjectCode),
    ] {
        match vault::classify_filename(name).unwrap() {
            Classified::Unsorted { reason, .. } => assert_eq!(reason, expected, "for {name:?}"),
            other => panic!("expected Unsorted for {name:?}, got {other:?}"),
        }
    }

    // An empty project code: the filename itself has a leading separator,
    // so the code component is the empty string.
    match vault::classify_filename(" - 260812 - x.mp4").unwrap() {
        Classified::Unsorted { reason, .. } => {
            assert_eq!(reason, Rejection::EmptyProjectCode)
        }
        other => panic!("expected Unsorted(EmptyProjectCode), got {other:?}"),
    }
}

// ---------------------------------------------------------------------
// FR-5: calendar-correct date, verbatim in the folder name
// ---------------------------------------------------------------------

/// Deep matrix (Arabic-Indic digits, year range, all-zero month/day
/// combinations) in `tests/date.rs`.
#[test]
fn fr05_calendar_rejections_and_verbatim_date_in_the_folder_name() {
    for (name, expected) in [
        ("ELS - 260230 - x.mp4", Rejection::DateNotACalendarDate), // Feb 30
        ("ELS - 991345 - x.mp4", Rejection::DateNotACalendarDate),
        ("ELS - 2026-08-12 - x.mp4", Rejection::DateNotSixDigits),
        ("ELS - 26081 - x.mp4", Rejection::DateNotSixDigits),
    ] {
        match vault::classify_filename(name).unwrap() {
            Classified::Unsorted { reason, .. } => assert_eq!(reason, expected, "for {name:?}"),
            other => panic!("expected Unsorted for {name:?}, got {other:?}"),
        }
    }

    // The accepted date appears verbatim in the folder name.
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");
    let ingested = vault.ingest_on(&source, today()).expect("ingest");
    assert_eq!(
        ingested.meeting_dir.file_name().unwrap().to_str().unwrap(),
        "260812 - Security issue"
    );
}

/// **R3 — spec defect, recorded here rather than buried in prose.**
///
/// The spec's own FR-5 acceptance bullet reads: "`260230` (Feb 30) and
/// `991345` are rejections; `260229` is accepted (2026 leap-year check
/// applied correctly for `260228`/`260229`)." That is factually wrong:
/// `26` maps to the year 2026, and 2026 is **not** a leap year (the next
/// one after 2024 is 2028). A calendar-correct implementation — which
/// FR-5 explicitly requires ("must denote a real calendar date") — has no
/// choice but to *reject* `260229`, not accept it. This test is the
/// traceability record of that deviation: the real leap-year boundary is
/// exercised with `240229` (2024, a leap year → accepted) and `250229`
/// (2025, not a leap year → rejected), alongside `260228` (accepted) and
/// `260229` (rejected). See plan.md R3 and `tests/date.rs::leap_year_boundary_is_calendar_correct`
/// for the unit-level coverage this integrates.
#[test]
fn fr05_260229_is_rejected_2026_is_not_a_leap_year() {
    match vault::classify_filename("ELS - 260229 - Security issue.mp4").unwrap() {
        Classified::Unsorted { reason, .. } => {
            assert_eq!(
                reason,
                Rejection::DateNotACalendarDate,
                "260229 must be rejected: 2026 is not a leap year, so February \
                 has only 28 days — the spec's acceptance bullet is wrong on this point"
            );
        }
        other => panic!("expected Unsorted(DateNotACalendarDate), got {other:?}"),
    }

    // 260228 (the real last day of February 2026) is accepted.
    match vault::classify_filename("ELS - 260228 - Security issue.mp4").unwrap() {
        Classified::Sorted(parsed) => assert_eq!(parsed.date, "260228"),
        other => panic!("expected Sorted, got {other:?}"),
    }

    // The genuine leap-year boundary: 2024 is a leap year, 2025 is not.
    match vault::classify_filename("ELS - 240229 - Security issue.mp4").unwrap() {
        Classified::Sorted(parsed) => assert_eq!(parsed.date, "240229"),
        other => panic!("expected Sorted (2024 is a leap year), got {other:?}"),
    }
    match vault::classify_filename("ELS - 250229 - Security issue.mp4").unwrap() {
        Classified::Unsorted { reason, .. } => {
            assert_eq!(reason, Rejection::DateNotACalendarDate)
        }
        other => panic!("expected Unsorted (2025 is not a leap year), got {other:?}"),
    }
}

// ---------------------------------------------------------------------
// FR-6: title rules
// ---------------------------------------------------------------------

/// Deep matrix (every illegal char, every reserved device name, control
/// characters, dots-only titles) in `tests/title.rs` and
/// `tests/parse_filename.rs`.
#[test]
fn fr06_illegal_char_reserved_name_blank_title_rejections_and_trailing_space_stripped() {
    for (name, expected) in [
        (
            "ELS - 260812 - Q3: review.mp4",
            Rejection::IllegalTitleCharacter(':'),
        ),
        ("ELS - 260812 - NUL.mp4", Rejection::ReservedDeviceName),
        ("ELS - 260812 -  .mp4", Rejection::EmptyTitle),
    ] {
        match vault::classify_filename(name).unwrap() {
            Classified::Unsorted { reason, .. } => assert_eq!(reason, expected, "for {name:?}"),
            other => panic!("expected Unsorted for {name:?}, got {other:?}"),
        }
    }

    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Review .mp4");
    write_file(&source, b"video");
    let ingested = vault.ingest_on(&source, today()).expect("ingest");
    assert_eq!(
        ingested.meeting_dir.file_name().unwrap().to_str().unwrap(),
        "260812 - Review",
        "trailing space on the title must be stripped from the folder name"
    );
}

// ---------------------------------------------------------------------
// FR-7: media extension gate
// ---------------------------------------------------------------------

/// Deep matrix (all ten extensions, last-extension resolution, non-ASCII
/// extensions) in `tests/media.rs`; the "creates nothing on disk" half of
/// this bullet in `tests/ingest.rs::fr07_unsupported_extension_creates_nothing`.
#[test]
fn fr07_uppercase_media_ingests_lowercase_and_unsupported_types_create_nothing() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.MP4");
    write_file(&source, b"video");
    let ingested = vault.ingest_on(&source, today()).expect("ingest");
    assert!(ingested.source_path.ends_with("source.mp4"));

    for ext in ["exe", "txt"] {
        let bad = staging_dir
            .path()
            .join(format!("ELS - 260812 - Security issue.{ext}"));
        write_file(&bad, b"not a recording");
        let result = vault.ingest_on(&bad, today());
        assert_eq!(
            result,
            Err(VaultError::UnsupportedMediaType {
                ext: ext.to_string()
            })
        );
    }
}

// ---------------------------------------------------------------------
// FR-8/FR-9: sorted destination and project-folder reuse
// ---------------------------------------------------------------------

/// Deep matrix (case-insensitive reuse never renaming, file-conflict typed
/// errors) in `tests/layout.rs` and `tests/ingest.rs`
/// (`fr08_sorted_ingest_creates_exact_destination_and_nothing_else`,
/// `fr09_reuses_existing_case_insensitive_project_folder`).
#[test]
fn fr08_fr09_exact_sorted_destination_and_existing_project_folder_reused() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    fs::create_dir_all(vault_dir.path().join("els")).expect("pre-create lowercase project dir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");
    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert_eq!(
        list_files(vault.root()),
        BTreeSet::from(["els/260812 - Security issue/source.mp4".to_string()]),
        "writes into the pre-existing 'els' folder, not a sibling 'ELS'"
    );
    assert!(ingested.meeting_dir.starts_with(vault.root().join("els")));
}

// ---------------------------------------------------------------------
// FR-10: unsorted layout
// ---------------------------------------------------------------------

/// Deep matrix in `tests/ingest.rs`
/// (`fr10_unsorted_lands_under_unsorted_with_injected_date_and_is_writable`,
/// `fr10_two_unsorted_files_are_distinguishable_by_injected_date`).
#[test]
fn fr10_badly_named_file_lands_under_unsorted_never_in_a_project_folder() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("random meeting.mp4");
    write_file(&source, b"video");

    let ingested = vault.ingest_on(&source, today()).expect("ingest");

    assert_eq!(
        ingested.meeting_dir,
        vault
            .root()
            .join("unsorted")
            .join("260821 - random meeting")
    );
    assert!(matches!(
        ingested.classification,
        Classification::Unsorted {
            reason: Rejection::MissingSeparator
        }
    ));

    // F2 could write transcript.json into it.
    let transcript = ingested.meeting_dir.join("transcript.json");
    write_file(&transcript, b"{}");
    assert!(transcript.is_file());
}

// ---------------------------------------------------------------------
// FR-11: collision policy
// ---------------------------------------------------------------------

/// Deep matrix (suffix-slot skipping, duplicate-matches-a-suffixed-folder,
/// the 999-suffix limit) in `tests/layout.rs` and `tests/ingest.rs`
/// (`fr11_duplicate_redrop_is_a_noop`,
/// `fr11_different_file_same_name_gets_suffixed_folder`).
#[test]
fn fr11_identical_redrop_is_a_noop_and_a_distinct_file_gets_suffixed() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"identical content");
    let first = vault.ingest_on(&source, today()).expect("first ingest");
    assert_eq!(first.collision, CollisionOutcome::Fresh);

    // An identical re-drop (same size + mtime, via a fresh copy of what's
    // already in the vault) is a no-op.
    fs::copy(&first.source_path, &source).expect("seed identical re-drop");
    let second = vault.ingest_on(&source, today()).expect("second ingest");
    assert_eq!(second.collision, CollisionOutcome::DuplicateRedrop);
    assert_eq!(second.meeting_dir, first.meeting_dir);

    // A genuinely different recording with the same name gets a suffixed
    // folder, and both survive.
    let different = staging_dir
        .path()
        .join("retake/ELS - 260812 - Security issue.mp4");
    write_file(&different, b"a completely different, longer recording");
    let third = vault.ingest_on(&different, today()).expect("third ingest");
    assert_eq!(third.collision, CollisionOutcome::SuffixedFolder(2));
    assert_eq!(
        list_files(vault.root()),
        BTreeSet::from([
            "ELS/260812 - Security issue/source.mp4".to_string(),
            "ELS/260812 - Security issue (2)/source.mp4".to_string(),
        ])
    );
}

// ---------------------------------------------------------------------
// FR-12: transfer semantics and rollback
// ---------------------------------------------------------------------

/// Deep matrix (mtime preservation, zero-byte files, the 8 MiB timing
/// bound, the copy-path verify-before-delete ordering) in
/// `tests/transfer.rs`; the destination-made-unwritable rollback scenario
/// in `tests/ingest.rs::fr12_failed_transfer_removes_only_the_meeting_directory_it_created`.
#[test]
fn fr12_successful_ingest_removes_the_original_exactly_as_move_semantics_require() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    vault.ingest_on(&source, today()).expect("ingest");

    assert!(
        !source.exists(),
        "Q3/FR-12: the original is deleted only after a verified transfer"
    );
}

// ---------------------------------------------------------------------
// FR-13: result shape
// ---------------------------------------------------------------------

/// Deep matrix in `tests/ingest.rs::fr13_result_fields_are_absolute_and_populated_correctly`.
#[test]
fn fr13_meeting_dir_is_absolute_and_exists_and_carries_the_right_payload() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");
    let sorted = vault.ingest_on(&source, today()).expect("sorted ingest");
    assert!(sorted.meeting_dir.is_absolute());
    assert!(sorted.meeting_dir.is_dir());
    assert!(matches!(
        sorted.classification,
        Classification::Sorted { .. }
    ));

    let source2 = staging_dir.path().join("random meeting.mp4");
    write_file(&source2, b"video");
    let unsorted = vault.ingest_on(&source2, today()).expect("unsorted ingest");
    assert!(unsorted.meeting_dir.is_absolute());
    assert!(unsorted.meeting_dir.is_dir());
    assert!(matches!(
        unsorted.classification,
        Classification::Unsorted { .. }
    ));
}

// ---------------------------------------------------------------------
// FR-14: containment
// ---------------------------------------------------------------------

/// Deep matrix in `tests/code.rs::rejects_path_escape_attempts_as_invalid_code`,
/// `tests/parse_filename.rs::path_escape_attempts_are_unsorted_and_retain_the_original_stem`
/// and `tests/paths.rs::escaping_component_combinations_are_all_rejected`;
/// the "runs before any directory creation" bullet in
/// `tests/ingest.rs::nfr4_path_too_long_creates_nothing_before_any_directory`.
///
/// A literal `\\?\C:`, an embedded `\` or `/` inside a project code or
/// title cannot be constructed as a single real Windows file name at all
/// (the OS itself treats `\` and `/` as path separators) — those cases are
/// exercised as pure-string tests against `paths::contained_child` and
/// `code::validate` in `tests/paths.rs` and `tests/code.rs` instead. Here,
/// the achievable real-file case is a leading-`..`-style project code.
#[test]
fn fr14_dotdot_style_project_code_creates_nothing_outside_the_root() {
    let outer_dir = tempdir().expect("outer tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault_root = outer_dir.path().join("vaultroot");
    let vault = Vault::open(&vault_root).expect("open vault");

    let source = staging_dir.path().join(".. - 260812 - x.mp4");
    write_file(&source, b"video");
    // Either an outright rejection or a successfully contained result is
    // acceptable — what must never happen is landing outside root.
    if let Ok(ingested) = vault.ingest_on(&source, today()) {
        assert!(ingested.meeting_dir.starts_with(vault.root()));
    }

    assert!(
        fs::read_dir(outer_dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .all(|name| name == "vaultroot"),
        "nothing may ever be created outside the vault root"
    );
}

// ---------------------------------------------------------------------
// FR-15: reserved names
// ---------------------------------------------------------------------

/// Deep matrix in `tests/code.rs::rejects_reserved_word_case_insensitively`,
/// `tests/parse_filename.rs::reserved_project_code_is_unsorted` and
/// `tests/ingest.rs::fr15_summary_md_never_exists_after_any_ingest`.
#[test]
fn fr15_unsorted_reserved_word_routes_to_unsorted_and_summary_md_never_appears() {
    for name in ["unsorted - 260812 - x.mp4", "UNSORTED - 260812 - x.mp4"] {
        match vault::classify_filename(name).unwrap() {
            Classified::Unsorted { reason, .. } => {
                assert_eq!(reason, Rejection::ReservedProjectCode, "for {name:?}")
            }
            other => panic!("expected Unsorted for {name:?}, got {other:?}"),
        }
    }

    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");
    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");
    vault.ingest_on(&source, today()).expect("ingest");

    assert!(
        !list_files(vault.root())
            .iter()
            .any(|f| f.ends_with("summary.md")),
        "summary.md must never exist — it is a placeholder reserved name only"
    );
}

// ---------------------------------------------------------------------
// NFR-3: no panic under randomized input
// ---------------------------------------------------------------------

/// The full 10k+-case fuzz sweep lives in `tests/parse_fuzz.rs`
/// (`ten_thousand_random_filenames_never_panic`,
/// `same_seed_reproduces_the_same_run`) — deliberately not duplicated
/// here. This is a small, direct sanity check that the public entry point
/// itself never panics on the specific NFR-3 examples named in the spec.
#[test]
fn nfr3_unusual_unicode_and_empty_strings_never_panic() {
    for name in ["", "🎥📹.mp4", "\u{200f}reversed.mp4", "\u{0}nul.mp4"] {
        let _ = vault::classify_filename(name);
    }
}

// ---------------------------------------------------------------------
// NFR-4: 260-character path cap
// ---------------------------------------------------------------------

/// Deep matrix (259 vs. over-260, measured on the *full* absolute
/// destination) in `tests/paths.rs`
/// (`check_len_rejects_a_destination_over_260_characters`,
/// `check_len_accepts_a_259_character_destination`); the
/// creates-nothing-before-the-check bullet in
/// `tests/ingest.rs::nfr4_path_too_long_creates_nothing_before_any_directory`.
#[test]
fn nfr4_over_long_destination_fails_with_path_too_long_and_creates_nothing() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");

    let mut deep_root = vault_dir.path().to_path_buf();
    for i in 0..20 {
        deep_root = deep_root.join(format!("segment-{i:02}-abcdefghij"));
    }
    fs::create_dir_all(&deep_root).expect("create deep root chain");
    let vault = Vault::open(&deep_root).expect("open vault");

    let source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&source, b"video");

    let result = vault.ingest_on(&source, today());

    assert!(matches!(result, Err(VaultError::PathTooLong { .. })));
    assert!(!vault.root().join("ELS").exists());
}

// ---------------------------------------------------------------------
// The full-session scenario no single earlier task owns end to end
// ---------------------------------------------------------------------

/// Ingests one sorted recording, one badly named (unsorted) recording,
/// and one duplicate re-drop of the sorted recording, in sequence, and
/// asserts the exact resulting tree — the realistic session F3 will
/// actually produce, rather than one isolated case at a time.
#[test]
fn full_session_sorted_then_unsorted_then_duplicate_redrop_produces_exact_tree() {
    let vault_dir = tempdir().expect("vault tempdir");
    let staging_dir = tempdir().expect("staging tempdir");
    let vault = Vault::open(vault_dir.path()).expect("open vault");

    // 1. One sorted recording.
    let sorted_source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    write_file(&sorted_source, b"sorted recording bytes");
    let sorted = vault
        .ingest_on(&sorted_source, today())
        .expect("sorted ingest");
    assert_eq!(sorted.collision, CollisionOutcome::Fresh);
    assert!(matches!(
        sorted.classification,
        Classification::Sorted { .. }
    ));

    // 2. One badly named recording, ingested on a different injected date.
    let unsorted_source = staging_dir.path().join("random meeting.mp4");
    write_file(&unsorted_source, b"unsorted recording bytes");
    let unsorted_day = NaiveDate::from_ymd_opt(2026, 8, 20).unwrap();
    let unsorted = vault
        .ingest_on(&unsorted_source, unsorted_day)
        .expect("unsorted ingest");
    assert!(matches!(
        unsorted.classification,
        Classification::Unsorted {
            reason: Rejection::MissingSeparator
        }
    ));

    // 3. A duplicate re-drop of the sorted recording: byte-identical size
    // and mtime, via a fresh copy of the already-placed file.
    let redrop_source = staging_dir.path().join("ELS - 260812 - Security issue.mp4");
    fs::copy(&sorted.source_path, &redrop_source).expect("seed identical re-drop");
    let redrop = vault
        .ingest_on(&redrop_source, today())
        .expect("duplicate re-drop ingest");
    assert_eq!(redrop.collision, CollisionOutcome::DuplicateRedrop);
    assert_eq!(redrop.meeting_dir, sorted.meeting_dir);

    // The exact resulting tree: exactly one sorted meeting folder, exactly
    // one unsorted meeting folder, no third folder from the "duplicate"
    // step, and nothing named summary.md anywhere.
    assert_eq!(
        list_files(vault.root()),
        BTreeSet::from([
            "ELS/260812 - Security issue/source.mp4".to_string(),
            "unsorted/260820 - random meeting/source.mp4".to_string(),
        ])
    );
    assert_eq!(
        fs::read(&sorted.source_path).unwrap(),
        b"sorted recording bytes",
        "the duplicate re-drop must not have touched the original bytes"
    );
}
