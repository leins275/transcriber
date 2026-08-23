//! Decoding audio and video, through a bundled `ffmpeg` binary.
//!
//! The Python service decoded with PyAV, which rode in on faster-whisper's
//! dependency tree and bundled the ffmpeg libraries. Nothing in the Rust build
//! brings ffmpeg along, and the two alternatives are not close: linking
//! `ffmpeg-next` means building the ffmpeg libraries on Windows, which is the
//! most painful native dependency available, while a pure-Rust decoder like
//! symphonia handles neither video nor opus -- so it would cover less of the
//! formats a user actually drops in, not more.
//!
//! Running `ffmpeg` as a child process covers every format the vault accepts,
//! costs nothing at build time, and puts a process boundary exactly where the
//! input is least trustworthy: a decoder that crashes on a malformed
//! recording takes its own process down, not the app's. The rule the Python
//! spec called "no external ffmpeg binary" was really "no *system*
//! dependency"; a binary shipped by the installer and addressed by absolute
//! path honours that.

pub mod ffmpeg;

use std::path::Path;

use crate::jobs::CancelToken;

/// 16 kHz mono PCM -- what whisper.cpp consumes, and the only audio shape this
/// engine passes around.
#[derive(Debug, Clone, PartialEq)]
pub struct Pcm {
    pub samples: Vec<f32>,
}

/// The sample rate every whisper model in use here expects.
pub const SAMPLE_RATE: u32 = 16_000;

impl Pcm {
    pub fn duration_sec(&self) -> f64 {
        self.samples.len() as f64 / SAMPLE_RATE as f64
    }

    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }
}

/// Why media could not be decoded.
#[derive(Debug, thiserror::Error)]
pub enum MediaError {
    #[error("cancelled")]
    Cancelled,
    #[error("the bundled ffmpeg binary was not found at {0}")]
    FfmpegMissing(String),
    #[error("failed to decode {path}: {detail}")]
    Decode { path: String, detail: String },
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

impl From<MediaError> for crate::jobs::JobFailure {
    fn from(error: MediaError) -> Self {
        let kind = match error {
            MediaError::Cancelled => wire::ErrorKind::Cancelled,
            // A missing ffmpeg is a broken installation, not a bad recording,
            // and telling the two apart is the difference between "reinstall"
            // and "this file is damaged".
            MediaError::FfmpegMissing(_) => wire::ErrorKind::Internal,
            MediaError::Decode { .. } | MediaError::Io(_) => wire::ErrorKind::AudioDecode,
        };
        crate::jobs::JobFailure::new(kind, error.to_string())
    }
}

/// What the engine needs from a decoder, so tests can substitute one.
pub trait MediaDecoder: Send {
    /// Decode any audio or video file to 16 kHz mono PCM.
    fn decode_pcm(&self, path: &Path, cancel: &CancelToken) -> Result<Pcm, MediaError>;

    /// PNG bytes for the frame at each timestamp, in the order given.
    ///
    /// An audio-only recording is not an error: it yields an empty list, and
    /// the caller writes its items without screenshots.
    fn extract_frames(
        &self,
        path: &Path,
        timestamps: &[f64],
        cancel: &CancelToken,
    ) -> Result<Vec<(f64, Vec<u8>)>, MediaError>;
}
