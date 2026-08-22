//! Project code pattern (FR-4) and the reserved `unsorted` word (FR-15).
//!
//! Owned by T4. Hand-rolled `^[A-Za-z][A-Za-z0-9]{1,9}$` matching over
//! `char`s — no `regex` dependency. Pure — no filesystem, no path types.
//!
//! ## Case (supersedes R4)
//!
//! The original resolution R4 read FR-4's `^[A-Z][A-Z0-9]{1,9}$` as
//! uppercase-only, so `els - 260812 - Title.mp4` was classified *unsorted*
//! rather than filed under `ELS/`. The operator has since resolved that the
//! project is decoded from the filename and *always capitalized*: matching
//! is now case-insensitive and [`validate`] normalizes the accepted code to
//! uppercase, which is what the crate-level docs always described
//! ("case-normalized to uppercase"). Folder reuse was already
//! case-insensitive (`layout::ensure_project_dir`), so an existing `ELS/`
//! folder is reused by a lowercase drop rather than a second folder being
//! created beside it.

use crate::error::Rejection;

/// The reserved project-code word that can never be used as a real project
/// (FR-15). Compared case-insensitively.
const RESERVED_WORD: &str = "unsorted";

/// A project code that has passed [`validate`], normalized to uppercase.
///
/// Normalization is load-bearing: the pattern is matched case-insensitively
/// (see the module docs), so `els`, `Els` and `ELS` all validate and all
/// yield the same `ELS` code.
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

/// Validates a raw project code against `^[A-Za-z][A-Za-z0-9]{1,9}$`
/// case-insensitively (superseding R4 — see the module docs) and returns it
/// uppercased, applying the `unsorted` reserved-word check regardless of the
/// pattern outcome so that `UNSORTED` maps to
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

/// Hand-rolled match against `^[A-Za-z][A-Za-z0-9]{1,9}$` over `char`s.
///
/// Length must be 2 to 10 chars: one leading ASCII letter of either case,
/// followed by one to nine ASCII letters/digits. Case is folded by
/// [`validate`]'s uppercasing, not by this predicate.
fn matches_pattern(raw: &str) -> bool {
    let mut chars = raw.chars();

    let Some(first) = chars.next() else {
        return false;
    };
    if !first.is_ascii_alphabetic() {
        return false;
    }

    let rest: Vec<char> = chars.collect();
    if rest.is_empty() || rest.len() > 9 {
        return false;
    }

    rest.iter().all(|c| c.is_ascii_alphanumeric())
}
