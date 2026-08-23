//! The seam that moves bytes, and the one implementation that uses a network.
//!
//! The Python had this seam so resume, cancellation and progress could be
//! tested against a fake with no network, no weights and no waiting; the same
//! reason keeps it here. Everything above [`Transport`] is ordinary file and
//! digest work, and [`crate::fakes::FakeTransport`] replaces the rest.
//!
//! The API is blocking rather than async, which is a deliberate narrowing. The
//! transfer is one long sequential read whose only concurrency requirement is
//! that it not block the UI, and [`crate::DownloadManager`] already satisfies
//! that by owning a thread. A blocking client needs no runtime handed in, keeps
//! the loop readable enough to see the cancellation check between chunks, and
//! leaves this crate usable from a plain `std::thread` in a program that has no
//! async runtime at all. The one constraint it imposes is stated on
//! [`HttpTransport::fetch`].

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::RANGE;
use reqwest::{redirect, StatusCode};

use crate::allowlist;
use crate::error::FetchError;

/// How much is read before the cancellation flag is checked again, which is
/// therefore also the worst-case latency of a cancel.
const CHUNK_BYTES: usize = 1024 * 1024;

/// A redirect chain longer than this is a loop or a misconfiguration, not a
/// CDN handoff.
const MAX_REDIRECTS: usize = 5;

/// Cooperative cancellation, checked between chunks.
///
/// Cloning shares the flag, so a caller can hold one while the transfer holds
/// another and neither has to reach into the other.
#[derive(Debug, Clone, Default)]
pub struct CancelToken(Arc<AtomicBool>);

impl CancelToken {
    pub fn new() -> Self {
        CancelToken::default()
    }

    /// Ask the transfer to stop. It stops within one chunk, leaving the
    /// partial file in place to resume from.
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    /// Clear the flag so a retry on the same token is not cancelled before it
    /// begins.
    pub fn reset(&self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

/// One transfer's parameters, named rather than positional because four of the
/// five are easy to swap by accident.
pub struct FetchRequest<'a> {
    pub url: &'a str,
    /// Where the bytes are written -- always the `.incomplete` sibling, never
    /// the final path.
    pub dest: &'a Path,
    /// Byte offset to resume from, or zero to start fresh.
    pub resume_from: u64,
    /// Called with the length of each chunk actually written.
    pub on_chunk: &'a mut dyn FnMut(u64),
    pub cancel: &'a CancelToken,
}

/// What a transfer did, beyond succeeding.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Fetched {
    /// True when a resume was asked for and the server answered with the whole
    /// file anyway, so `dest` was rewritten from byte zero.
    ///
    /// The Python had no equivalent and would have appended a second copy of
    /// the file to the partial one, producing an oversized blob that failed
    /// verification for a reason nobody could read. The caller uses this to
    /// discount the bytes it had already counted.
    pub restarted: bool,
}

/// The seam that moves bytes.
pub trait Transport: Send + Sync {
    /// Append (or write) the remote file to `dest`, returning once it is
    /// complete or the transfer was cancelled.
    ///
    /// Cancellation is not an error: an implementation returns `Ok` with a
    /// short file, and the caller notices the cancel flag itself.
    fn fetch(&self, request: FetchRequest<'_>) -> Result<Fetched, FetchError>;
}

/// The real transport: a resumable ranged GET over HTTPS.
#[derive(Debug)]
pub struct HttpTransport {
    client: Client,
}

impl HttpTransport {
    /// Build a client that can only reach allowlisted hosts.
    ///
    /// The redirect policy is the load-bearing part. Hugging Face answers a
    /// download with a redirect to an LFS CDN, so redirects have to be
    /// followed, and every hop is a request to a host the original URL check
    /// never saw. A hop off the allowlist fails the request outright rather
    /// than stopping quietly, because a stopped redirect returns the 3xx body
    /// as if it were the payload.
    pub fn new() -> Result<Self, FetchError> {
        let policy = redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= MAX_REDIRECTS {
                attempt.error("too many redirects")
            } else if allowlist::is_allowed(attempt.url()) {
                attempt.follow()
            } else {
                let refused = format!("redirect to {} left the allowlist", attempt.url());
                attempt.error(refused)
            }
        });

        let client = Client::builder()
            .redirect(policy)
            // Belt to the allowlist's braces: even a bug that let an `http`
            // URL through would fail here rather than on the wire.
            .https_only(true)
            .connect_timeout(Duration::from_secs(30))
            // The blocking client's `timeout` bounds the whole request, body
            // included, and defaults to 30 seconds -- which would abort every
            // model download this crate exists to make. There is no idle-read
            // timeout on the blocking builder to put in its place, so the
            // guard against a hang is the connect timeout above plus a cancel
            // the user can press.
            .timeout(None)
            .user_agent(concat!("transcriber-fetcher/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|source| FetchError::Client { source })?;
        Ok(HttpTransport { client })
    }
}

impl Transport for HttpTransport {
    /// Must not be called from a thread running an async executor: the
    /// blocking client refuses to nest inside one. [`crate::DownloadManager`]
    /// runs it on a thread of its own, which is the intended path.
    fn fetch(&self, request: FetchRequest<'_>) -> Result<Fetched, FetchError> {
        // Checked again here even though the caller checked at construction:
        // this is the function that opens the connection, and it is the one
        // place a future caller cannot forget.
        let url = allowlist::check(request.url)?;

        let mut builder = self.client.get(url);
        if request.resume_from > 0 {
            builder = builder.header(RANGE, format!("bytes={}-", request.resume_from));
        }
        let mut response = builder.send().map_err(|source| FetchError::Request {
            url: request.url.to_string(),
            source,
        })?;
        if !response.status().is_success() {
            return Err(FetchError::Status {
                url: request.url.to_string(),
                status: response.status().as_u16(),
            });
        }

        // A server may ignore `Range` and answer 200 with the whole file.
        // Appending that to the partial one would silently corrupt it.
        let resuming = request.resume_from > 0 && response.status() == StatusCode::PARTIAL_CONTENT;
        let restarted = request.resume_from > 0 && !resuming;

        let mut file = open_dest(request.dest, resuming)?;
        let mut buffer = vec![0u8; CHUNK_BYTES];
        loop {
            if request.cancel.is_cancelled() {
                break;
            }
            let read = response
                .read(&mut buffer)
                .map_err(|source| FetchError::io(request.dest, source))?;
            if read == 0 {
                break;
            }
            file.write_all(&buffer[..read])
                .map_err(|source| FetchError::io(request.dest, source))?;
            (request.on_chunk)(read as u64);
        }
        file.flush()
            .map_err(|source| FetchError::io(request.dest, source))?;
        Ok(Fetched { restarted })
    }
}

/// Open the partial file for appending, or truncate it to start over.
pub(crate) fn open_dest(dest: &Path, append: bool) -> Result<File, FetchError> {
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|source| FetchError::io(parent, source))?;
    }
    OpenOptions::new()
        .create(true)
        .write(true)
        .append(append)
        .truncate(!append)
        .open(dest)
        .map_err(|source| FetchError::io(dest, source))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cancel_token_is_shared_by_its_clones() {
        let token = CancelToken::new();
        let clone = token.clone();
        assert!(!clone.is_cancelled());
        token.cancel();
        assert!(clone.is_cancelled());

        clone.reset();
        assert!(!token.is_cancelled(), "a retry must not start cancelled");
    }

    #[test]
    fn the_client_refuses_to_be_built_around_a_disallowed_host() {
        // Nothing about client construction reaches a network, so this is the
        // cheap half of the guarantee; the redirect policy is the other half.
        let transport = HttpTransport::new().expect("build client");
        let mut on_chunk = |_n: u64| {};
        let err = transport
            .fetch(FetchRequest {
                url: "https://example.com/model.bin",
                dest: Path::new("unused"),
                resume_from: 0,
                on_chunk: &mut on_chunk,
                cancel: &CancelToken::new(),
            })
            .unwrap_err();
        assert!(matches!(err, FetchError::HostNotAllowed { .. }));
    }

    #[test]
    fn opening_a_destination_creates_the_directory_it_lives_in() {
        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("nested/deeper/file.bin.incomplete");
        open_dest(&dest, false).expect("open");
        assert!(dest.is_file());
    }

    #[test]
    fn reopening_to_append_keeps_what_was_already_written() {
        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("file.bin.incomplete");
        std::fs::write(&dest, b"first").unwrap();

        let mut file = open_dest(&dest, true).expect("append");
        file.write_all(b"second").unwrap();
        drop(file);
        assert_eq!(std::fs::read(&dest).unwrap(), b"firstsecond");

        // And the truncating form is what a restart-from-zero needs.
        let mut file = open_dest(&dest, false).expect("truncate");
        file.write_all(b"again").unwrap();
        drop(file);
        assert_eq!(std::fs::read(&dest).unwrap(), b"again");
    }
}
