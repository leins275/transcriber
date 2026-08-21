//! Tests for `vault::code` — project code pattern and reserved word (FR-4,
//! FR-15) plus FR-14 defense-in-depth.

use vault::code;
use vault::error::Rejection;

#[test]
fn accepts_valid_codes() {
    assert!(code::validate("ELS").is_ok());
    assert!(code::validate("GIS").is_ok());
    assert!(code::validate("AB").is_ok());
    assert!(code::validate("A1B2C3").is_ok());
    assert!(code::validate("ABCDEFGHIJ").is_ok());
}

#[test]
fn accepted_code_matches_input_uppercased() {
    let c = code::validate("ELS").expect("ELS should be valid");
    assert_eq!(c.as_str(), "ELS");
}

#[test]
fn rejects_too_short_or_too_long() {
    assert_eq!(code::validate("A"), Err(Rejection::InvalidProjectCode));
    assert_eq!(
        code::validate("ABCDEFGHIJK"),
        Err(Rejection::InvalidProjectCode)
    );
}

#[test]
fn rejects_lowercase() {
    assert_eq!(code::validate("els"), Err(Rejection::InvalidProjectCode));
    assert_eq!(code::validate("Els"), Err(Rejection::InvalidProjectCode));
}

#[test]
fn rejects_embedded_space() {
    assert_eq!(code::validate("EL S"), Err(Rejection::InvalidProjectCode));
}

#[test]
fn rejects_empty() {
    assert_eq!(code::validate(""), Err(Rejection::EmptyProjectCode));
}

#[test]
fn rejects_leading_digit() {
    assert_eq!(code::validate("1ELS"), Err(Rejection::InvalidProjectCode));
}

#[test]
fn rejects_reserved_word_case_insensitively() {
    assert_eq!(
        code::validate("UNSORTED"),
        Err(Rejection::ReservedProjectCode)
    );
    assert_eq!(
        code::validate("unsorted"),
        Err(Rejection::ReservedProjectCode)
    );
    assert_eq!(
        code::validate("Unsorted"),
        Err(Rejection::ReservedProjectCode)
    );
}

#[test]
fn rejects_path_escape_attempts_as_invalid_code() {
    for raw in ["..", "\\\\?\\C:", "C:", "ELS\\evil", "ELS/evil"] {
        let result = code::validate(raw);
        assert!(
            result.is_err(),
            "expected {raw:?} to be rejected, got {result:?}"
        );
    }
}
