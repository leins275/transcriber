//! On-disk document formats shared by the engine and the desktop app.
//!
//! Everything here is a *contract with files that already exist*: transcripts
//! and artifacts written by the Python service sit in users' vaults today, and
//! the Rust engine that replaces it must produce byte-identical documents and
//! keep reading every historical one. That is why this crate is separate and
//! deliberately dependency-light -- it is the one place the formats are
//! defined, and both sides of the app depend on it rather than restating it.
//!
//! - [`transcript`] -- the `transcript.json` v1 document.
//! - [`artifacts`] -- front matter, slugs, and item folders for derived
//!   knowledge (action items, facts, reports, exports).
//! - [`python_json`] -- serialization that matches Python's `json.dump`
//!   defaults byte for byte.
//! - [`atomic`] -- the temp-file-and-rename write every document uses.
//! - [`error_kind`] -- the failure taxonomy that reaches the ledger and the UI.

pub mod artifacts;
pub mod atomic;
pub mod error_kind;
pub mod python_json;
pub mod transcript;

pub use error_kind::ErrorKind;
pub use transcript::{
    DiarizationInfo, DiarizationStatus, ProviderInfo, Segment, Source, Stats, TranscriptDoc, Word,
    SCHEMA_VERSION, TRANSCRIPT_FILE_NAME,
};
