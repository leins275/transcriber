//! The model downloads the app offers, and their status.
//!
//! Two slots, kept independent because they are two decisions a user makes at
//! different times: the speech model is needed before anything works at all,
//! and the assistant is tens of gigabytes that many sessions never need. The
//! Python service had the same split for the same reason.
//!
//! Where the bytes come from and how they are verified belongs to
//! [`fetcher`]; what this adds is which payloads each slot is made of and
//! where they land.

use std::path::PathBuf;
use std::sync::Arc;

use fetcher::manifest::pinned;
use fetcher::{Download, DownloadManager, HttpTransport, Payload, Status};
// Re-exported so a caller can map a status without depending on the fetcher
// crate directly: the download slots are this module's contract, not that
// crate's.
pub use fetcher::DownloadState;

use crate::config::Config;

/// Which download a request is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Slot {
    /// The speech model and the VAD model that goes with it.
    ///
    /// One slot rather than two: whisper without the VAD model transcribes
    /// the silence between utterances and invents words there, so a machine
    /// with one and not the other is in a worse state than one with neither.
    Speech,
    /// The GGUF the assistant runs on.
    Assistant,
}

/// The download slots, each with its own transfer.
pub struct Downloads {
    speech: Arc<DownloadManager>,
    assistant: Arc<DownloadManager>,
    config: Config,
}

impl Downloads {
    pub fn new(config: Config) -> Self {
        Downloads {
            speech: Arc::new(DownloadManager::new()),
            assistant: Arc::new(DownloadManager::new()),
            config,
        }
    }

    fn manager(&self, slot: Slot) -> &DownloadManager {
        match slot {
            Slot::Speech => &self.speech,
            Slot::Assistant => &self.assistant,
        }
    }

    /// Where a slot's files are installed.
    fn destination(&self, slot: Slot) -> PathBuf {
        match slot {
            Slot::Speech => self.config.model_path.join("whisper"),
            Slot::Assistant => self.config.llm_model_path.clone(),
        }
    }

    /// What a slot is made of.
    ///
    /// Both entries come from the compiled-in pin table; a name missing from
    /// it is a build mistake, and an empty payload list is refused by the
    /// downloader rather than silently succeeding.
    fn payloads(&self, slot: Slot) -> Vec<Payload> {
        let names: &[&str] = match slot {
            Slot::Speech => &["whisper-large-v3", "whisper-vad"],
            Slot::Assistant => &["llm-qwen3.6-35b-a3b-q4-k-m"],
        };
        names.iter().filter_map(|name| pinned(name)).collect()
    }

    /// Start a slot's transfer, or return the running one's status.
    pub fn start(&self, slot: Slot) -> Status {
        let download = HttpTransport::new().and_then(|transport| {
            Download::new(
                self.destination(slot),
                self.payloads(slot),
                Box::new(transport),
            )
        });
        match download {
            Ok(download) => self.manager(slot).start(download),
            // A refused payload set -- an off-allowlist URL, an empty slot --
            // is reported through the same status the UI already polls,
            // rather than as an error nothing is listening for.
            Err(err) => Status {
                state: DownloadState::Error,
                error_kind: Some(err.kind()),
                error_message: Some(err.to_string()),
                ..Status::idle()
            },
        }
    }

    /// The live status, including "already installed" for a slot that is
    /// complete on disk from a previous run.
    pub fn status(&self, slot: Slot) -> Status {
        let live = self.manager(slot).status();
        if live.state == DownloadState::Idle && self.is_installed(slot) {
            // Nothing is running and everything is on disk: a second launch
            // must not offer to download what the first one already fetched.
            return Status {
                state: DownloadState::Complete,
                percent: 100.0,
                ..Status::idle()
            };
        }
        live
    }

    pub fn cancel(&self, slot: Slot) -> Status {
        self.manager(slot).cancel()
    }

    /// Whether every payload in the slot is present and verified.
    pub fn is_installed(&self, slot: Slot) -> bool {
        let destination = self.destination(slot);
        let payloads = self.payloads(slot);
        !payloads.is_empty()
            && payloads
                .iter()
                .all(|payload| fetcher::is_installed(&destination.join(&payload.file_name)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn downloads_in(dir: &std::path::Path) -> Downloads {
        let mut env = crate::config::Env::new();
        env.insert("TRANSCRIBER_APP_DIR".to_string(), dir.display().to_string());
        Downloads::new(Config::load(None, &env).expect("config"))
    }

    #[test]
    fn each_slot_lands_in_the_directory_its_engine_reads_from() {
        let dir = tempfile::tempdir().unwrap();
        let downloads = downloads_in(dir.path());
        assert_eq!(
            downloads.destination(Slot::Speech),
            dir.path().join("models/whisper")
        );
        assert_eq!(
            downloads.destination(Slot::Assistant),
            dir.path().join("models/llm")
        );
    }

    #[test]
    fn the_speech_slot_carries_the_vad_model_too() {
        // Without it whisper transcribes silence; a machine with the weights
        // and no VAD is worse off than one with neither.
        let dir = tempfile::tempdir().unwrap();
        let payloads = downloads_in(dir.path()).payloads(Slot::Speech);
        assert_eq!(payloads.len(), 2);
        assert!(payloads.iter().any(|p| p.file_name.contains("large-v3")));
        assert!(payloads.iter().any(|p| p.file_name.contains("silero")));
    }

    #[test]
    fn every_slots_payloads_resolve_from_the_pin_table() {
        // A renamed pin would otherwise show up as an empty download that
        // reports success without fetching anything.
        let dir = tempfile::tempdir().unwrap();
        let downloads = downloads_in(dir.path());
        assert!(!downloads.payloads(Slot::Speech).is_empty());
        assert!(!downloads.payloads(Slot::Assistant).is_empty());
    }

    #[test]
    fn a_slot_is_installed_only_when_every_payload_is() {
        let dir = tempfile::tempdir().unwrap();
        let downloads = downloads_in(dir.path());
        assert!(!downloads.is_installed(Slot::Speech));

        let destination = downloads.destination(Slot::Speech);
        std::fs::create_dir_all(&destination).unwrap();
        let payloads = downloads.payloads(Slot::Speech);

        // One of the two present is not the slot being ready.
        let first = destination.join(&payloads[0].file_name);
        std::fs::write(&first, b"weights").unwrap();
        std::fs::write(fetcher::ready_marker(&first), b"").unwrap();
        assert!(!downloads.is_installed(Slot::Speech));

        let second = destination.join(&payloads[1].file_name);
        std::fs::write(&second, b"vad").unwrap();
        std::fs::write(fetcher::ready_marker(&second), b"").unwrap();
        assert!(downloads.is_installed(Slot::Speech));
    }

    #[test]
    fn an_installed_slot_reports_complete_without_a_transfer() {
        // What the settings page reads on a second launch: the files are
        // there, and nothing is running.
        let dir = tempfile::tempdir().unwrap();
        let downloads = downloads_in(dir.path());
        assert_eq!(
            downloads.status(Slot::Assistant).state,
            fetcher::DownloadState::Idle
        );

        let destination = downloads.destination(Slot::Assistant);
        std::fs::create_dir_all(&destination).unwrap();
        for payload in downloads.payloads(Slot::Assistant) {
            let path = destination.join(&payload.file_name);
            std::fs::write(&path, b"gguf").unwrap();
            std::fs::write(fetcher::ready_marker(&path), b"").unwrap();
        }
        assert_eq!(
            downloads.status(Slot::Assistant).state,
            fetcher::DownloadState::Complete
        );
    }
}
