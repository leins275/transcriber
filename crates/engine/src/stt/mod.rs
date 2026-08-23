//! Speech to text: the whisper.cpp engine and the two passes around it.
//!
//! The order matters and is the same as the Python service's: decode, then
//! re-segment on word timestamps, then filter. Re-segmentation runs first
//! because the filters judge confidence per segment, and a half-minute block
//! spanning several utterances is the wrong unit to judge.

pub mod filters;
pub mod segmentation;
pub mod whisper;

pub use whisper::{SttError, TranscribeOptions, Transcription, WhisperEngine};
