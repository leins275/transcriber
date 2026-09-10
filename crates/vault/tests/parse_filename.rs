//! Tests for `vault::parse::classify_filename` — the pure classification
//! entry point.
//!
//! The grammar is `<PRJ> - <DATE> - <NAME>[ - <TYPE>].<ext>` with `-`
//! reserved as the separator everywhere: the stem is split on *every*
//! hyphen, each section is trimmed of ASCII spaces only, three sections
//! are an untyped meeting, four carry a type and five or more route to
//! `unsorted` (FR-1, NFR-1).

use std::time::{Duration, Instant};

use vault::error::{Rejection, VaultError};
use vault::parse::{classify_filename, Classified};

fn assert_sorted(
    name: &str,
    project: &str,
    date: &str,
    title: &str,
    kind: Option<&str>,
    ext: &str,
) {
    match classify_filename(name).unwrap_or_else(|e| panic!("expected Ok for {name:?}, got {e:?}"))
    {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.project, project, "project mismatch for {name:?}");
            assert_eq!(parsed.date, date, "date mismatch for {name:?}");
            assert_eq!(parsed.title, title, "title mismatch for {name:?}");
            assert_eq!(parsed.kind.as_deref(), kind, "kind mismatch for {name:?}");
            assert_eq!(parsed.ext, ext, "ext mismatch for {name:?}");
        }
        other => panic!("expected Sorted for {name:?}, got {other:?}"),
    }
}

fn assert_unsorted(name: &str, expected: Rejection) {
    match classify_filename(name).unwrap_or_else(|e| panic!("expected Ok for {name:?}, got {e:?}"))
    {
        Classified::Unsorted { reason, .. } => {
            assert_eq!(reason, expected, "reason mismatch for {name:?}");
        }
        other => panic!("expected Unsorted for {name:?}, got {other:?}"),
    }
}

#[test]
fn parses_a_well_formed_name() {
    assert_sorted(
        "ELS - 260812 - Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        None,
        "mp4",
    );
}

#[test]
fn parses_another_well_formed_name() {
    assert_sorted(
        "GIS - 260724 - Client demo.mp4",
        "GIS",
        "260724",
        "Client demo",
        None,
        "mp4",
    );
}

#[test]
fn a_fourth_section_is_the_meeting_type() {
    assert_sorted(
        "ELS - 260812 - Security issue - Standup.mp4",
        "ELS",
        "260812",
        "Security issue",
        Some("Standup"),
        "mp4",
    );
}

#[test]
fn spaces_inside_a_title_and_a_type_survive_the_split() {
    assert_sorted(
        "GIS - 260724 - Weekly sync - Sprint review.mp4",
        "GIS",
        "260724",
        "Weekly sync",
        Some("Sprint review"),
        "mp4",
    );
}

#[test]
fn whitespace_around_the_type_separator_is_optional() {
    // The spaces around `-` are decoration, not structure: the compact
    // form and every mixed form parse identically to the canonical one.
    assert_sorted(
        "ELS-260812-Security issue-Standup.mp4",
        "ELS",
        "260812",
        "Security issue",
        Some("Standup"),
        "mp4",
    );
    assert_sorted(
        "ELS - 260812 - Security issue -Standup.mp4",
        "ELS",
        "260812",
        "Security issue",
        Some("Standup"),
        "mp4",
    );
    assert_sorted(
        "ELS - 260812 - Security issue  -  Standup.mp4",
        "ELS",
        "260812",
        "Security issue",
        Some("Standup"),
        "mp4",
    );
}

#[test]
fn whitespace_around_separators_is_optional() {
    assert_sorted(
        "ELS-260812-Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        None,
        "mp4",
    );
    assert_sorted(
        "ELS -260812- Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        None,
        "mp4",
    );
    assert_sorted(
        "ELS  -  260812  -  Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        None,
        "mp4",
    );
}

#[test]
fn a_fifth_section_is_unsorted_rather_than_a_hyphenated_title() {
    // `-` is the separator everywhere: a name carrying one inside a
    // section has too many sections to route, so it goes to `unsorted`.
    assert_unsorted(
        "ELS - 260812 - Security - issue - part 2.mp4",
        Rejection::TooManySeparators,
    );
}

#[test]
fn a_hyphen_inside_a_compact_name_is_a_separator_too() {
    assert_unsorted(
        "ELS-260812-follow-up call-Standup.mp4",
        Rejection::TooManySeparators,
    );
}

#[test]
fn a_real_vault_name_with_hyphens_in_its_timestamp_is_unsorted() {
    assert_unsorted(
        "TBOT - 260731 - Запись встречи 31.07.2026 11-04-56 - запись.mp4",
        Rejection::TooManySeparators,
    );
}

#[test]
fn a_too_many_sections_name_keeps_its_verbatim_stem_for_the_unsorted_folder() {
    match classify_filename("ELS - 260812 - Security - issue - part 2.mp4")
        .expect("should classify, not error")
    {
        Classified::Unsorted { reason, stem, .. } => {
            assert_eq!(reason, Rejection::TooManySeparators);
            assert_eq!(stem, "ELS - 260812 - Security - issue - part 2");
        }
        other => panic!("expected Unsorted, got {other:?}"),
    }
}

#[test]
fn missing_separators_are_unsorted() {
    assert_unsorted("recording_final(1).mp4", Rejection::MissingSeparator);
}

#[test]
fn exactly_one_separator_is_unsorted() {
    assert_unsorted("ELS - 260812.mp4", Rejection::MissingSeparator);
    assert_unsorted("just one - separator.mp4", Rejection::MissingSeparator);
}

#[test]
fn a_present_but_empty_type_is_unsorted() {
    assert_unsorted("ELS - 260812 - Title - .mp4", Rejection::EmptyType);
    assert_unsorted("ELS - 260812 - Title -.mp4", Rejection::EmptyType);
}

#[test]
fn an_illegal_character_in_the_type_is_reported_against_the_type() {
    assert_unsorted(
        "ELS - 260812 - Title - Q:A.mp4",
        Rejection::IllegalTypeCharacter(':'),
    );
}

#[test]
fn a_reserved_device_name_as_the_type_is_unsorted() {
    assert_unsorted(
        "ELS - 260812 - Title - CON.mp4",
        Rejection::ReservedDeviceName,
    );
}

#[test]
fn a_tab_beside_the_type_separator_reaches_the_validator_rather_than_being_trimmed() {
    // Only ASCII spaces are decoration. A control character hiding next to
    // a separator must still be reported, never silently trimmed away.
    assert_unsorted(
        "ELS - 260812 - Title -\tK.mp4",
        Rejection::IllegalTypeCharacter('\t'),
    );
}

#[test]
fn a_tab_beside_the_date_separator_reaches_the_validator_rather_than_being_trimmed() {
    assert_unsorted("ELS -\t260812 - Title.mp4", Rejection::DateNotSixDigits);
}

#[test]
fn the_section_count_is_judged_before_the_project_code() {
    assert_unsorted(
        "1ELS - 260812 - T - a - b.mp4",
        Rejection::TooManySeparators,
    );
}

#[test]
fn the_project_code_is_judged_before_the_type() {
    assert_unsorted("1ELS - 260812 - T - K.mp4", Rejection::InvalidProjectCode);
}

#[test]
fn the_title_is_judged_before_the_type() {
    assert_unsorted(
        "ELS - 260812 - Q:A - K.mp4",
        Rejection::IllegalTitleCharacter(':'),
    );
}

#[test]
fn parser_needs_no_fixture_on_disk() {
    // The parser is pure — no filesystem access at all. This test
    // deliberately creates no fixture directory.
    assert_sorted(
        "NOPE - 260812 - does not exist anywhere.mp4",
        "NOPE",
        "260812",
        "does not exist anywhere",
        None,
        "mp4",
    );
}

#[test]
fn unsupported_extensions_abort_rather_than_route_to_unsorted() {
    match classify_filename("ELS - 260812 - Security issue.exe") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "exe"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
    match classify_filename("ELS - 260812 - Title - K.exe") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "exe"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
}

#[test]
fn the_extension_gate_runs_before_the_section_count() {
    match classify_filename("ELS - 260812 - a - b - c.txt") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "txt"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
}

#[test]
fn lowercase_project_code_is_sorted_and_capitalized() {
    assert_sorted("els - 260812 - x.mp4", "ELS", "260812", "x", None, "mp4");
}

#[test]
fn reserved_project_code_is_unsorted() {
    assert_unsorted("unsorted - 260812 - x.mp4", Rejection::ReservedProjectCode);
    assert_unsorted("UNSORTED - 260812 - x.mp4", Rejection::ReservedProjectCode);
}

#[test]
fn bad_date_is_unsorted() {
    assert_unsorted("ELS - 260230 - x.mp4", Rejection::DateNotACalendarDate);
}

#[test]
fn bad_title_is_unsorted() {
    assert_unsorted(
        "ELS - 260812 - Q3: review.mp4",
        Rejection::IllegalTitleCharacter(':'),
    );
    assert_unsorted("ELS - 260812 - NUL.mp4", Rejection::ReservedDeviceName);
    assert_unsorted("ELS - 260812 -  .mp4", Rejection::EmptyTitle);
}

#[test]
fn path_escape_attempts_are_unsorted_and_retain_the_original_stem() {
    match classify_filename(".. - 260812 - x.mp4").expect("should classify, not error") {
        Classified::Unsorted { reason, stem, .. } => {
            assert_eq!(reason, Rejection::InvalidProjectCode);
            assert_eq!(stem, ".. - 260812 - x");
        }
        other => panic!("expected Unsorted, got {other:?}"),
    }

    match classify_filename("ELS - 260812 - ..\\..\\evil.mp4").expect("should classify, not error")
    {
        Classified::Unsorted { reason, stem, .. } => {
            assert_eq!(reason, Rejection::IllegalTitleCharacter('\\'));
            assert_eq!(stem, "ELS - 260812 - ..\\..\\evil");
        }
        other => panic!("expected Unsorted, got {other:?}"),
    }
}

#[test]
fn first_failing_rule_wins() {
    // Code is checked before date before title — a name that is wrong in
    // more than one place reports the earliest, specific reason.
    assert_unsorted(
        "EL S - 260230 - Q3: review.mp4",
        Rejection::InvalidProjectCode,
    );
}

#[test]
fn parsing_a_long_filename_is_fast() {
    // NFR-1: under 1ms per call for inputs up to 4096 chars. Measure in
    // batches (large enough to beat timer granularity) and judge the
    // fastest one: scheduler noise on a shared CI runner only ever *adds*
    // time, so the best batch is what the code itself costs, while a
    // single-shot measurement flakes on any preemption. The per-call bound
    // stays the NFR's own 1ms.
    let title = "A".repeat(4096 - "ELS - 260812 - .mp4".len());
    let name = format!("ELS - 260812 - {title}.mp4");
    assert_eq!(name.len(), 4096);

    const BATCHES: usize = 5;
    const CALLS_PER_BATCH: u32 = 20;
    let best = (0..BATCHES)
        .map(|_| {
            let start = Instant::now();
            for _ in 0..CALLS_PER_BATCH {
                let _ = classify_filename(&name);
            }
            start.elapsed()
        })
        .min()
        .unwrap();

    assert!(
        best < Duration::from_millis(CALLS_PER_BATCH as u64),
        "fastest batch of {CALLS_PER_BATCH} calls took {best:?}, expected under 1ms per call"
    );
}
