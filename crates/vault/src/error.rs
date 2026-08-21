//! `VaultError` and `Rejection` — the crate's full error vocabulary (NFR-5).
//!
//! `VaultError` aborts an ingest outright (nothing landed anywhere).
//! `Rejection` routes a recording to `unsorted/` instead — every rejected
//! filename is still a media file that must land somewhere (FR-10).
//!
//! Both enums are `Debug + Clone + PartialEq` so F3 can carry them across
//! the Tauri IPC boundary and compare them in tests; `Rejection` is also
//! `Copy` because every variant's payload (a `char`, or nothing) is
//! trivially cheap. `VaultError` cannot be `Copy` (`String`/`PathBuf`
//! payloads), but stays cheap to clone.
//!
//! `std::io::Error` itself implements neither `Clone` nor `PartialEq`, so
//! [`VaultError::Io`] captures the parts of it that do — see
//! [`crate::error::IoFailure`] — instead of wrapping the original error.

use std::path::PathBuf;

/// A captured `std::io::Error`.
///
/// Retained as plain data (an [`std::io::ErrorKind`] plus the rendered
/// message) rather than the original error, so that [`VaultError`] can
/// stay `Clone + PartialEq`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IoFailure {
    kind: std::io::ErrorKind,
    message: String,
}

impl IoFailure {
    /// The kind of the underlying I/O error.
    pub fn kind(&self) -> std::io::ErrorKind {
        self.kind
    }

    /// The rendered message of the underlying I/O error.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl From<&std::io::Error> for IoFailure {
    fn from(source: &std::io::Error) -> Self {
        IoFailure {
            kind: source.kind(),
            message: source.to_string(),
        }
    }
}

impl std::fmt::Display for IoFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

/// An error that aborts an ingest call outright.
///
/// Every variant here means the call produced no observable effect beyond
/// what it explicitly documents — never a partial write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VaultError {
    /// The configured vault root exists but is not a directory.
    RootIsNotADirectory,
    /// The source path passed to `ingest` does not exist.
    SourceMissing,
    /// The source path passed to `ingest` exists but is not a regular file.
    SourceNotAFile,
    /// The source file's extension is not one of the ten recording media
    /// extensions this crate accepts (FR-7).
    UnsupportedMediaType {
        /// The offending extension, as found on the source file name.
        ext: String,
    },
    /// A computed destination path would escape the vault root (FR-14).
    PathEscapesVault,
    /// The full absolute destination path exceeds the Windows 260-character
    /// limit (NFR-4).
    PathTooLong {
        /// The length that was measured.
        len: usize,
        /// The limit that was exceeded (260).
        limit: usize,
    },
    /// More than 999 numeric collision suffixes were probed for one
    /// destination name (FR-11).
    SuffixLimitExceeded,
    /// The application-data directory could not be resolved (FR-16).
    AppDataUnavailable,
    /// An I/O operation failed at the given path.
    Io {
        /// The path the failing operation was acting on.
        path: PathBuf,
        /// The captured underlying I/O error.
        source: IoFailure,
    },
}

impl VaultError {
    /// One instance of every [`VaultError`] variant, with representative
    /// data for the variants that carry a payload.
    ///
    /// Used to assert, exhaustively, that no two variants render the same
    /// [`std::fmt::Display`] string (NFR-5).
    pub fn all_kinds() -> Vec<VaultError> {
        vec![
            VaultError::RootIsNotADirectory,
            VaultError::SourceMissing,
            VaultError::SourceNotAFile,
            VaultError::UnsupportedMediaType {
                ext: "exe".to_string(),
            },
            VaultError::PathEscapesVault,
            VaultError::PathTooLong {
                len: 300,
                limit: 260,
            },
            VaultError::SuffixLimitExceeded,
            VaultError::AppDataUnavailable,
            VaultError::Io {
                path: PathBuf::from("example"),
                source: IoFailure {
                    kind: std::io::ErrorKind::Other,
                    message: "example I/O failure".to_string(),
                },
            },
        ]
    }
}

impl std::fmt::Display for VaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            VaultError::RootIsNotADirectory => {
                write!(f, "vault root exists but is not a directory")
            }
            VaultError::SourceMissing => write!(f, "source file does not exist"),
            VaultError::SourceNotAFile => {
                write!(f, "source path exists but is not a regular file")
            }
            VaultError::UnsupportedMediaType { ext } => {
                write!(f, "'.{ext}' is not a supported recording media type")
            }
            VaultError::PathEscapesVault => {
                write!(f, "computed destination path escapes the vault root")
            }
            VaultError::PathTooLong { len, limit } => write!(
                f,
                "destination path is {len} characters, exceeding the {limit}-character limit"
            ),
            VaultError::SuffixLimitExceeded => {
                write!(f, "exceeded the maximum number of collision suffixes (999)")
            }
            VaultError::AppDataUnavailable => {
                write!(f, "application-data directory is unavailable")
            }
            VaultError::Io { path, source } => {
                write!(f, "I/O error at \"{}\": {source}", path.display())
            }
        }
    }
}

impl std::error::Error for VaultError {}

/// A reason a recording was routed to `unsorted/` instead of aborting the
/// ingest (FR-10).
///
/// Every variant here still ends with the file safely somewhere in the
/// vault — only [`VaultError`] aborts a call outright.
///
/// There is deliberately no dedicated "title escapes the vault" variant:
/// [`crate::title::validate`] rejects every character a path-shaped title
/// would need (`/`, `\`, a bare `.`/`..` stem) as
/// [`Rejection::IllegalTitleCharacter`] or [`Rejection::EmptyTitle`] before
/// containment is ever considered (FR-14's acceptance bullet is satisfied
/// by that earlier, more specific rejection), so a separate variant that no
/// code path could ever construct would only be dead surface for F3.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rejection {
    /// The filename has fewer than two `" - "` separators (FR-3).
    MissingSeparator,
    /// The project code component is empty.
    EmptyProjectCode,
    /// The project code component does not match `^[A-Z][A-Z0-9]{1,9}$`
    /// (FR-4).
    InvalidProjectCode,
    /// The project code component is the reserved word `unsorted`
    /// (case-insensitively) (FR-15).
    ReservedProjectCode,
    /// The date component is not exactly six ASCII digits (FR-5).
    DateNotSixDigits,
    /// The date component is six ASCII digits but does not denote a real
    /// calendar date (FR-5).
    DateNotACalendarDate,
    /// The title component is empty after trimming (FR-6).
    EmptyTitle,
    /// The title component contains a Windows-illegal or control
    /// character (FR-6).
    IllegalTitleCharacter(char),
    /// The title component's stem matches a reserved Windows device name
    /// (FR-6).
    ReservedDeviceName,
}

impl Rejection {
    /// One instance of every [`Rejection`] variant, with representative
    /// data for the variant that carries a payload.
    ///
    /// Used to assert, exhaustively, that no two variants render the same
    /// [`std::fmt::Display`] string (NFR-5).
    pub fn all() -> Vec<Rejection> {
        vec![
            Rejection::MissingSeparator,
            Rejection::EmptyProjectCode,
            Rejection::InvalidProjectCode,
            Rejection::ReservedProjectCode,
            Rejection::DateNotSixDigits,
            Rejection::DateNotACalendarDate,
            Rejection::EmptyTitle,
            Rejection::IllegalTitleCharacter(':'),
            Rejection::ReservedDeviceName,
        ]
    }
}

impl std::fmt::Display for Rejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Rejection::MissingSeparator => {
                write!(f, "filename is missing the required \" - \" separators")
            }
            Rejection::EmptyProjectCode => write!(f, "project code is empty"),
            Rejection::InvalidProjectCode => {
                write!(f, "project code does not match ^[A-Z][A-Z0-9]{{1,9}}$")
            }
            Rejection::ReservedProjectCode => write!(
                f,
                "\"unsorted\" is a reserved project code and cannot be used"
            ),
            Rejection::DateNotSixDigits => {
                write!(f, "date is not exactly six ASCII digits")
            }
            Rejection::DateNotACalendarDate => {
                write!(f, "date does not denote a real calendar date")
            }
            Rejection::EmptyTitle => write!(f, "title is empty after trimming"),
            Rejection::IllegalTitleCharacter(c) => {
                write!(f, "title contains the illegal character '{c}'")
            }
            Rejection::ReservedDeviceName => {
                write!(f, "title matches a reserved Windows device name")
            }
        }
    }
}

/// Compiler-enforced exhaustiveness guard for [`Rejection::all`] and
/// [`VaultError::all_kinds`] (NFR-5).
///
/// `all()` / `all_kinds()` are hand-written `Vec`s, so adding a variant to
/// either enum without also adding it here would otherwise leave the
/// pairwise-distinct-`Display` check silently blind to the new variant. The
/// two functions below exist to be compiled, never to be called: each
/// `match` lists every current variant with no wildcard arm, so the build
/// fails the moment a variant is added to `Rejection` or `VaultError` and
/// not added to the corresponding match here — a compile error pointing
/// straight at the enum change, rather than a test quietly continuing to
/// pass with a stale count.
#[cfg(test)]
mod exhaustiveness {
    use super::{Rejection, VaultError};

    #[allow(dead_code)]
    fn rejection_is_exhaustively_matched(r: &Rejection) {
        match r {
            Rejection::MissingSeparator
            | Rejection::EmptyProjectCode
            | Rejection::InvalidProjectCode
            | Rejection::ReservedProjectCode
            | Rejection::DateNotSixDigits
            | Rejection::DateNotACalendarDate
            | Rejection::EmptyTitle
            | Rejection::IllegalTitleCharacter(_)
            | Rejection::ReservedDeviceName => {}
        }
    }

    #[allow(dead_code)]
    fn vault_error_is_exhaustively_matched(e: &VaultError) {
        match e {
            VaultError::RootIsNotADirectory
            | VaultError::SourceMissing
            | VaultError::SourceNotAFile
            | VaultError::UnsupportedMediaType { .. }
            | VaultError::PathEscapesVault
            | VaultError::PathTooLong { .. }
            | VaultError::SuffixLimitExceeded
            | VaultError::AppDataUnavailable
            | VaultError::Io { .. } => {}
        }
    }
}
