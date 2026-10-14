//! Filename parser — the pure classification entry point (FR-2, FR-3).
//!
//! Owned by T8. `classify_filename` runs the extension gate, then splits
//! the stem on *every* `-` (whitespace around them optional) and validates
//! project code, date, title and — when a fourth section is present — the
//! comma-separated tag list, in that order, mapping the first failure into
//! an unsorted classification. Zero filesystem access in this module.
//!
//! `-` is reserved as the separator everywhere, so the number of sections
//! is itself part of the grammar: three sections are an untagged meeting,
//! four carry tags, fewer than three are a missing separator and five or
//! more mean a section contained a `-` — routed to `unsorted` with
//! [`Rejection::TooManySeparators`], never rejected outright (FR-1).

use crate::error::{Rejection, VaultError};
use crate::{code, date, media, tags, title};

/// A filename that matched the full naming convention (FR-3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedName {
    /// The normalized (uppercase) project code.
    pub project: String,
    /// The verbatim six-character `YYMMDD` date.
    pub date: String,
    /// The validated, trimmed title.
    pub title: String,
    /// The validated, normalized tags — the optional fourth section of the
    /// name, split on `,`; empty when the name had only three sections
    /// (FR-1).
    pub tags: Vec<String>,
    /// The normalized (lowercase) media extension, without a leading dot.
    pub ext: String,
    /// The original filename stem (everything before the extension),
    /// exactly as given.
    pub stem: String,
}

/// The result of classifying a filename against the naming convention.
///
/// A [`VaultError`] (returned outright by [`classify_filename`], not
/// carried in this enum) means the extension itself was rejected and
/// nothing about the file's name was even considered (FR-7). Every other
/// outcome — sorted or unsorted — is represented here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Classified {
    /// The filename fully matched the convention.
    Sorted(ParsedName),
    /// The filename matched the media allowlist but failed some rule of
    /// the naming convention; it is still a valid recording and must land
    /// in `unsorted/` (FR-10).
    Unsorted {
        /// The first rule the filename failed.
        reason: Rejection,
        /// The original filename stem, kept for the unsorted fallback
        /// folder name (FR-10).
        stem: String,
        /// The normalized (lowercase) media extension.
        ext: String,
    },
}

/// Classifies a filename against the naming convention `<Project code> -
/// <date> - <Title>[ - <tag>, <tag>, …].<ext>` (FR-1, FR-2, FR-3).
///
/// The extension is checked first: an unsupported extension aborts with
/// `Err(VaultError::UnsupportedMediaType)` before anything else about the
/// name is considered — the section count included (FR-7) — and that is
/// not a rejection, it means the file is never ingested at all. Everything
/// else — too few or too many separators, a bad project code, a bad date,
/// a bad title, a bad tag — maps to `Ok(Classified::Unsorted { .. })`,
/// because every accepted media file must land somewhere (FR-10). This
/// function performs no filesystem access and never panics (NFR-1, NFR-3).
///
/// Order of judgement after the extension gate: section count → project
/// code → date → title → tags; the first failure wins.
pub fn classify_filename(file_name: &str) -> Result<Classified, VaultError> {
    let media_ext = media::from_file_name(file_name)?;
    let ext = media_ext.as_str().to_string();
    let stem = media::stem(file_name).to_string();

    // `-` is the separator everywhere, so the stem is split on *every*
    // occurrence and the section count decides the shape of the name
    // before any section is validated (FR-1). Whitespace around a
    // separator is decoration -- `ELS - 260812 - Title` and
    // `ELS-260812-Title` both parse -- so each section is trimmed of
    // surrounding spaces. Only spaces: a control character hiding next to
    // a separator (a tab, say) must still reach the validators and be
    // rejected, never silently trimmed away (the same rule
    // `title::validate` applies to trailing whitespace).
    let parts: Vec<&str> = stem.split('-').map(|part| part.trim_matches(' ')).collect();

    let (code_part, date_part, title_part, tags_part) = match parts.as_slice() {
        [code_part, date_part, title_part] => (*code_part, *date_part, *title_part, None),
        [code_part, date_part, title_part, tags_part] => {
            (*code_part, *date_part, *title_part, Some(*tags_part))
        }
        // Fewer than three sections: the name never matched the
        // convention at all. Five or more: a section contained the
        // reserved separator, so the name cannot be routed (FR-1).
        parts => {
            let reason = if parts.len() < 3 {
                Rejection::MissingSeparator
            } else {
                Rejection::TooManySeparators
            };
            return Ok(Classified::Unsorted { reason, stem, ext });
        }
    };

    let project = match code::validate(code_part) {
        Ok(project) => project,
        Err(reason) => return Ok(Classified::Unsorted { reason, stem, ext }),
    };

    let valid_date = match date::validate(date_part) {
        Ok(valid_date) => valid_date,
        Err(reason) => return Ok(Classified::Unsorted { reason, stem, ext }),
    };

    let valid_title = match title::validate(title_part) {
        Ok(valid_title) => valid_title,
        Err(reason) => return Ok(Classified::Unsorted { reason, stem, ext }),
    };

    let valid_tags = match tags_part.map(tags::validate).transpose() {
        Ok(valid_tags) => valid_tags.unwrap_or_default(),
        Err(reason) => return Ok(Classified::Unsorted { reason, stem, ext }),
    };

    Ok(Classified::Sorted(ParsedName {
        project: project.as_str().to_string(),
        date: valid_date.as_str().to_string(),
        title: valid_title.to_string(),
        tags: valid_tags,
        ext,
        stem,
    }))
}
