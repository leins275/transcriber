//! Project code pattern (FR-4) and the reserved `unsorted` word (FR-15).
//!
//! Owned by T4. Hand-rolled `^[A-Z][A-Z0-9]{1,9}$` matching over `char`s —
//! no `regex` dependency. Pure — no filesystem, no path types.

use crate::error::Rejection;

/// The reserved project-code word that can never be used as a real project
/// (FR-15). Compared case-insensitively.
const RESERVED_WORD: &str = "unsorted";

/// A project code that has passed [`validate`], normalized to uppercase.
///
/// Since R4 resolves FR-4's pattern as uppercase-only, normalization here is
/// defensive and a no-op in practice — the raw input already matched
/// `^[A-Z][A-Z0-9]{1,9}$`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectCode(String);

impl ProjectCode {
    /// The normalized (uppercase) project code.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::ops::Deref for ProjectCode {
    type Target = str;

    fn deref(&self) -> &str {
        &self.0
    }
}

/// Validates a raw project code against `^[A-Z][A-Z0-9]{1,9}$` (R4: the
/// pattern is uppercase-only — a lowercase code is a rejection, not
/// something normalization repairs), applying the `unsorted` reserved-word
/// check regardless of the pattern outcome so that `UNSORTED` maps to
/// [`Rejection::ReservedProjectCode`] rather than a generic pattern failure.
pub fn validate(raw: &str) -> Result<ProjectCode, Rejection> {
    if raw.eq_ignore_ascii_case(RESERVED_WORD) {
        return Err(Rejection::ReservedProjectCode);
    }

    if raw.is_empty() {
        return Err(Rejection::EmptyProjectCode);
    }

    if !matches_pattern(raw) {
        return Err(Rejection::InvalidProjectCode);
    }

    Ok(ProjectCode(raw.to_ascii_uppercase()))
}

/// Hand-rolled match against `^[A-Z][A-Z0-9]{1,9}$` over `char`s.
///
/// Length must be 2 to 10 chars: one leading uppercase letter, followed by
/// one to nine uppercase letters/digits.
fn matches_pattern(raw: &str) -> bool {
    let mut chars = raw.chars();

    let Some(first) = chars.next() else {
        return false;
    };
    if !first.is_ascii_uppercase() {
        return false;
    }

    let rest: Vec<char> = chars.collect();
    if rest.is_empty() || rest.len() > 9 {
        return false;
    }

    rest.iter()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}
