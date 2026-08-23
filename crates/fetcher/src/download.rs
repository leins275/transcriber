//! One acquisition: fetch, verify, install, mark ready.
//!
//! Port of `ModelDownload.start()` and `CudaRuntimeDownload.start()`, which
//! were the same loop over different payload shapes. The order of operations is
//! the part worth preserving exactly, because every step of it was paid for by
//! a real failure:
//!
//! 1. A payload that is already installed is skipped, so a restarted wizard
//!    never re-fetches gigabytes it already has.
//! 2. Bytes land in a `<file>.incomplete` sibling, resumed from its current
//!    length, so an interrupted transfer costs only what it had not yet moved.
//! 3. A transfer that ends short of the pinned size is an interruption, not a
//!    success, and the partial file is left where it is to resume from.
//! 4. The digest is checked before the file is renamed into place.
//! 5. A file that was already sitting at the destination is digest-checked too,
//!    not trusted for having the right length: the app folder is user-writable
//!    and a crash mid-write leaves same-sized rubbish.
//! 6. Markers are written last, and only after every payload has verified.
//!
//! Step 6 differs from the Python, which wrote one marker for a whole group. A
//! marker per payload is what `engine::models` reads, and writing them all at
//! the end is what keeps a group of payloads sharing one destination honest: an
//! interrupted run leaves no marker at all rather than a marker claiming a
//! half-extracted tree is ready.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{Failure, FetchError};
use crate::manifest::{Install, Payload};
use crate::transport::{CancelToken, FetchRequest, Transport};
use crate::{extract, manifest, ready_marker, ready_marker_in};

/// How often progress is reported while bytes are moving, unless a caller says
/// otherwise. The Python's contract was "at least once a second"; this is the
/// rate limit that delivers it.
pub const DEFAULT_PROGRESS_INTERVAL: Duration = Duration::from_secs(1);

/// Suffix of the sibling a transfer writes into before verification.
const INCOMPLETE_SUFFIX: &str = ".incomplete";

/// Scratch directory holding archives that are unpacked and then discarded.
const ARCHIVE_DIRNAME: &str = "_archives";

/// Where an acquisition has got to.
///
/// A wire vocabulary shared with the first-run wizard and the CLI, so the
/// strings are spelled out rather than derived from the variant names.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DownloadState {
    Idle,
    Downloading,
    Verifying,
    Complete,
    /// Asked to stop. Distinct from [`DownloadState::Error`] because a user
    /// stopping a download is not the same as a download breaking, and because
    /// the partial file is deliberately kept.
    Cancelled,
    Error,
}

impl DownloadState {
    /// The wire string, as polled by the UI.
    pub fn as_str(self) -> &'static str {
        match self {
            DownloadState::Idle => "idle",
            DownloadState::Downloading => "downloading",
            DownloadState::Verifying => "verifying",
            DownloadState::Complete => "complete",
            DownloadState::Cancelled => "cancelled",
            DownloadState::Error => "error",
        }
    }
}

impl std::fmt::Display for DownloadState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One progress report.
///
/// `percent` is carried rather than left to the caller because two callers
/// computing it from the same two numbers is two chances to divide by zero.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Progress {
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    /// The payload currently moving, empty between payloads and at the end.
    pub file: String,
    pub state: DownloadState,
}

fn percent_of(downloaded: u64, total: u64) -> f64 {
    if total == 0 {
        0.0
    } else {
        (downloaded as f64 / total as f64) * 100.0
    }
}

/// Throttles progress reports without ever swallowing one that matters.
struct Emitter<'a> {
    sink: &'a mut dyn FnMut(&Progress),
    interval: Duration,
    last: Instant,
}

impl Emitter<'_> {
    /// Report unless something was reported less than `interval` ago.
    fn throttled(&mut self, progress: &Progress) {
        if self.last.elapsed() >= self.interval {
            self.last = Instant::now();
            (self.sink)(progress);
        }
    }

    /// Report whatever the throttle says. Phase changes and terminal events go
    /// through this, so a caller always learns how a download ended even if it
    /// ended inside the throttle window.
    fn forced(&mut self, progress: &Progress) {
        self.last = Instant::now();
        (self.sink)(progress);
    }
}

/// One acquisition of one set of payloads into one directory.
///
/// Synchronous and blocking, like the Python it ports: a caller that needs it
/// off the current thread uses [`crate::DownloadManager`], which is also where
/// the one-transfer-at-a-time rule lives. Here, `&mut self` already makes a
/// second concurrent `start()` on the same download impossible.
pub struct Download {
    dest_dir: PathBuf,
    payloads: Vec<Payload>,
    transport: Box<dyn Transport>,
    cancel: CancelToken,
    /// Live count, shared with the chunk callback so it can be read while the
    /// transfer runs.
    downloaded: Arc<AtomicU64>,
    total_bytes: u64,
    state: DownloadState,
    failure: Option<Failure>,
}

impl Download {
    /// Prepare an acquisition, refusing it now if any URL is off the
    /// allowlist or there is nothing to fetch.
    ///
    /// Nothing here touches the network or the disk, so building one is cheap
    /// enough to do on a UI thread.
    pub fn new(
        dest_dir: impl Into<PathBuf>,
        payloads: Vec<Payload>,
        transport: Box<dyn Transport>,
    ) -> Result<Self, FetchError> {
        manifest::check_payloads(&payloads)?;
        let total_bytes = payloads.iter().map(|p| p.size).sum();
        Ok(Download {
            dest_dir: dest_dir.into(),
            payloads,
            transport,
            cancel: CancelToken::new(),
            downloaded: Arc::new(AtomicU64::new(0)),
            total_bytes,
            state: DownloadState::Idle,
            failure: None,
        })
    }

    /// The token this download watches, for a caller that wants to cancel from
    /// another thread.
    pub fn cancel_token(&self) -> CancelToken {
        self.cancel.clone()
    }

    /// Ask the transfer to stop; it stops within one chunk.
    pub fn cancel(&self) {
        self.cancel.cancel();
    }

    pub fn state(&self) -> DownloadState {
        self.state
    }

    pub fn downloaded_bytes(&self) -> u64 {
        self.downloaded.load(Ordering::Relaxed)
    }

    pub fn total_bytes(&self) -> u64 {
        self.total_bytes
    }

    /// How the last run failed, if it did.
    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }

    /// Where a payload ends up: the file itself, or the directory a zip
    /// payload was unpacked into.
    pub fn installed_path(&self, payload: &Payload) -> PathBuf {
        match &payload.install {
            Install::File => self.dest_dir.join(&payload.file_name),
            Install::ZipTree { dest_subdir, .. } => {
                if dest_subdir.is_empty() {
                    self.dest_dir.clone()
                } else {
                    self.dest_dir.join(dest_subdir)
                }
            }
        }
    }

    /// Whether every payload is present *and* verified, so `start()` would
    /// have nothing to do.
    pub fn already_installed(&self) -> bool {
        self.payloads.iter().all(|p| self.is_payload_installed(p))
    }

    fn is_payload_installed(&self, payload: &Payload) -> bool {
        let path = self.installed_path(payload);
        match payload.install {
            Install::File => crate::is_installed(&path),
            Install::ZipTree { .. } => crate::is_extracted(&path),
        }
    }

    /// Run the acquisition to completion, to cancellation, or to failure.
    ///
    /// `on_progress` is called at most once per `interval` while bytes move,
    /// and always on a phase change and on the terminal event.
    pub fn start(
        &mut self,
        on_progress: &mut dyn FnMut(&Progress),
        interval: Duration,
    ) -> Result<(), FetchError> {
        let mut emitter = Emitter {
            sink: on_progress,
            interval,
            last: Instant::now(),
        };

        if self.already_installed() {
            // Idempotency: a restarted wizard, or a retry of a two-phase setup
            // whose other phase failed, must not re-fetch what is already here.
            self.state = DownloadState::Complete;
            self.downloaded.store(self.total_bytes, Ordering::Relaxed);
            emitter.forced(&self.progress(DownloadState::Complete, ""));
            return Ok(());
        }

        // A retry on a token that was cancelled once must not stop instantly.
        self.cancel.reset();
        self.state = DownloadState::Downloading;
        self.failure = None;
        self.downloaded.store(0, Ordering::Relaxed);
        // Forced, so a caller learns the byte total before the first chunk
        // rather than a second later.
        emitter.forced(&self.progress(DownloadState::Downloading, ""));

        match self.run(&mut emitter) {
            Ok(DownloadState::Cancelled) => {
                self.state = DownloadState::Cancelled;
                emitter.forced(&self.progress(DownloadState::Cancelled, ""));
                Ok(())
            }
            Ok(_) => {
                self.state = DownloadState::Complete;
                emitter.forced(&self.progress(DownloadState::Complete, ""));
                Ok(())
            }
            Err(error) => {
                self.state = DownloadState::Error;
                self.failure = Some(error.failure());
                emitter.forced(&self.progress(DownloadState::Error, ""));
                Err(error)
            }
        }
    }

    /// The three phases, so `start()` is left holding only the state
    /// transitions.
    fn run(&self, emitter: &mut Emitter<'_>) -> Result<DownloadState, FetchError> {
        let landed = match self.transfer_all(emitter)? {
            Some(landed) => landed,
            None => return Ok(DownloadState::Cancelled),
        };

        emitter.forced(&self.progress(DownloadState::Verifying, ""));
        self.install_all(&landed)?;
        Ok(DownloadState::Complete)
    }

    /// Fetch every payload that is not already installed, returning where each
    /// one's verified bytes landed, or `None` if the run was cancelled.
    fn transfer_all(
        &self,
        emitter: &mut Emitter<'_>,
    ) -> Result<Option<Vec<(PathBuf, Payload)>>, FetchError> {
        let mut landed = Vec::new();
        for payload in &self.payloads {
            if self.cancel.is_cancelled() {
                return Ok(None);
            }
            if self.is_payload_installed(payload) {
                self.downloaded.fetch_add(payload.size, Ordering::Relaxed);
                emitter.throttled(&self.progress(DownloadState::Downloading, &payload.name));
                continue;
            }

            let dest = self.download_path(payload);
            match self.transfer_one(payload, &dest, emitter)? {
                Some(()) => landed.push((dest, payload.clone())),
                None => return Ok(None),
            }
        }
        Ok(Some(landed))
    }

    /// Fetch one payload into `dest`, verified. `None` means cancelled.
    fn transfer_one(
        &self,
        payload: &Payload,
        dest: &Path,
        emitter: &mut Emitter<'_>,
    ) -> Result<Option<()>, FetchError> {
        if dest.is_file() {
            // A prior run already landed this file (it may have died during a
            // later payload, or during extraction). Size is not proof: the app
            // folder is user-writable and a crash mid-write can leave a
            // same-sized but corrupt file, which would otherwise be installed
            // unverified.
            if file_len(dest)? == payload.size && sha256_file(dest)? == payload.sha256 {
                self.downloaded.fetch_add(payload.size, Ordering::Relaxed);
                emitter.throttled(&self.progress(DownloadState::Downloading, &payload.name));
                return Ok(Some(()));
            }
            std::fs::remove_file(dest).map_err(|source| FetchError::io(dest, source))?;
        }

        let incomplete = incomplete_sibling(dest);
        let mut resume_from = if incomplete.is_file() {
            file_len(&incomplete)?
        } else {
            0
        };
        if resume_from > payload.size {
            // A stale or corrupt partial blob longer than the real file is
            // never a resume point.
            std::fs::remove_file(&incomplete)
                .map_err(|source| FetchError::io(&incomplete, source))?;
            resume_from = 0;
        }
        self.downloaded.fetch_add(resume_from, Ordering::Relaxed);

        let fetched = {
            // Held in locals so the chunk callback borrows neither `self` nor
            // the emitter's sink twice.
            let downloaded = Arc::clone(&self.downloaded);
            let total = self.total_bytes;
            let name = payload.name.clone();
            let sink = &mut *emitter.sink;
            let last = &mut emitter.last;
            let interval = emitter.interval;
            let mut on_chunk = |written: u64| {
                let so_far = downloaded.fetch_add(written, Ordering::Relaxed) + written;
                if last.elapsed() >= interval {
                    *last = Instant::now();
                    sink(&Progress {
                        downloaded_bytes: so_far,
                        total_bytes: total,
                        percent: percent_of(so_far, total),
                        file: name.clone(),
                        state: DownloadState::Downloading,
                    });
                }
            };
            self.transport.fetch(FetchRequest {
                url: &payload.url,
                dest: &incomplete,
                resume_from,
                on_chunk: &mut on_chunk,
                cancel: &self.cancel,
            })?
        };
        if fetched.restarted {
            // The server ignored the range and sent the whole file, so the
            // bytes counted for the resume point were counted twice.
            self.downloaded.fetch_sub(resume_from, Ordering::Relaxed);
        }

        if self.cancel.is_cancelled() {
            return Ok(None);
        }

        let actual = if incomplete.is_file() {
            file_len(&incomplete)?
        } else {
            0
        };
        if actual != payload.size {
            // A dropped connection or a killed process, not a deliberate
            // cancel. The partial blob stays exactly where it is so the next
            // attempt resumes from here instead of from byte zero.
            return Err(FetchError::Interrupted {
                file: payload.file_name.clone(),
                expected: payload.size,
                actual,
            });
        }

        let digest = sha256_file(&incomplete)?;
        if digest != payload.sha256 {
            // Unlike a short transfer, this one is not resumable: the bytes
            // that arrived are wrong, so keeping them would poison every
            // retry.
            std::fs::remove_file(&incomplete)
                .map_err(|source| FetchError::io(&incomplete, source))?;
            return Err(FetchError::DigestMismatch {
                file: payload.file_name.clone(),
                expected: payload.sha256.clone(),
                actual: digest,
            });
        }

        std::fs::rename(&incomplete, dest).map_err(|source| FetchError::io(dest, source))?;
        Ok(Some(()))
    }

    /// Unpack what needs unpacking, then write every marker.
    fn install_all(&self, landed: &[(PathBuf, Payload)]) -> Result<(), FetchError> {
        for (archive, payload) in landed {
            if let Install::ZipTree { prefix, .. } = &payload.install {
                extract::extract_tree(archive, prefix, &self.installed_path(payload))?;
            }
        }

        // Only now, with every payload verified and unpacked, does anything on
        // disk count as installed.
        for payload in &self.payloads {
            self.write_marker(payload)?;
        }

        // The archives are pure duplication once unpacked -- hundreds of
        // megabytes of it -- and nothing reads them again.
        let scratch = self.dest_dir.join(ARCHIVE_DIRNAME);
        if scratch.is_dir() {
            let _ = std::fs::remove_dir_all(&scratch);
        }
        Ok(())
    }

    fn write_marker(&self, payload: &Payload) -> Result<(), FetchError> {
        let path = self.installed_path(payload);
        let marker = match payload.install {
            Install::File => ready_marker(&path),
            Install::ZipTree { .. } => {
                // An archive whose prefix matched nothing leaves no directory
                // to mark, and a marker with no tree beside it would be a lie
                // either way.
                std::fs::create_dir_all(&path).map_err(|source| FetchError::io(&path, source))?;
                ready_marker_in(&path)
            }
        };
        // Provenance, so a support session can tell which pin a machine has.
        // `engine::models` only tests that the marker exists, so its contents
        // are free to be useful.
        let body = serde_json::json!({
            "name": payload.name,
            "url": payload.url,
            "sha256": payload.sha256,
            "verified_at": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or_default(),
        });
        std::fs::write(&marker, body.to_string()).map_err(|source| FetchError::io(&marker, source))
    }

    /// Re-check what is on disk against the pins, removing the marker of
    /// anything that no longer matches.
    ///
    /// A zip payload can only be checked for presence: its archive is deleted
    /// once unpacked, and the unpacked files have no individual pins. That is
    /// the same trade the Python made and the reason the digest is checked
    /// before extraction rather than after.
    pub fn verify(&self) -> bool {
        let mut ok = true;
        for payload in &self.payloads {
            let path = self.installed_path(payload);
            let good = match payload.install {
                Install::File => {
                    path.is_file()
                        && file_len(&path).map(|n| n == payload.size).unwrap_or(false)
                        && sha256_file(&path)
                            .map(|d| d == payload.sha256)
                            .unwrap_or(false)
                }
                Install::ZipTree { .. } => path.is_dir(),
            };
            if !good {
                ok = false;
                let _ = std::fs::remove_file(match payload.install {
                    Install::File => ready_marker(&path),
                    Install::ZipTree { .. } => ready_marker_in(&path),
                });
            }
        }
        ok
    }

    /// Where a payload's bytes are written: the installed path for a plain
    /// file, or a scratch directory for an archive that is about to be
    /// unpacked and thrown away.
    fn download_path(&self, payload: &Payload) -> PathBuf {
        match payload.install {
            Install::File => self.dest_dir.join(&payload.file_name),
            Install::ZipTree { .. } => self.dest_dir.join(ARCHIVE_DIRNAME).join(&payload.file_name),
        }
    }

    fn progress(&self, state: DownloadState, file: &str) -> Progress {
        let downloaded = self.downloaded.load(Ordering::Relaxed);
        Progress {
            downloaded_bytes: downloaded,
            total_bytes: self.total_bytes,
            percent: percent_of(downloaded, self.total_bytes),
            file: file.to_string(),
            state,
        }
    }
}

impl std::fmt::Debug for Download {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Download")
            .field("dest_dir", &self.dest_dir)
            .field("payloads", &self.payloads.len())
            .field("state", &self.state)
            .finish()
    }
}

/// The `<file>.incomplete` sibling a transfer writes into.
pub(crate) fn incomplete_sibling(dest: &Path) -> PathBuf {
    let mut name = dest.as_os_str().to_os_string();
    name.push(INCOMPLETE_SUFFIX);
    PathBuf::from(name)
}

fn file_len(path: &Path) -> Result<u64, FetchError> {
    std::fs::metadata(path)
        .map(|m| m.len())
        .map_err(|source| FetchError::io(path, source))
}

/// Lowercase hex SHA-256 of a file, read in chunks so a multi-gigabyte model
/// never has to fit in memory.
pub fn sha256_file(path: &Path) -> Result<String, FetchError> {
    use std::io::Read;

    let mut file = std::fs::File::open(path).map_err(|source| FetchError::io(path, source))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|source| FetchError::io(path, source))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_state_vocabulary_is_the_python_one() {
        // Six states, spelled exactly as the wizard and the CLI poll them.
        assert_eq!(DownloadState::Idle.as_str(), "idle");
        assert_eq!(DownloadState::Downloading.as_str(), "downloading");
        assert_eq!(DownloadState::Verifying.as_str(), "verifying");
        assert_eq!(DownloadState::Complete.as_str(), "complete");
        assert_eq!(DownloadState::Cancelled.as_str(), "cancelled");
        assert_eq!(DownloadState::Error.as_str(), "error");
        assert_eq!(
            serde_json::to_string(&DownloadState::Cancelled).unwrap(),
            "\"cancelled\""
        );
    }

    #[test]
    fn percent_of_nothing_is_zero_rather_than_a_division_by_zero() {
        assert_eq!(percent_of(0, 0), 0.0);
        assert_eq!(percent_of(50, 200), 25.0);
    }

    #[test]
    fn the_incomplete_sibling_sits_next_to_the_file_it_belongs_to() {
        let sibling = incomplete_sibling(Path::new("C:\\models\\ggml-large-v3.bin"));
        assert_eq!(
            sibling,
            PathBuf::from("C:\\models\\ggml-large-v3.bin.incomplete")
        );
    }

    #[test]
    fn hashing_a_file_matches_the_digest_of_its_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.bin");
        std::fs::write(&path, b"abc").unwrap();
        assert_eq!(
            sha256_file(&path).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
