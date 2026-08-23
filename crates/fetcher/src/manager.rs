//! The single download slot, and the status a poller reads.
//!
//! Port of `api/model_routes.py`'s `ModelDownloadManager`. Exactly one transfer
//! exists at a time: a second request while one is running is answered with the
//! running one's status, never with a second parallel transfer. That was worth
//! a class of its own in the Python and is worth one here for the same reason
//! -- a first-run wizard that retries a request, or a user who clicks twice,
//! must not double the bytes moving over a slow connection.
//!
//! One difference from the Python, which had a race it never hit in practice:
//! the slot is marked busy before the worker thread starts, not by the worker
//! itself. A second request arriving in the moment before the thread is
//! scheduled would otherwise have found an idle download and started another.

use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::JoinHandle;

use serde::{Deserialize, Serialize};

use crate::download::{Download, DownloadState, Progress, DEFAULT_PROGRESS_INTERVAL};
use crate::error::ErrorKind;

/// What a poller sees. The wire shape the first-run wizard and the CLI read.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Status {
    pub state: DownloadState,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub percent: f64,
    /// The payload currently moving, empty when none is.
    pub file: String,
    pub error_kind: Option<ErrorKind>,
    pub error_message: Option<String>,
}

impl Status {
    /// Nothing has been asked for yet.
    pub fn idle() -> Self {
        Status {
            state: DownloadState::Idle,
            downloaded_bytes: 0,
            total_bytes: 0,
            percent: 0.0,
            file: String::new(),
            error_kind: None,
            error_message: None,
        }
    }

    /// Whether a transfer is running, which is what makes the slot busy.
    pub fn is_running(&self) -> bool {
        self.state == DownloadState::Downloading || self.state == DownloadState::Verifying
    }
}

impl Default for Status {
    fn default() -> Self {
        Status::idle()
    }
}

impl From<&Progress> for Status {
    fn from(progress: &Progress) -> Self {
        Status {
            state: progress.state,
            downloaded_bytes: progress.downloaded_bytes,
            total_bytes: progress.total_bytes,
            percent: progress.percent,
            file: progress.file.clone(),
            error_kind: None,
            error_message: None,
        }
    }
}

/// Owns the one in-process download slot.
#[derive(Debug, Default)]
pub struct DownloadManager {
    status: Arc<Mutex<Status>>,
    slot: Mutex<Slot>,
}

#[derive(Debug, Default)]
struct Slot {
    cancel: Option<crate::transport::CancelToken>,
    worker: Option<JoinHandle<()>>,
}

impl DownloadManager {
    pub fn new() -> Self {
        DownloadManager::default()
    }

    /// Run `download` on a background thread unless one is already running.
    ///
    /// Either way the answer is the current status, so a caller does not have
    /// to know which of the two happened.
    pub fn start(&self, mut download: Download) -> Status {
        let mut slot = lock(&self.slot);

        {
            let mut status = lock(&self.status);
            if status.is_running() {
                return status.clone();
            }
            *status = Status {
                state: DownloadState::Downloading,
                total_bytes: download.total_bytes(),
                ..Status::idle()
            };
        }

        // The previous worker has already finished -- the status said so above
        // -- so this only reaps its handle.
        if let Some(worker) = slot.worker.take() {
            let _ = worker.join();
        }
        slot.cancel = Some(download.cancel_token());

        let status = Arc::clone(&self.status);
        let worker = std::thread::Builder::new()
            .name("fetcher-download".to_string())
            .spawn(move || {
                let outcome = download.start(
                    &mut |progress| *lock(&status) = Status::from(progress),
                    DEFAULT_PROGRESS_INTERVAL,
                );
                if let Err(error) = outcome {
                    // The terminal progress event has already reported the
                    // error state; this is what attributes it.
                    let failure = error.failure();
                    let mut status = lock(&status);
                    status.state = DownloadState::Error;
                    status.error_kind = Some(failure.kind);
                    status.error_message = Some(failure.message);
                }
            })
            .expect("spawn download thread");
        slot.worker = Some(worker);

        drop(slot);
        self.status()
    }

    /// Ask the running transfer to stop, and report where it had got to.
    pub fn cancel(&self) -> Status {
        if let Some(token) = &lock(&self.slot).cancel {
            token.cancel();
        }
        self.status()
    }

    pub fn status(&self) -> Status {
        lock(&self.status).clone()
    }

    /// Block until the running transfer has finished.
    ///
    /// For a caller with nothing else to do -- a CLI subcommand, a test -- and
    /// for shutdown. Polling [`DownloadManager::status`] is what a UI does
    /// instead.
    pub fn wait(&self) {
        let worker = lock(&self.slot).worker.take();
        if let Some(worker) = worker {
            let _ = worker.join();
        }
    }
}

/// A poisoned lock means a previous caller panicked while holding it. The data
/// behind it is a status snapshot, so refusing every later poll would be worse
/// than reading a stale one.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fakes::FakeTransport;
    use crate::manifest::Payload;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};

    const URL: &str = "https://huggingface.co/org/repo/resolve/rev/model.bin";

    fn payload(content: &[u8]) -> Payload {
        Payload::file(
            "model",
            "model.bin",
            URL,
            content.len() as u64,
            &crate::fakes::sha256_of(content),
        )
    }

    /// Wait for `condition`, failing rather than hanging if it never holds.
    fn until(what: &str, condition: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if condition() {
                return;
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        panic!("timed out waiting for {what}");
    }

    #[test]
    fn an_untouched_manager_reports_idle() {
        let manager = DownloadManager::new();
        let status = manager.status();
        assert_eq!(status.state, DownloadState::Idle);
        assert_eq!(status.total_bytes, 0);
    }

    #[test]
    fn a_finished_transfer_reports_complete_with_every_byte_accounted_for() {
        let dir = tempfile::tempdir().unwrap();
        let content = vec![b'a'; 1000];
        let transport = FakeTransport::new(&[("model.bin", &content)]);
        let download =
            Download::new(dir.path(), vec![payload(&content)], Box::new(transport)).unwrap();

        let manager = DownloadManager::new();
        manager.start(download);
        manager.wait();

        let status = manager.status();
        assert_eq!(status.state, DownloadState::Complete);
        assert_eq!(status.downloaded_bytes, 1000);
        assert_eq!(status.percent, 100.0);
        assert!(crate::is_installed(&dir.path().join("model.bin")));
    }

    #[test]
    fn a_second_request_gets_the_running_transfers_status_and_starts_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let content = vec![b'b'; 1000];
        let gate = Arc::new(AtomicBool::new(true));
        let first = FakeTransport::new(&[("model.bin", &content)]).gated(Arc::clone(&gate));
        let calls = first.call_counter();
        let download = Download::new(dir.path(), vec![payload(&content)], Box::new(first)).unwrap();

        let manager = DownloadManager::new();
        manager.start(download);
        until("the transfer to reach the gate", || {
            calls.load(Ordering::SeqCst) == 1
        });

        // A second attempt while the first is held open at the gate.
        let second = FakeTransport::new(&[("model.bin", &content)]);
        let second_calls = second.call_counter();
        let queued = Download::new(dir.path(), vec![payload(&content)], Box::new(second)).unwrap();
        let status = manager.start(queued);

        assert_eq!(status.state, DownloadState::Downloading);
        assert_eq!(
            second_calls.load(Ordering::SeqCst),
            0,
            "a second request must never start a parallel transfer"
        );

        gate.store(false, Ordering::SeqCst);
        manager.wait();
        assert_eq!(manager.status().state, DownloadState::Complete);
    }

    #[test]
    fn cancelling_reports_cancelled_rather_than_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let content = vec![b'c'; 4000];
        let gate = Arc::new(AtomicBool::new(true));
        let transport = FakeTransport::new(&[("model.bin", &content)])
            .with_chunk_size(10)
            .gated(Arc::clone(&gate));
        let calls = transport.call_counter();
        let download =
            Download::new(dir.path(), vec![payload(&content)], Box::new(transport)).unwrap();

        let manager = DownloadManager::new();
        manager.start(download);
        until("the transfer to reach the gate", || {
            calls.load(Ordering::SeqCst) == 1
        });
        manager.cancel();
        gate.store(false, Ordering::SeqCst);
        manager.wait();

        let status = manager.status();
        assert_eq!(status.state, DownloadState::Cancelled);
        assert!(status.error_kind.is_none(), "a cancel is not a failure");
        assert!(!crate::is_installed(&dir.path().join("model.bin")));
    }

    #[test]
    fn a_failed_transfer_reports_the_attributed_error_to_a_poller() {
        let dir = tempfile::tempdir().unwrap();
        let content = vec![b'd'; 500];
        // The pin says one thing, the bytes say another.
        let payload = Payload::file(
            "model",
            "model.bin",
            URL,
            content.len() as u64,
            &"0".repeat(64),
        );
        let transport = FakeTransport::new(&[("model.bin", &content)]);
        let download = Download::new(dir.path(), vec![payload], Box::new(transport)).unwrap();

        let manager = DownloadManager::new();
        manager.start(download);
        manager.wait();

        let status = manager.status();
        assert_eq!(status.state, DownloadState::Error);
        assert_eq!(status.error_kind, Some(ErrorKind::Internal));
        assert!(status
            .error_message
            .as_deref()
            .unwrap_or_default()
            .contains("digest mismatch"));
    }

    #[test]
    fn the_slot_is_reusable_once_a_transfer_has_ended() {
        let dir = tempfile::tempdir().unwrap();
        let content = vec![b'e'; 300];
        let manager = DownloadManager::new();

        for _ in 0..2 {
            let transport = FakeTransport::new(&[("model.bin", &content)]);
            let download =
                Download::new(dir.path(), vec![payload(&content)], Box::new(transport)).unwrap();
            manager.start(download);
            manager.wait();
            assert_eq!(manager.status().state, DownloadState::Complete);
        }
    }
}
