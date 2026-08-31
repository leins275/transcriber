//! Path containment (FR-14), the 260-character cap (NFR-4), and vault name
//! shaping (FR-8, FR-10, FR-11).
//!
//! Owned by T5. Lexical rejection of `..`, absolute, drive-relative,
//! `\\?\`, UNC and separator-bearing components happens here, before any
//! directory is created. The only filesystem call permitted in this module
//! is canonicalizing the already-existing vault root.
//!
//! ## Why unsorted repairs while sorted rejects (R6)
//!
//! FR-6 says a sorted title is rejected outright when it cannot be used
//! verbatim — sanitization must never silently rewrite a title into a
//! different meaning. FR-10 says every media file must land somewhere,
//! with no exception. These two rules collide for a badly named file whose
//! stem contains a character Windows forbids: there is no unsorted
//! rejection path left to route it to, since it is *already* the unsorted
//! path. So the private stem sanitizer repairs rather than rejects: illegal
//! and control characters become `_`, trailing dots and spaces are
//! trimmed, and an empty or all-illegal result falls back to a fixed
//! generic name (`recording`) rather than either failing the ingest or
//! producing a folder name made entirely of underscores that would convey
//! no information at all.

use std::path::{Path, PathBuf};

use chrono::NaiveDate;

use crate::date::format_yymmdd;
use crate::error::{IoFailure, VaultError};

/// The Windows path length limit checked against a full absolute
/// destination, including the `source.<ext>` leaf (NFR-4, R9).
pub const MAX_PATH_LEN: usize = 260;

/// The maximum number of characters kept from an unsorted stem before the
/// generic-name fallback and the 260-character destination cap take over.
const MAX_UNSORTED_STEM_CHARS: usize = 120;

/// Characters Windows forbids in a path component, mirroring `title.rs`'s
/// rule — kept as a private copy here since the unsorted shaper repairs
/// these characters rather than rejecting on them (see the module docs).
const ILLEGAL_CHARS: [char; 9] = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];

/// The reserved `source` file stem (without extension) inside a meeting
/// folder (FR-15).
pub const SOURCE_STEM: &str = "source";

/// The reserved `transcript.json` file name F2 writes into a meeting
/// folder (FR-15).
pub const TRANSCRIPT_FILE_NAME: &str = "transcript.json";

/// The reserved `summary.md` file name — a placeholder only; nothing in
/// this crate creates or writes it (FR-15, out of scope).
pub const SUMMARY_FILE_NAME: &str = "summary.md";

/// The reserved `note.md` file name — the user's own markdown note for a
/// meeting, written by the app's note editor. A placeholder here like
/// [`SUMMARY_FILE_NAME`]: nothing in this crate creates or writes it, and
/// `list_meetings` never recurses far enough to need a listing exclusion.
/// The exact string is a cross-language contract with the Python service's
/// `artifacts.py`.
pub const NOTE_FILE_NAME: &str = "note.md";

/// The reserved `unsorted` directory name at the vault root (FR-10,
/// FR-15).
pub const UNSORTED_DIR_NAME: &str = "unsorted";

/// Reserved directory *inside a meeting folder* that used to hold extracted
/// action items (`<meeting>/action items/<slug>/`). The extraction job was
/// retired — the summary carries the action items as a section now — so
/// nothing writes or reads this tree any more, but the name stays reserved
/// so existing folders keep living untouched beside the meeting's other
/// materials for external tools. The exact string remains a cross-language
/// contract with the Python service's legacy trees.
///
/// Like [`EXPORTS_DIR_NAME`], the meeting-level directory needs no listing
/// exclusion: `list_meetings` never recurses into a meeting folder. The
/// name still appears in [`RESERVED_PROJECT_DIR_NAMES`] for the *legacy*
/// project-level tree — see that constant.
pub const ACTION_ITEMS_DIR_NAME: &str = "action items";

/// Reserved directory *inside a meeting folder* that used to hold extracted
/// facts and answered questions (`<meeting>/facts/<slug>/`). The facts
/// extraction job was retired — the summary carries the notable facts now —
/// so nothing writes or reads this tree any more, but the name stays
/// reserved (same contract, anchor and listing rationale as
/// [`ACTION_ITEMS_DIR_NAME`]) so existing folders keep living untouched
/// beside the meeting's other materials for external tools.
pub const FACTS_DIR_NAME: &str = "facts";

/// Reserved project-level directory for dated project-essence reports
/// (`<PROJECT>/reports/<YYMMDD>/`) — still anchored at the project folder,
/// unlike the two per-meeting item kinds above. Same cross-language
/// contract on the exact string as [`ACTION_ITEMS_DIR_NAME`].
pub const REPORTS_DIR_NAME: &str = "reports";

/// Reserved directory *inside a meeting folder* that used to hold dated
/// per-recording exports (`<meeting>/exports/<YYMMDD>/`). Exports now land
/// in the meeting folder itself (`export.md` plus the share-named PDF,
/// overwritten in place), so nothing writes this tree any more; the name
/// stays reserved for the legacy folders. `list_meetings` never recurses
/// into meeting folders, so it needs no listing exclusion.
pub const EXPORTS_DIR_NAME: &str = "exports";

/// Reserved project-level directory for saved chat conversations
/// (`<PROJECT>/chats/<id>.json`) — the app's project chat persists its
/// history here, beside the project's meetings, per the redesign's "stored
/// in the project itself" rule. Same cross-language contract on the exact
/// string as [`ACTION_ITEMS_DIR_NAME`].
pub const CHATS_DIR_NAME: &str = "chats";

/// Every project-level directory name that is *not* a meeting and must be
/// skipped by the vault listing.
///
/// Two of these — `action items` and `facts` — are *legacy* at the
/// project level (and at the meeting level too, since both extraction jobs
/// were retired — see [`ACTION_ITEMS_DIR_NAME`]): nothing reads the
/// project-level trees any more. They are never migrated or deleted
/// either, so an existing vault keeps them on disk for external tools, and
/// the listing must go on skipping them or they would surface as bogus,
/// undated meetings. `reports` and `chats` are not legacy: both are still
/// written at the project level.
pub const RESERVED_PROJECT_DIR_NAMES: [&str; 4] = [
    ACTION_ITEMS_DIR_NAME,
    FACTS_DIR_NAME,
    REPORTS_DIR_NAME,
    CHATS_DIR_NAME,
];

/// Joins `components` onto the canonicalized `root`, rejecting the whole
/// call with [`VaultError::PathEscapesVault`] if any component could
/// smuggle the result outside the vault (FR-14).
///
/// A component is rejected if it is empty, `.`, `..`, or contains `/`,
/// `\` or `:` — which also catches drive-relative paths (`C:`), UNC
/// prefixes (`\\server\share`) and the `\\?\` device-path prefix, since
/// all of them require one of those characters. No filesystem mutation
/// happens here; the only I/O is canonicalizing the already-existing
/// root, and canonicalization runs *after* every component has already
/// passed the lexical check, so a rejecting call creates nothing.
///
/// The returned path is built from the *canonicalized* root, which on
/// Windows carries the extended-length `\\?\` device prefix. Callers on
/// the crate's public boundary (`Vault::root`, `Ingested::meeting_dir`,
/// `Ingested::source_path`) must not hand that prefix to F2/F3 (FR-13); see
/// `simplify_extended_prefix` (private to this module), applied once at
/// the vault root in `Vault::open` and again to the resolved project
/// directory in `Vault::ensure_project_dir`, so every path built by
/// joining onto either one downstream stays plain.
pub fn contained_child(root: &Path, components: &[&str]) -> Result<PathBuf, VaultError> {
    for component in components {
        if !is_safe_component(component) {
            return Err(VaultError::PathEscapesVault);
        }
    }

    let canonical_root = root.canonicalize().map_err(|e| VaultError::Io {
        path: root.to_path_buf(),
        source: IoFailure::from(&e),
    })?;

    let mut joined = canonical_root.clone();
    for component in components {
        joined.push(component);
    }

    if !joined.starts_with(&canonical_root) {
        return Err(VaultError::PathEscapesVault);
    }

    Ok(joined)
}

/// Strips a Windows extended-length `\\?\` device prefix from an
/// already-canonicalized path, when the remainder is a plain drive path
/// (`\\?\C:\...` becomes `C:\...`) (FR-13).
///
/// `std::fs::canonicalize` always returns the extended-length form on
/// Windows. That form suppresses Win32 path normalization and is rejected
/// or mangled by a large fraction of downstream tooling (ffmpeg, many
/// Python path-manipulating idioms, anything that round-trips through
/// `os.path.normpath` or a shell) — and it is unpresentable in a UI. F3
/// passes the meeting-folder path straight to F2 as an argument, so every
/// path this crate hands back to a caller (`Vault::root`,
/// `Ingested::meeting_dir`, `Ingested::source_path`) must be the plain
/// form.
///
/// Left unchanged (returned as-is) for anything that is not a plain
/// `<drive>:\...` path once the prefix is stripped — in particular a
/// `\\?\UNC\server\share\...` extended UNC path, whose simplified form is
/// `\\server\share\...` rather than a bare prefix strip. This crate's vault
/// roots are always local drive paths (FR-13's caller-supplied root), so
/// that case does not arise in practice; refusing to guess at it is safer
/// than mangling an unfamiliar shape.
pub(crate) fn simplify_extended_prefix(path: PathBuf) -> PathBuf {
    const PREFIX: &str = r"\\?\";

    let Some(s) = path.to_str() else {
        return path;
    };
    let Some(rest) = s.strip_prefix(PREFIX) else {
        return path;
    };

    if is_plain_drive_path(rest) {
        PathBuf::from(rest)
    } else {
        path
    }
}

/// Whether `s` starts with a plain drive-letter path component, i.e.
/// `<letter>:\` (as opposed to a UNC share or some other device form).
fn is_plain_drive_path(s: &str) -> bool {
    let bytes = s.as_bytes();
    bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\'
}

fn is_safe_component(component: &str) -> bool {
    !component.is_empty()
        && component != "."
        && component != ".."
        && !component.contains('/')
        && !component.contains('\\')
        && !component.contains(':')
}

/// Checks a full absolute destination path — including the `source.<ext>`
/// leaf — against the Windows 260-character limit (NFR-4).
///
/// Must be called with the complete destination, not just the directory
/// component: the budget shrinks with the vault root's own depth, so
/// checking only the relative part would let a deep root turn a passing
/// test into a runtime OS error (R9).
pub fn check_len(full_destination: &Path) -> Result<(), VaultError> {
    let len = full_destination
        .as_os_str()
        .to_string_lossy()
        .chars()
        .count();
    if len > MAX_PATH_LEN {
        Err(VaultError::PathTooLong {
            len,
            limit: MAX_PATH_LEN,
        })
    } else {
        Ok(())
    }
}

/// Builds a sorted meeting folder name: `<date> - <title>` (FR-8).
///
/// `date` is the verbatim six-character `YYMMDD` string — never
/// reformatted — and `title` is expected to already be a
/// [`crate::title::ValidTitle`]'s content, since FR-6 rejects (rather than
/// repairs) a sorted title that cannot be used verbatim.
pub fn meeting_folder_name(date: &str, title: &str) -> String {
    format!("{date} - {title}")
}

/// Builds an unsorted meeting folder name: `<date of ingest> - <stem>`
/// (FR-10), sanitizing the original filename stem so it can never fail to
/// produce a usable name (R6, see the module docs).
pub fn unsorted_folder_name(ingested_on: NaiveDate, stem: &str) -> String {
    format!(
        "{} - {}",
        format_yymmdd(ingested_on),
        sanitize_unsorted_stem(stem)
    )
}

/// Sanitizes an arbitrary filename stem for use in an unsorted folder name.
///
/// Every Windows-illegal or control character becomes `_`; the result is
/// then trimmed of leading whitespace and trailing dots/spaces; if what
/// remains is empty or made entirely of underscores (the stem carried no
/// usable information at all), it falls back to the fixed name
/// `recording`. Finally the result is capped at
/// [`MAX_UNSORTED_STEM_CHARS`] characters, truncated on a `char` boundary
/// so a multi-byte character is never split.
fn sanitize_unsorted_stem(raw: &str) -> String {
    let substituted: String = raw
        .chars()
        .map(|c| if is_illegal(c) { '_' } else { c })
        .collect();

    let trimmed = substituted.trim_start().trim_end_matches(['.', ' ']);

    let candidate = if trimmed.is_empty() || trimmed.chars().all(|c| c == '_') {
        "recording"
    } else {
        trimmed
    };

    candidate.chars().take(MAX_UNSORTED_STEM_CHARS).collect()
}

fn is_illegal(c: char) -> bool {
    ILLEGAL_CHARS.contains(&c) || c.is_control()
}

/// Appends a numeric collision suffix to a base folder name:
/// `<base_name> (<n>)` (FR-11).
pub fn suffixed(base_name: &str, n: u32) -> String {
    format!("{base_name} ({n})")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The artifact directory names are a cross-language contract, mirrored
    /// verbatim in `services/transcription/src/transcription/artifacts.py`
    /// and recognizable to the operator's external tools over the synced
    /// meeting folders. Moving the *anchor* from the project folder to the
    /// meeting folder deliberately left the strings alone; this test is the
    /// Rust half of the pin, so a rename can never half-land across the
    /// language boundary.
    #[test]
    fn artifact_directory_names_are_the_pinned_cross_language_strings() {
        assert_eq!(ACTION_ITEMS_DIR_NAME, "action items");
        assert_eq!(FACTS_DIR_NAME, "facts");
        assert_eq!(REPORTS_DIR_NAME, "reports");
        assert_eq!(EXPORTS_DIR_NAME, "exports");
    }

    /// The listing exclusion covers the *legacy* project-level trees, which
    /// stay on disk unread after the meeting-level move (FR-5, FR-6): a
    /// vault still holding `<PROJECT>/action items/` must not surface it as
    /// a meeting. Meeting-level artifact dirs need no entry here — the
    /// listing never recurses into a meeting folder, the same reason
    /// `exports` is absent from this array.
    #[test]
    fn reserved_project_dir_names_cover_the_legacy_trees_but_not_exports() {
        assert_eq!(
            RESERVED_PROJECT_DIR_NAMES,
            ["action items", "facts", "reports", "chats"]
        );
        assert!(!RESERVED_PROJECT_DIR_NAMES.contains(&EXPORTS_DIR_NAME));
    }
}
