#![forbid(unsafe_code)]
#![deny(missing_docs)]

//! # vault
//!
//! A vault domain library that turns a dropped meeting recording into a
//! well-formed place on disk.
//!
//! ## Naming convention
//!
//! A source filename is expected to match:
//!
//! ```text
//! <Project code> - <date> - <Title>.<ext>
//! ```
//!
//! split on the **first two** occurrences of the separator `" - "` (space,
//! hyphen, space), so a title may itself contain `" - "`. `<Project code>`
//! matches `^[A-Z][A-Z0-9]{1,9}$` (case-normalized to uppercase), `<date>`
//! is exactly six digits in `YYMMDD` form denoting a real calendar date,
//! `<Title>` is any non-empty, Windows-legal string that is not a reserved
//! device name, and `<ext>` is one of the ten recording media extensions:
//! `mp4`, `mkv`, `mov`, `webm`, `avi`, `m4a`, `mp3`, `wav`, `flac`, `ogg`.
//!
//! A filename that matches the convention is a *sorted* recording. Any
//! other reason for non-conformance (a bad code, a bad date, a bad title,
//! too few separators) yields an *unsorted* recording — routed to
//! `unsorted/`, never rejected outright, because every media file must
//! land somewhere. Only an unsupported extension is rejected outright.
//!
//! ## On-disk layout
//!
//! ```text
//! <vault root>/
//!   <PROJECT>/
//!     <date> - <Title>/
//!       source.<ext>
//!   unsorted/
//!     <YYMMDD of ingest> - <original stem>/
//!       source.<ext>
//! ```
//!
//! Every ingested recording — sorted or unsorted — gets its own folder, so
//! that later artifacts (a transcript, eventually a summary) can be
//! written next to the source. `source.*`, `transcript.json`, `summary.md`
//! and `unsorted` are reserved names owned by the vault contract.
//!
//! ## Usage (the call F3's `#[command]` will make)
//!
//! ```no_run
//! # fn main() -> Result<(), vault::VaultError> {
//! use std::path::Path;
//! use vault::Vault;
//!
//! let vault = Vault::open(Path::new(r"D:\MeetingVault"))?;
//! let ingested = vault.ingest(Path::new(
//!     r"C:\Users\me\Downloads\ELS - 260812 - Security issue.mp4",
//! ))?;
//! // F3 passes `ingested.meeting_dir` straight to F2 as an argument.
//! println!("{}", ingested.meeting_dir.display());
//! # Ok(())
//! # }
//! ```
//!
//! This module declares the crate's module tree and re-exports the curated
//! public API surface below; the individual modules remain reachable
//! directly (`vault::paths`, `vault::date`, …) for anything not curated
//! here.

/// The crate's error vocabulary: `VaultError` (aborts an ingest) and
/// `Rejection` (routes a recording to `unsorted`).
pub mod error;

/// Date component — `YYMMDD` validation and the local-date clock.
pub mod date;

/// Title rules — Windows-illegal characters, trimming, reserved device
/// names.
pub mod title;

/// Project code pattern and the reserved `unsorted` word.
pub mod code;

/// Media extension allowlist.
pub mod media;

/// Path containment, the 260-character cap, and vault name shaping.
pub mod paths;

/// File transfer — rename/copy, verify, delete, roll back.
pub mod transfer;

/// Application-data directory as a concept distinct from the vault.
pub mod appdata;

/// Filename parser — the pure classification entry point.
pub mod parse;

/// Vault layout — init, project-folder reuse, collision policy.
pub mod layout;

/// Ingest orchestration, rollback, and the public API surface.
pub mod ingest;

// Curated public API surface (T10) — the fixed shape F3 links against.
pub use appdata::app_data_dir;
pub use error::{Rejection, VaultError};
pub use ingest::{Classification, CollisionOutcome, Ingested, Vault};
pub use parse::{classify_filename, Classified, ParsedName};
pub use paths::{SOURCE_STEM, SUMMARY_FILE_NAME, TRANSCRIPT_FILE_NAME, UNSORTED_DIR_NAME};
