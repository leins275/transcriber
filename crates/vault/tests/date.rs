//! Tests for the date component (FR-5, NFR-3): `YYMMDD` validation and the
//! local-date clock. Owned by T2.

use vault::date::{format_yymmdd, today_local, validate};
use vault::error::Rejection;

#[test]
fn accepts_valid_dates_verbatim() {
    for raw in ["260812", "260724"] {
        let valid = validate(raw).expect("should be accepted");
        assert_eq!(valid.as_str(), raw);
    }
}

#[test]
fn rejects_non_calendar_dates() {
    assert_eq!(validate("260230"), Err(Rejection::DateNotACalendarDate));
    assert_eq!(validate("991345"), Err(Rejection::DateNotACalendarDate));
}

#[test]
fn leap_year_boundary_is_calendar_correct() {
    // R3: 2026 is not a leap year, so 260229 must be a rejection even though
    // the (incorrect) spec acceptance bullet asked for it to be accepted.
    assert!(validate("260228").is_ok());
    assert_eq!(validate("260229"), Err(Rejection::DateNotACalendarDate));

    // 2024 is a leap year; 2025 is not.
    assert!(validate("240229").is_ok());
    assert_eq!(validate("250229"), Err(Rejection::DateNotACalendarDate));
}

#[test]
fn rejects_strings_that_are_not_exactly_six_ascii_digits() {
    for raw in ["2026-08-12", "26081", "2608123", ""] {
        assert_eq!(
            validate(raw),
            Err(Rejection::DateNotSixDigits),
            "expected DateNotSixDigits for {raw:?}"
        );
    }
}

#[test]
fn rejects_non_ascii_digits() {
    // NFR-3: ASCII-digits-only, never char::is_numeric.
    assert_eq!(
        validate("\u{0662}\u{0666}\u{0660}\u{0668}\u{0661}\u{0662}"),
        Err(Rejection::DateNotSixDigits)
    );
}

#[test]
fn accepts_the_full_year_range() {
    assert!(validate("000101").is_ok()); // year 2000
    assert!(validate("991231").is_ok()); // year 2099
}

#[test]
fn rejects_invalid_month_and_day_components() {
    assert_eq!(validate("260012"), Err(Rejection::DateNotACalendarDate)); // month 00
    assert_eq!(validate("261300"), Err(Rejection::DateNotACalendarDate)); // month 13
    assert_eq!(validate("260800"), Err(Rejection::DateNotACalendarDate)); // day 00
}

#[test]
fn today_local_round_trips_through_the_validator() {
    let today = today_local();
    let formatted = format_yymmdd(today);

    assert_eq!(formatted.len(), 6);
    assert!(formatted.chars().all(|c| c.is_ascii_digit()));

    let valid = validate(&formatted).expect("today's own formatting must validate");
    assert_eq!(valid.as_str(), formatted);
    assert_eq!(valid.date(), today);
}
