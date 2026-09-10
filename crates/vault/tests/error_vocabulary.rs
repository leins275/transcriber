//! NFR-5: every rejection reason is a distinct, specifically-worded
//! enumerated variant. This file is the traceability test for that
//! requirement, exercised against `vault::error` and the type validator
//! in `vault::title` that produces three of its new variants (FR-2).

use vault::error::{Rejection, VaultError};
use vault::title;

fn assert_debug_clone_partial_eq<T: std::fmt::Debug + Clone + PartialEq>() {}
fn assert_error<T: std::error::Error>() {}

fn display_strings<T: std::fmt::Display>(items: &[T]) -> Vec<String> {
    items.iter().map(|i| i.to_string()).collect()
}

fn assert_pairwise_distinct(strings: &[String]) {
    for i in 0..strings.len() {
        for j in (i + 1)..strings.len() {
            assert_ne!(
                strings[i], strings[j],
                "Display strings at indices {i} and {j} collide: {:?}",
                strings[i]
            );
        }
    }
}

#[test]
fn rejection_all_covers_every_variant() {
    // 9 variants declared in the plan's error vocabulary (the tenth,
    // `TitleEscapesVault`, was removed as dead code — no code path could
    // ever construct it, see `Rejection`'s doc comment), plus the four
    // added for the meeting type: `TooManySeparators`, `EmptyType`,
    // `IllegalTypeCharacter` and `ReservedSeparator` (FR-2). This length
    // check is a coarse signal; `error::exhaustiveness` (a `#[cfg(test)]`
    // compiler-enforced match in `src/error.rs`) is what actually fails
    // the build if a variant is ever added without updating `all()`.
    assert_eq!(Rejection::all().len(), 13);
}

#[test]
fn rejection_display_strings_are_pairwise_distinct() {
    let strings = display_strings(&Rejection::all());
    assert_pairwise_distinct(&strings);
}

#[test]
fn date_not_a_calendar_date_message_mentions_the_calendar() {
    let message = Rejection::DateNotACalendarDate.to_string();
    assert!(
        message.to_lowercase().contains("calendar"),
        "message was {message:?}"
    );
    assert!(
        !message.to_lowercase().contains("invalid filename"),
        "message was {message:?}"
    );
}

#[test]
fn vault_error_all_kinds_covers_every_variant() {
    // 9 variants declared in the plan's error vocabulary, plus the three
    // `crate::manage` added for post-ingest rename/re-file/delete. See
    // `error::exhaustiveness` in `src/error.rs` for the compile-time guard
    // that backs this count up.
    assert_eq!(VaultError::all_kinds().len(), 12);
}

#[test]
fn vault_error_display_strings_are_pairwise_distinct() {
    let strings = display_strings(&VaultError::all_kinds());
    assert_pairwise_distinct(&strings);
}

#[test]
fn rejection_and_vault_error_satisfy_required_traits() {
    assert_debug_clone_partial_eq::<Rejection>();
    assert_debug_clone_partial_eq::<VaultError>();
    assert_error::<VaultError>();
}

fn assert_copy<T: Copy>() {}

#[test]
fn rejection_is_cheap_to_clone() {
    // `Rejection` goes further than "cheap to clone" and is actually `Copy`.
    assert_copy::<Rejection>();
    let original = Rejection::IllegalTitleCharacter(':');
    let copied = original;
    assert_eq!(original, copied);
}

#[test]
fn rejection_variants_carry_data_where_needed() {
    let a = Rejection::IllegalTitleCharacter(':');
    let b = Rejection::IllegalTitleCharacter('?');
    assert_ne!(a.to_string(), b.to_string());
}

#[test]
fn vault_error_unsupported_media_type_message_names_the_extension() {
    let err = VaultError::UnsupportedMediaType {
        ext: "exe".to_string(),
    };
    assert!(err.to_string().contains("exe"));
}

#[test]
fn vault_error_path_too_long_message_names_len_and_limit() {
    let err = VaultError::PathTooLong {
        len: 300,
        limit: 260,
    };
    let message = err.to_string();
    assert!(message.contains("300"));
    assert!(message.contains("260"));
}

// FR-2: the four variants added for the optional fourth filename section
// (the meeting type), and the type validator that produces three of them.

#[test]
fn too_many_separators_message_names_the_separator_and_the_section_rule() {
    let message = Rejection::TooManySeparators.to_string().to_lowercase();
    assert!(message.contains('-'), "message was {message:?}");
    assert!(message.contains("section"), "message was {message:?}");
}

#[test]
fn empty_type_message_names_the_type_and_says_it_is_empty() {
    let message = Rejection::EmptyType.to_string().to_lowercase();
    assert!(message.contains("type"), "message was {message:?}");
    assert!(message.contains("empty"), "message was {message:?}");
}

#[test]
fn illegal_type_character_message_names_the_type_and_the_offending_char() {
    let message = Rejection::IllegalTypeCharacter(':')
        .to_string()
        .to_lowercase();
    assert!(message.contains("type"), "message was {message:?}");
    assert!(message.contains(':'), "message was {message:?}");
}

#[test]
fn an_illegal_character_in_the_type_reads_differently_from_one_in_the_title() {
    assert_ne!(
        Rejection::IllegalTypeCharacter(':').to_string(),
        Rejection::IllegalTitleCharacter(':').to_string()
    );
}

#[test]
fn reserved_separator_message_says_the_hyphen_is_reserved() {
    let message = Rejection::ReservedSeparator.to_string().to_lowercase();
    assert!(message.contains("reserved"), "message was {message:?}");
    assert!(message.contains('-'), "message was {message:?}");
}

#[test]
fn a_plain_type_is_accepted_verbatim() {
    assert_eq!(&*title::validate_kind("Standup").unwrap(), "Standup");
}

#[test]
fn surrounding_spaces_are_trimmed_from_a_type() {
    assert_eq!(&*title::validate_kind(" Retro ").unwrap(), "Retro");
}

#[test]
fn an_empty_type_is_rejected_as_an_empty_type_not_an_empty_title() {
    assert_eq!(title::validate_kind(""), Err(Rejection::EmptyType));
}

#[test]
fn a_whitespace_only_type_is_rejected_as_an_empty_type() {
    assert_eq!(title::validate_kind("   "), Err(Rejection::EmptyType));
}

#[test]
fn an_illegal_character_in_a_type_is_reported_against_the_type() {
    assert_eq!(
        title::validate_kind("Q:A"),
        Err(Rejection::IllegalTypeCharacter(':'))
    );
}

#[test]
fn a_type_matching_a_reserved_device_name_is_rejected() {
    assert_eq!(
        title::validate_kind("CON"),
        Err(Rejection::ReservedDeviceName)
    );
}

#[test]
fn a_hyphen_inside_a_type_is_not_this_validators_concern() {
    // The parser can never hand a section containing `-` to this
    // validator, and an operator-supplied one is refused earlier in
    // `manage::rename_meeting` with `ReservedSeparator` (FR-2, FR-6).
    assert_eq!(
        &*title::validate_kind("Q3 - review").unwrap(),
        "Q3 - review"
    );
}
