//! A [`Transport`] that moves bytes without a network.
//!
//! Every guarantee this crate makes -- resume from a partial file, verify
//! before installing, stop within a chunk of a cancel, report progress about
//! once a second -- is a property of the code *above* the transport. The Python
//! had this seam for exactly that reason, and its tests ran with no network, no
//! weights and no waiting. So do these.
//!
//! Public rather than `#[cfg(test)]` so the crates that depend on this one can
//! test their own download flows the same way.

use std::collections::HashMap;
use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

use crate::error::FetchError;
use crate::transport::{FetchRequest, Fetched, Transport};

/// The SHA-256 a payload pin would carry for `content`.
pub fn sha256_of(content: &[u8]) -> String {
    Sha256::digest(content)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// A zip archive's bytes, for standing in for a runtime payload.
pub fn zip_bytes(entries: &[(&str, &[u8])]) -> Vec<u8> {
    use zip::write::SimpleFileOptions;

    let mut buffer = std::io::Cursor::new(Vec::new());
    {
        let mut writer = zip::ZipWriter::new(&mut buffer);
        for (name, content) in entries {
            writer
                .start_file(*name, SimpleFileOptions::default())
                .expect("start zip entry");
            writer.write_all(content).expect("write zip entry");
        }
        writer.finish().expect("finish zip");
    }
    buffer.into_inner()
}

#[derive(Debug, Default)]
struct FakeState {
    /// Every offset a transfer was asked to resume from, in order, so a test
    /// can assert that a restart picked up where the last one stopped.
    resume_offsets: Vec<u64>,
    /// Stop this file's transfer once it has written this many bytes,
    /// standing in for a dropped connection.
    interrupt: Option<(String, u64)>,
}

/// Writes pre-baked content in chunks, recording what it was asked to do.
#[derive(Debug)]
pub struct FakeTransport {
    contents: HashMap<String, Vec<u8>>,
    chunk_size: usize,
    calls: Arc<AtomicUsize>,
    gate: Option<Arc<AtomicBool>>,
    state: Mutex<FakeState>,
}

impl FakeTransport {
    /// Serve `contents`, keyed by the destination file name.
    pub fn new(contents: &[(&str, &[u8])]) -> Self {
        FakeTransport {
            contents: contents
                .iter()
                .map(|(name, data)| ((*name).to_string(), data.to_vec()))
                .collect(),
            chunk_size: 10,
            calls: Arc::new(AtomicUsize::new(0)),
            gate: None,
            state: Mutex::new(FakeState::default()),
        }
    }

    /// Move this many bytes between cancellation checks and progress
    /// callbacks. Small values make the throttling and cancellation tests
    /// deterministic.
    pub fn with_chunk_size(mut self, chunk_size: usize) -> Self {
        self.chunk_size = chunk_size;
        self
    }

    /// Hold every transfer at the start while `gate` is set, so a test can
    /// keep one running for as long as it needs to observe the slot.
    pub fn gated(mut self, gate: Arc<AtomicBool>) -> Self {
        self.gate = Some(gate);
        self
    }

    /// Stop `file`'s transfer once it has written `at_bytes`, without setting
    /// the cancel flag -- a dropped connection rather than a deliberate stop.
    pub fn interrupt_at(self, file: &str, at_bytes: u64) -> Self {
        self.state.lock().expect("fake state").interrupt = Some((file.to_string(), at_bytes));
        self
    }

    /// Let an interrupted transfer run to completion on the next attempt.
    pub fn heal(&self) {
        self.state.lock().expect("fake state").interrupt = None;
    }

    /// Every resume offset seen so far, oldest first.
    pub fn resume_offsets(&self) -> Vec<u64> {
        self.state
            .lock()
            .expect("fake state")
            .resume_offsets
            .clone()
    }

    /// How many transfers have been asked for.
    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    /// The same counter [`FakeTransport::calls`] reads, for a test that has
    /// handed the transport away to a background thread.
    pub fn call_counter(&self) -> Arc<AtomicUsize> {
        Arc::clone(&self.calls)
    }

    /// Wait out the gate, if one was installed. Bounded so a mistake in a test
    /// fails it rather than hanging the suite.
    fn wait_for_gate(&self) {
        let Some(gate) = &self.gate else { return };
        let deadline = Instant::now() + Duration::from_secs(30);
        while gate.load(Ordering::SeqCst) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}

/// The content key for a destination: the file name without the
/// `.incomplete` suffix a transfer writes into.
fn content_key(dest: &Path) -> String {
    dest.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
        .trim_end_matches(".incomplete")
        .to_string()
}

impl Transport for FakeTransport {
    fn fetch(&self, request: FetchRequest<'_>) -> Result<Fetched, FetchError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.wait_for_gate();

        let key = content_key(request.dest);
        let data = self.contents.get(&key).ok_or_else(|| FetchError::Status {
            url: request.url.to_string(),
            status: 404,
        })?;

        let interrupt_at = {
            let mut state = self.state.lock().expect("fake state");
            state.resume_offsets.push(request.resume_from);
            state
                .interrupt
                .as_ref()
                .filter(|(file, _)| *file == key)
                .map(|(_, at)| *at)
        };

        let mut file = crate::transport::open_dest(request.dest, request.resume_from > 0)?;
        let mut offset = (request.resume_from as usize).min(data.len());
        let mut written = 0u64;
        while offset < data.len() {
            if request.cancel.is_cancelled() {
                break;
            }
            if interrupt_at.is_some_and(|at| request.resume_from + written >= at) {
                break;
            }
            let end = (offset + self.chunk_size).min(data.len());
            file.write_all(&data[offset..end])
                .map_err(|source| FetchError::io(request.dest, source))?;
            (request.on_chunk)((end - offset) as u64);
            written += (end - offset) as u64;
            offset = end;
        }
        file.flush()
            .map_err(|source| FetchError::io(request.dest, source))?;
        Ok(Fetched::default())
    }
}

/// Lets a test keep hold of the fake -- to heal an interruption, or to read
/// back the offsets it was asked to resume from -- after handing a boxed copy
/// to a [`crate::Download`], which takes ownership of its transport.
impl Transport for Arc<FakeTransport> {
    fn fetch(&self, request: FetchRequest<'_>) -> Result<Fetched, FetchError> {
        (**self).fetch(request)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::CancelToken;

    #[test]
    fn a_fetch_writes_the_pre_baked_content_and_reports_every_chunk() {
        let dir = tempfile::tempdir().unwrap();
        let transport = FakeTransport::new(&[("model.bin", b"abcdefghij")]).with_chunk_size(4);
        let dest = dir.path().join("model.bin.incomplete");
        let mut chunks = Vec::new();

        let mut on_chunk = |n: u64| chunks.push(n);
        transport
            .fetch(FetchRequest {
                url: "https://huggingface.co/x",
                dest: &dest,
                resume_from: 0,
                on_chunk: &mut on_chunk,
                cancel: &CancelToken::new(),
            })
            .unwrap();

        assert_eq!(std::fs::read(&dest).unwrap(), b"abcdefghij");
        assert_eq!(chunks, vec![4, 4, 2]);
        assert_eq!(transport.resume_offsets(), vec![0]);
    }

    #[test]
    fn a_resumed_fetch_appends_only_the_missing_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let transport = FakeTransport::new(&[("model.bin", b"abcdefghij")]);
        let dest = dir.path().join("model.bin.incomplete");
        std::fs::write(&dest, b"abcd").unwrap();

        let mut on_chunk = |_n: u64| {};
        transport
            .fetch(FetchRequest {
                url: "https://huggingface.co/x",
                dest: &dest,
                resume_from: 4,
                on_chunk: &mut on_chunk,
                cancel: &CancelToken::new(),
            })
            .unwrap();

        assert_eq!(std::fs::read(&dest).unwrap(), b"abcdefghij");
    }

    #[test]
    fn zip_bytes_builds_an_archive_the_extractor_can_read() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.zip");
        std::fs::write(&path, zip_bytes(&[("nvidia/bin/x.dll", b"dll")])).unwrap();

        let dest = dir.path().join("out");
        assert_eq!(
            crate::extract::extract_tree(&path, "nvidia/", &dest).unwrap(),
            1
        );
        assert_eq!(
            std::fs::read(dest.join("nvidia/bin/x.dll")).unwrap(),
            b"dll"
        );
    }
}
