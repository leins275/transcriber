//! Tests for `vault::parse::classify_filename` — the pure classification
//! entry point (FR-2, FR-3, NFR-1, NFR-5). Owned by T8.

use std::time::{Duration, Instant};

use vault::error::{Rejection, VaultError};
use vault::parse::{classify_filename, Classified};

fn assert_sorted(name: &str, project: &str, date: &str, title: &str, ext: &str) {
    match classify_filename(name).unwrap_or_else(|e| panic!("expected Ok for {name:?}, got {e:?}"))
    {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.project, project, "project mismatch for {name:?}");
            assert_eq!(parsed.date, date, "date mismatch for {name:?}");
            assert_eq!(parsed.title, title, "title mismatch for {name:?}");
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
        "mp4",
    );
}

#[test]
fn title_may_itself_contain_the_separator() {
    // Split on the first two " - " occurrences only.
    assert_sorted(
        "ELS - 260812 - Security - issue - part 2.mp4",
        "ELS",
        "260812",
        "Security - issue - part 2",
        "mp4",
    );
}

#[test]
fn missing_separators_are_unsorted() {
    assert_unsorted("recording_final(1).mp4", Rejection::MissingSeparator);
}

#[test]
fn whitespace_around_separators_is_optional() {
    // The spaces around `-` are decoration, not structure: the compact
    // form and every mixed form parse identically to the canonical one.
    assert_sorted(
        "ELS-260812-Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        "mp4",
    );
    assert_sorted(
        "ELS -260812- Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        "mp4",
    );
    assert_sorted(
        "ELS  -  260812  -  Security issue.mp4",
        "ELS",
        "260812",
        "Security issue",
        "mp4",
    );
}

#[test]
fn compact_separators_still_leave_later_hyphens_to_the_title() {
    assert_sorted(
        "ELS-260812-follow-up call.mp4",
        "ELS",
        "260812",
        "follow-up call",
        "mp4",
    );
}

#[test]
fn exactly_one_separator_is_unsorted() {
    assert_unsorted("ELS - 260812.mp4", Rejection::MissingSeparator);
}

#[test]
fn parser_needs_no_fixture_on_disk() {
    // FR-2 acceptance: the parser is pure — no filesystem access at all.
    // This test deliberately creates no fixture directory.
    let result = classify_filename("does-not-exist-anywhere - 260812 - x.mp4");
    assert!(result.is_ok());
}

#[test]
fn unsupported_extensions_abort_rather_than_route_to_unsorted() {
    match classify_filename("ELS - 260812 - Security issue.exe") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "exe"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
    match classify_filename("ELS - 260812 - Security issue.txt") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "txt"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
}

#[test]
fn lowercase_project_code_is_sorted_and_capitalized() {
    match classify_filename("els - 260812 - x.mp4").expect("should classify, not error") {
        Classified::Sorted(parsed) => {
            assert_eq!(parsed.project, "ELS");
            assert_eq!(parsed.date, "260812");
            assert_eq!(parsed.title, "x");
        }
        other => panic!("expected Sorted, got {other:?}"),
    }
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
