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
//! split on the **first two** occurrences of the separator `-` — the
//! spaces around it are optional, so `ELS-260812-Title.mp4` parses the
//! same as `ELS - 260812 - Title.mp4` — and each part is trimmed of
//! surrounding spaces. Only the first two hyphens act as separators, so a
//! title may itself contain `-`. `<Project code>`
//! matches `^[A-Za-z][A-Za-z0-9]{1,9}$` (case-normalized to uppercase), `<date>`
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
//!       summary.md                      (F2's LLM summary; carries the
//!                                        action items as a section)
//!       export.md + <name>.pdf          (per-recording export)
//!       action items/<slug>/<slug>.md   (legacy: the retired extraction job)
//!       facts/<slug>/<slug>.md          (legacy: the retired facts job)
//!       exports/<YYMMDD>/               (legacy: the old dated export tree)
//!     reports/<YYMMDD>/                 (project-level, F2's LLM jobs)
//!   unsorted/
//!     <YYMMDD of ingest> - <original stem>/
//!       source.<ext>
//! ```
//!
//! Every ingested recording — sorted or unsorted — gets its own folder, so
//! that later artifacts (a transcript, a summary, a per-recording export)
//! can be written next to the source; an unsorted meeting folder takes
//! exactly the same contents as a filed one. `source.*`,
//! `transcript.json`, `summary.md`, `action items` (legacy — the retired
//! extraction job's tree, still reserved), `facts` (legacy — the retired
//! facts job's tree, still reserved), `exports` (legacy — the old dated
//! export tree; exports now land in the meeting folder itself) (all inside
//! a meeting folder), `reports` (project-level) and `unsorted` are
//! reserved names owned by the vault contract.
//!
//! Only the project *level* needs a listing exclusion, since [`list`] never
//! recurses into a meeting folder: `<PROJECT>/reports/`, plus the legacy
//! `<PROJECT>/action items/` and `<PROJECT>/facts/` trees an older build
//! wrote before the per-meeting anchor (kept on disk, no longer read or
//! written).
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

/// Read-only vault listing — a scope extension over this crate's original
/// spec (`specs/meeting-vault-layout/spec.md`'s "Out of scope" section
/// explicitly excluded browsing/listing from the MVP); the operator has
/// since extended the scope for the vault-browser feature. See the module
/// docs for exactly what "read-only" and "two levels" mean here.
pub mod list;

/// Post-ingest meeting management — rename, re-file and delete an existing
/// meeting folder. Like [`list`], a scope extension over this crate's
/// original spec; see the module docs for the containment gate that keeps
/// these the only vault-mutating calls outside ingest.
pub mod manage;

// Curated public API surface (T10) — the fixed shape F3 links against.
pub use appdata::app_data_dir;
pub use error::{Rejection, VaultError};
pub use ingest::{Classification, CollisionOutcome, Ingested, Vault};
pub use list::{list_meetings, MeetingEntry};
pub use manage::{delete_meeting, rename_meeting, MeetingUpdate, ResolvedMeeting};
pub use parse::{classify_filename, Classified, ParsedName};
pub use paths::{
    ACTION_ITEMS_DIR_NAME, EXPORTS_DIR_NAME, FACTS_DIR_NAME, REPORTS_DIR_NAME,
    RESERVED_PROJECT_DIR_NAMES, SOURCE_STEM, SUMMARY_FILE_NAME, TRANSCRIPT_FILE_NAME,
    UNSORTED_DIR_NAME,
};
