//! Acquisition of the payloads the engine cannot ship inside the installer:
//! whisper weights, VAD weights, the LLM's GGUF, and zipped runtime trees.
//!
//! Port of `services/transcription/src/transcription/model_download.py` and
//! `cuda_runtime.py`. Those two modules disagreed only about what a finished
//! transfer means -- a directory of hub files versus a zip to unpack -- so the
//! port keeps one download loop and makes that difference a per-payload
//! [`Install`] choice.
//!
//! # Why this is a crate of its own
//!
//! It is the only code in the workspace that opens a connection to the public
//! internet. The desktop crate builds `reqwest` with no TLS at all, because the
//! only thing it ever talked to was the Python sidecar on loopback. Keeping the
//! TLS-capable client, the certificate roots and the host allowlist in one
//! small crate means the surface that can reach outward stays reviewable in a
//! sitting.
//!
//! Two limits on that claim, stated because they are easy to over-read. Cargo
//! unifies features across a workspace, so linking this crate does put a
//! TLS-capable `reqwest` into the final binary; what is confined here is the
//! code that *uses* it. And confinement only helps if the destination is
//! constrained too, which is what [`allowlist`] is for: every request, and
//! every redirect hop, must land on a host this crate names as a constant.
//!
//! A URL or a digest is never read from `config.json`. They come from
//! [`manifest::PINS`], compiled into the binary, or from a caller that built a
//! [`Payload`] in code. Configuration can say *where* a file goes and which
//! model is wanted; it can never say what to fetch or what it should hash to.
//!
//! # What the Python proved, and what is kept
//!
//! - A transfer resumes from a `<file>.incomplete` sibling with an HTTP
//!   `Range` request, so a dropped connection costs the remaining bytes and not
//!   all of them.
//! - The completed file is verified against the pinned SHA-256 before it counts
//!   as anything.
//! - A `.ready` marker is written last, after verification. A file on disk
//!   means nothing without it, because a half-finished download is a file too.
//!   The convention -- marker path, and marker written last -- is the one
//!   `engine::models` reads; see [`ready_marker`].
//! - Exactly one transfer runs at a time. A second request gets the running
//!   one's status, never a parallel transfer ([`DownloadManager`]).
//! - Progress is throttled to roughly one report a second, with phase changes
//!   and terminal events always forced through, and cancellation is checked
//!   between chunks.
//! - The six-state vocabulary [`DownloadState`] is a wire contract shared with
//!   the first-run wizard, so the strings are spelled out rather than derived.

pub mod allowlist;
pub mod download;
pub mod error;
pub mod extract;
pub mod fakes;
pub mod manager;
pub mod manifest;
pub mod transport;

use std::path::{Path, PathBuf};

pub use download::{Download, DownloadState, Progress, DEFAULT_PROGRESS_INTERVAL};
pub use error::{ErrorKind, Failure, FetchError};
pub use manager::{DownloadManager, Status};
pub use manifest::{Install, Payload, PinnedPayload};
pub use transport::{CancelToken, FetchRequest, Fetched, HttpTransport, Transport};

/// Suffix of the marker written after a payload is fully downloaded and
/// verified.
///
/// The same constant exists as `engine::models::READY_SUFFIX`. It is repeated
/// rather than shared because the dependency would have to run from the
/// downloader to the engine or back, and neither direction is true: the engine
/// reads a convention this crate writes.
pub const READY_SUFFIX: &str = ".ready";

/// The marker that says the file `payload` finished downloading and verified.
pub fn ready_marker(payload: &Path) -> PathBuf {
    let mut marker = payload.as_os_str().to_os_string();
    marker.push(READY_SUFFIX);
    PathBuf::from(marker)
}

/// The marker that says the tree unpacked into `dir` is complete.
///
/// Inside the directory rather than beside it, which is where the Python kept
/// it and the only place that cannot write outside the destination it was
/// given. A file's marker is a sibling because a file has nowhere to put one.
pub fn ready_marker_in(dir: &Path) -> PathBuf {
    dir.join(READY_SUFFIX)
}

/// Whether a single-file payload is present *and* was verified.
pub fn is_installed(payload: &Path) -> bool {
    payload.is_file() && ready_marker(payload).exists()
}

/// Whether an extracted payload tree is present *and* was verified.
pub fn is_extracted(dir: &Path) -> bool {
    dir.is_dir() && ready_marker_in(dir).exists()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_file_without_its_marker_is_not_installed() {
        // The half-download case, and the whole reason the marker exists:
        // bytes on disk prove nothing until the digest has been checked.
        let dir = tempfile::tempdir().unwrap();
        let payload = dir.path().join("ggml-large-v3.bin");
        std::fs::write(&payload, b"partial").unwrap();
        assert!(!is_installed(&payload));

        std::fs::write(ready_marker(&payload), b"{}").unwrap();
        assert!(is_installed(&payload));
    }

    #[test]
    fn a_marker_without_its_file_is_not_installed() {
        let dir = tempfile::tempdir().unwrap();
        let payload = dir.path().join("ggml-large-v3.bin");
        std::fs::write(ready_marker(&payload), b"{}").unwrap();
        assert!(!is_installed(&payload));
    }

    #[test]
    fn the_marker_path_matches_the_convention_the_engine_reads() {
        assert_eq!(
            ready_marker(Path::new("C:\\models\\ggml-large-v3.bin")),
            PathBuf::from("C:\\models\\ggml-large-v3.bin.ready")
        );
        assert_eq!(
            ready_marker_in(Path::new("C:\\app\\runtime")),
            PathBuf::from("C:\\app\\runtime\\.ready")
        );
    }
}
