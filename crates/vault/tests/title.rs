//! FR-6: title rules — Windows-illegal characters, trimming, reserved
//! device names. Exercised against `vault::title` only.

use vault::error::Rejection;
use vault::title;

#[test]
fn plain_titles_accepted_verbatim() {
    assert_eq!(
        &*title::validate("Security issue").unwrap(),
        "Security issue"
    );
    assert_eq!(&*title::validate("Client demo").unwrap(), "Client demo");
    assert_eq!(
        &*title::validate("Security - issue - part 2").unwrap(),
        "Security - issue - part 2"
    );
}

#[test]
fn each_windows_illegal_character_is_rejected_with_the_offending_char() {
    for c in ['<', '>', ':', '"', '/', '\\', '|', '?', '*'] {
        let raw = format!("bad{c}title");
        let result = title::validate(&raw);
        assert_eq!(
            result,
            Err(Rejection::IllegalTitleCharacter(c)),
            "input {raw:?} should reject on {c:?}"
        );
    }
}

#[test]
fn colon_in_title_reports_the_colon() {
    assert_eq!(
        title::validate("Q3: review"),
        Err(Rejection::IllegalTitleCharacter(':'))
    );
}

#[test]
fn control_characters_are_rejected() {
    assert!(matches!(
        title::validate("bad\u{0}title"),
        Err(Rejection::IllegalTitleCharacter('\u{0}'))
    ));
    assert!(matches!(
        title::validate("bad\u{1f}title"),
        Err(Rejection::IllegalTitleCharacter('\u{1f}'))
    ));
    assert!(matches!(
        title::validate("bad\u{7f}title"),
        Err(Rejection::IllegalTitleCharacter('\u{7f}'))
    ));
}

#[test]
fn reserved_device_names_are_rejected_case_insensitively() {
    for name in [
        "NUL", "nul", "CON", "com1", "COM9", "LPT1", "LPT9", "AUX", "PRN",
    ] {
        assert_eq!(
            title::validate(name),
            Err(Rejection::ReservedDeviceName),
            "expected {name:?} to be a reserved device name"
        );
    }
}

#[test]
fn reserved_device_name_check_applies_to_the_stem_before_the_first_dot() {
    assert_eq!(
        title::validate("NUL.backup"),
        Err(Rejection::ReservedDeviceName)
    );
}

#[test]
fn near_misses_of_reserved_device_names_are_accepted() {
    for name in ["COM0", "LPT0", "CONSOLE", "NULL"] {
        assert!(
            title::validate(name).is_ok(),
            "expected {name:?} to be accepted, not treated as reserved"
        );
    }
}

#[test]
fn blank_titles_are_empty_after_trimming() {
    for raw in ["", "   ", "...", " . "] {
        assert_eq!(
            title::validate(raw),
            Err(Rejection::EmptyTitle),
            "input {raw:?} should be EmptyTitle"
        );
    }
}

#[test]
fn trailing_space_is_stripped() {
    assert_eq!(&*title::validate("Review ").unwrap(), "Review");
}

#[test]
fn trailing_dots_are_stripped() {
    assert_eq!(&*title::validate("Review...").unwrap(), "Review");
}

#[test]
fn leading_whitespace_is_trimmed() {
    assert_eq!(&*title::validate("   Review").unwrap(), "Review");
}

#[test]
fn nfr3_unusual_unicode_is_accepted_without_panic() {
    assert!(title::validate("Meeting \u{1F600} notes").is_ok());
    assert!(title::validate("Meeting \u{200f} notes").is_ok());
    let long_title = "a".repeat(30_000);
    assert!(title::validate(&long_title).is_ok());
}

#[test]
fn trimming_never_alters_interior_characters() {
    // Only leading/trailing whitespace and trailing dots are ever removed;
    // an interior dot or space is preserved verbatim.
    assert_eq!(&*title::validate("Q3.5 review").unwrap(), "Q3.5 review");
    assert_eq!(
        &*title::validate("  Weekly  sync  ").unwrap(),
        "Weekly  sync"
    );
}
