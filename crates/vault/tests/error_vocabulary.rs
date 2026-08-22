//! NFR-5: every rejection reason is a distinct, specifically-worded
//! enumerated variant. This file is the traceability test for that
//! requirement, exercised against `vault::error` only.

use vault::error::{Rejection, VaultError};

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
    // ever construct it, see `Rejection`'s doc comment). This length check
    // is a coarse signal; `error::exhaustiveness` (a `#[cfg(test)]`
    // compiler-enforced match in `src/error.rs`) is what actually fails
    // the build if a variant is ever added without updating `all()`.
    assert_eq!(Rejection::all().len(), 9);
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
