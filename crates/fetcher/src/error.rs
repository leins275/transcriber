//! Why an acquisition failed, and how that reaches a poller.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// The failure taxonomy, as the first-run wizard and the ledger already spell
/// it.
///
/// These four are the subset of `wire::ErrorKind` a download can produce. They
/// are literals here rather than a dependency on the wire crate: this crate
/// sits below the document formats and must stay linkable on its own, and the
/// strings are a wire contract in either spelling. Renaming one here without
/// renaming it there is the mistake to watch for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// The request itself was wrong -- a host that is not on the allowlist, or
    /// a manifest selection that names nothing.
    InvalidRequest,
    /// The remote end did not deliver. Retryable, and the partial transfer is
    /// deliberately left on disk to resume from.
    ProviderUnavailable,
    Internal,
}

impl ErrorKind {
    /// The wire string, as polled by the UI and stored by callers.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InvalidRequest => "invalid_request",
            ErrorKind::ProviderUnavailable => "provider_unavailable",
            ErrorKind::Internal => "internal",
        }
    }
}

impl std::fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A failure flattened for polling: what kind, and what to show.
///
/// The status surface cannot hand out a [`FetchError`] because it is cloned on
/// every poll and an [`std::io::Error`] does not clone.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Failure {
    pub kind: ErrorKind,
    pub message: String,
}

/// Everything that can go wrong between a pinned URL and a verified payload.
#[derive(Debug, thiserror::Error)]
pub enum FetchError {
    /// Refused before any connection was opened. Carries the URL because the
    /// interesting part of this failure is always which host was asked for.
    #[error("refusing to fetch {url}: its host is not on the allowlist")]
    HostNotAllowed { url: String },

    /// A selection that matches nothing is an error, not an empty download: a
    /// zero-payload "success" would write `.ready` markers over nothing.
    #[error("no payload was selected to download")]
    NothingToDownload,

    #[error("could not build the HTTP client: {source}")]
    Client {
        #[source]
        source: reqwest::Error,
    },

    #[error("request for {url} failed: {source}")]
    Request {
        url: String,
        #[source]
        source: reqwest::Error,
    },

    #[error("request for {url} was answered with HTTP {status}")]
    Status { url: String, status: u16 },

    /// The transfer stopped short without anyone cancelling it -- a dropped
    /// connection or a killed process. The partial file stays on disk so the
    /// next attempt resumes from it.
    #[error("transfer interrupted for {file}: {actual} of {expected} bytes")]
    Interrupted {
        file: String,
        expected: u64,
        actual: u64,
    },

    #[error("digest mismatch for {file}: expected {expected}, got {actual}")]
    DigestMismatch {
        file: String,
        expected: String,
        actual: String,
    },

    #[error("{file} is missing from {dir}")]
    MissingPayload { file: String, dir: PathBuf },

    #[error("could not read the archive {path}: {source}")]
    Archive {
        path: PathBuf,
        #[source]
        source: zip::result::ZipError,
    },

    /// A zip member whose path escapes the destination directory. Refusing the
    /// whole archive is the only safe answer: a payload that tries this is not
    /// the payload that was pinned.
    #[error("{path} contains an entry that escapes the destination: {member}")]
    UnsafeArchiveMember { path: PathBuf, member: String },

    #[error("{path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

impl FetchError {
    /// How this failure is attributed to a user.
    pub fn kind(&self) -> ErrorKind {
        match self {
            FetchError::HostNotAllowed { .. } | FetchError::NothingToDownload => {
                ErrorKind::InvalidRequest
            }
            FetchError::Request { .. }
            | FetchError::Status { .. }
            | FetchError::Interrupted { .. } => ErrorKind::ProviderUnavailable,
            FetchError::Client { .. }
            | FetchError::DigestMismatch { .. }
            | FetchError::MissingPayload { .. }
            | FetchError::Archive { .. }
            | FetchError::UnsafeArchiveMember { .. }
            | FetchError::Io { .. } => ErrorKind::Internal,
        }
    }

    /// The poll-shaped form of this failure.
    pub fn failure(&self) -> Failure {
        Failure {
            kind: self.kind(),
            message: self.to_string(),
        }
    }

    /// An I/O failure that remembers which path it was about, because
    /// `std::io::Error` does not.
    pub(crate) fn io(path: impl Into<PathBuf>, source: std::io::Error) -> Self {
        FetchError::Io {
            path: path.into(),
            source,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_dropped_connection_is_retryable_and_a_bad_digest_is_not() {
        // The distinction the first-run wizard acts on: one offers a retry of
        // the same transfer, the other means the pin and the artifact disagree.
        let interrupted = FetchError::Interrupted {
            file: "ggml-large-v3.bin".to_string(),
            expected: 100,
            actual: 40,
        };
        assert_eq!(interrupted.kind(), ErrorKind::ProviderUnavailable);

        let mismatch = FetchError::DigestMismatch {
            file: "ggml-large-v3.bin".to_string(),
            expected: "a".to_string(),
            actual: "b".to_string(),
        };
        assert_eq!(mismatch.kind(), ErrorKind::Internal);
    }

    #[test]
    fn a_refused_host_is_the_callers_mistake_not_the_networks() {
        let err = FetchError::HostNotAllowed {
            url: "https://example.com/x".to_string(),
        };
        assert_eq!(err.kind(), ErrorKind::InvalidRequest);
        assert_eq!(err.failure().kind, ErrorKind::InvalidRequest);
        assert!(err.failure().message.contains("example.com"));
    }

    #[test]
    fn wire_strings_match_the_python_taxonomy() {
        assert_eq!(ErrorKind::InvalidRequest.as_str(), "invalid_request");
        assert_eq!(
            ErrorKind::ProviderUnavailable.as_str(),
            "provider_unavailable"
        );
        assert_eq!(ErrorKind::Internal.as_str(), "internal");
        assert_eq!(
            serde_json::to_string(&ErrorKind::ProviderUnavailable).unwrap(),
            "\"provider_unavailable\""
        );
    }
}
