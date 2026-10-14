//! Title rules — Windows-illegal characters, trimming, reserved device names.
//!
//! Owned by T3. A title is either usable verbatim or rejected outright
//! (FR-6): this module never repairs a sorted title, it only reports why
//! one cannot be used. Pure — no filesystem, no path types.
//!
//! Sanitization never silently rewrites a title into a different meaning:
//! the only characters ever removed are leading/trailing whitespace and
//! trailing dots (Windows itself strips these from a path component), and
//! only *after* the raw string has already been scanned for illegal and
//! control characters — so a character that would otherwise be silently
//! trimmed away (for example a trailing tab, which is both a control
//! character and Unicode whitespace) is still caught and reported rather
//! than disappearing unnoticed.

use crate::error::Rejection;

/// Characters Windows forbids in a path component.
const ILLEGAL_CHARS: [char; 9] = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];

/// Windows reserved device names (case-insensitive, checked against the
/// stem before the first `.`).
const RESERVED_EXACT: [&str; 4] = ["CON", "PRN", "AUX", "NUL"];

/// A title that has passed [`validate`] and is safe to use verbatim in a
/// meeting folder name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidTitle(String);

impl std::ops::Deref for ValidTitle {
    type Target = str;

    fn deref(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for ValidTitle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Validates a raw title component (FR-6).
///
/// Trims leading/trailing whitespace and trailing dots, rejects an empty
/// result, rejects any Windows-illegal or control character (checked
/// before trimming so one hiding in whitespace that would otherwise be
/// trimmed away is never silently lost), and rejects a title whose stem
/// (the part before the first `.`) matches a reserved Windows device name.
pub fn validate(raw: &str) -> Result<ValidTitle, Rejection> {
    if let Some(c) = first_illegal_char(raw) {
        return Err(Rejection::IllegalTitleCharacter(c));
    }

    let trimmed = raw.trim_start();
    let trimmed = trimmed.trim_end_matches(['.', ' ']);

    if let Some(c) = first_illegal_char(trimmed) {
        return Err(Rejection::IllegalTitleCharacter(c));
    }

    if trimmed.is_empty() {
        return Err(Rejection::EmptyTitle);
    }

    let stem = trimmed.split('.').next().unwrap_or(trimmed);
    if is_reserved_device_name(stem) {
        return Err(Rejection::ReservedDeviceName);
    }

    Ok(ValidTitle(trimmed.to_string()))
}

fn first_illegal_char(s: &str) -> Option<char> {
    s.chars()
        .find(|c| ILLEGAL_CHARS.contains(c) || c.is_control())
}

fn is_reserved_device_name(stem: &str) -> bool {
    let upper = stem.to_ascii_uppercase();
    RESERVED_EXACT.contains(&upper.as_str())
        || is_numbered_reserved(&upper, "COM")
        || is_numbered_reserved(&upper, "LPT")
}

fn is_numbered_reserved(upper: &str, prefix: &str) -> bool {
    match upper.strip_prefix(prefix) {
        Some(rest) => {
            let mut chars = rest.chars();
            match (chars.next(), chars.next()) {
                (Some(digit), None) => ('1'..='9').contains(&digit),
                _ => false,
            }
        }
        None => false,
    }
}
