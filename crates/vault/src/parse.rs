//! Filename parser — the pure classification entry point (FR-2, FR-3).
//!
//! Owned by T8. `classify_filename` runs the extension gate, then splits
//! the stem on the first two `" - "` separators and validates project
//! code, date and title in order, mapping the first failure into an
//! unsorted classification. Zero filesystem access in this module.

use crate::error::{Rejection, VaultError};
use crate::{code, date, media, title};

/// A filename that matched the full naming convention (FR-3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedName {
    /// The normalized (uppercase) project code.
    pub project: String,
    /// The verbatim six-character `YYMMDD` date.
    pub date: String,
    /// The validated, trimmed title.
    pub title: String,
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
/// <date> - <Title>.<ext>` (FR-2, FR-3).
///
/// The extension is checked first: an unsupported extension aborts with
/// `Err(VaultError::UnsupportedMediaType)` before anything else about the
/// name is considered (FR-7) — that is not a rejection, it means the file
/// is never ingested at all. Everything else — a missing separator, a bad
/// project code, a bad date, a bad title — maps to
/// `Ok(Classified::Unsorted { .. })`, because every accepted media file
/// must land somewhere (FR-10). This function performs no filesystem
/// access and never panics (NFR-1, NFR-3).
pub fn classify_filename(file_name: &str) -> Result<Classified, VaultError> {
    let media_ext = media::from_file_name(file_name)?;
    let ext = media_ext.as_str().to_string();
    let stem = media::stem(file_name).to_string();

    // Split on the first two occurrences of " - " only, so a title may
    // itself contain the separator (FR-3).
    let mut parts = stem.splitn(3, " - ");
    let code_part = parts.next().unwrap_or("");
    let date_part = parts.next();
    let title_part = parts.next();

    let (date_part, title_part) = match (date_part, title_part) {
        (Some(d), Some(t)) => (d, t),
        _ => {
            return Ok(Classified::Unsorted {
                reason: Rejection::MissingSeparator,
                stem,
                ext,
            })
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

    Ok(Classified::Sorted(ParsedName {
        project: project.as_str().to_string(),
        date: valid_date.as_str().to_string(),
        title: valid_title.to_string(),
        ext,
        stem,
    }))
}
