//! Media extension allowlist (FR-7).
//!
//! Owned by T4. Only the ten recording extensions in the convention are
//! accepted for ingest; anything else is an error, not an unsorted
//! classification. Pure — no filesystem, no path types.

use crate::error::VaultError;

/// The ten recording media extensions this crate accepts, lowercase.
const ALLOWED: [&str; 10] = [
    "mp4", "mkv", "mov", "webm", "avi", "m4a", "mp3", "wav", "flac", "ogg",
];

/// The five of them that are video containers; the other five only ever
/// hold audio. A stage that only makes sense for video (the chain's
/// compress stage) asks here rather than keeping its own list.
const VIDEO: [&str; 5] = ["mp4", "mkv", "mov", "webm", "avi"];

/// Whether `ext` -- with or without a leading dot, in any case -- is one of
/// the video container extensions. Pure; an unknown extension is simply
/// not video.
pub fn is_video_extension(ext: &str) -> bool {
    let lower = ext.trim_start_matches('.').to_ascii_lowercase();
    VIDEO.contains(&lower.as_str())
}

/// A validated, lowercase-normalized media extension.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaExt(String);

impl MediaExt {
    /// The normalized (lowercase) extension, without a leading dot.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The canonical source file name for this extension: `source.<ext>`.
    pub fn source_file_name(&self) -> String {
        format!("source.{}", self.0)
    }

    /// Whether this extension is a video container (see [`is_video_extension`]).
    pub fn is_video(&self) -> bool {
        is_video_extension(&self.0)
    }
}

/// Splits `name` on its **last** `.`, lowercases the extension and checks it
/// against the recording media allowlist (FR-7).
///
/// A name with no dot at all, an empty extension (a trailing dot with
/// nothing after it), or an extension not in the allowlist all return
/// [`VaultError::UnsupportedMediaType`]. Never panics, even on a name that
/// is only dots.
pub fn from_file_name(name: &str) -> Result<MediaExt, VaultError> {
    let raw_ext = match name.rfind('.') {
        // No dot at all, or the dot is the last character (empty extension).
        Some(idx) if idx + 1 < name.len() => &name[idx + 1..],
        _ => "",
    };

    let lower = raw_ext.to_ascii_lowercase();

    if ALLOWED.contains(&lower.as_str()) {
        Ok(MediaExt(lower))
    } else {
        Err(VaultError::UnsupportedMediaType { ext: lower })
    }
}

/// Returns `name` with its last extension (including the dot) stripped, for
/// use in the unsorted fallback folder name.
///
/// A name with no dot at all is returned unchanged.
pub fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(idx) => &name[..idx],
        None => name,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_five_video_containers_are_video() {
        for ext in ["mp4", "mkv", "mov", "webm", "avi"] {
            assert!(is_video_extension(ext), "{ext} must be video");
            assert!(from_file_name(&format!("x.{ext}")).unwrap().is_video());
        }
    }

    #[test]
    fn the_audio_only_extensions_are_not_video() {
        for ext in ["m4a", "mp3", "wav", "flac", "ogg", "", "txt"] {
            assert!(!is_video_extension(ext), "{ext} must not be video");
        }
        assert!(!from_file_name("x.wav").unwrap().is_video());
    }

    #[test]
    fn is_video_extension_ignores_case_and_a_leading_dot() {
        assert!(is_video_extension(".MP4"));
        assert!(is_video_extension("Mkv"));
        assert!(!is_video_extension(".M4A"));
    }
}
