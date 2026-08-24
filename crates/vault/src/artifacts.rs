//! The artifact directories (action items, facts) that F2's LLM extraction
//! jobs write into.
//!
//! Nothing here touches the filesystem. The vault owns the reserved
//! directory names (see [`crate::paths`]); this module only maps an
//! extraction kind onto its name so a caller can build the job's output
//! path. The anchor those names hang off is the *meeting folder*
//! (`<meeting>/action items/<slug>/`, `<meeting>/facts/<slug>/`) — an older
//! build wrote them at `<PROJECT>/<kind>/` instead, and those files are
//! never migrated and never deleted, just no longer written to or read by
//! the app's own flows. Enumerating and reading either tree from inside the
//! app was removed together with the project view's artifact and report
//! tabs — the operator reads the vault folder with external tools instead.
//!
//! # Front-matter field contract (mirror)
//!
//! Item `.md` files open with a `---` fenced block of `key: <json value>`
//! lines — JSON is a YAML subset, so external property editors (Obsidian
//! and friends) read it as ordinary YAML front matter. The field set is a
//! cross-language contract, exactly like the directory names in
//! [`crate::paths`].
//!
//! **The Python side owns it.** The source of truth is the module docstring
//! of `services/transcription/src/transcription/artifacts.py` together with
//! the key-set test in `services/transcription/tests/test_llm_jobs.py`,
//! which fails CI on any drift. What follows is a mirror kept in sync by
//! hand; if the two ever disagree, Python wins. Nothing in this crate parses
//! front matter today, but any Rust code that later reads or writes it must
//! use these names verbatim.
//!
//! Every key below is written on every extraction item (action items and
//! facts share one writer, `jobs._extract_sync`):
//!
//! - `type` / `kind` — string, non-null. `type` for action items, `kind`
//!   for facts; the only difference between the two key sets.
//! - `title` — string, non-null.
//! - `archived` — boolean, non-null. Always written `false`; flipped only
//!   by an external editor. An absent key reads as false.
//! - `source_project` — string, nullable. The vault project folder holding
//!   the meeting; `null` when the meeting lives under the reserved
//!   [`crate::paths::UNSORTED_DIR_NAME`] root — never the literal string
//!   `"unsorted"` posing as a project.
//! - `source_meeting` — string, non-null. The meeting folder's name.
//! - `source_recording` — string, nullable. The stored `source.<ext>`
//!   filename; `null` when the meeting folder has none.
//! - `source_date` — string `YYYY-MM-DD`, nullable. Parsed from the
//!   meeting folder's leading `YYMMDD` (the naming contract in
//!   [`crate::paths`]), century fixed at 20xx; `null` when unparseable.
//! - `timestamps` — number array, non-null. Transcript offsets in seconds.
//! - `created` — string, non-null. ISO datetime, UTC.
//! - `model` — string, non-null.
//! - `job_id` — string, non-null.
//! - `screenshots` — string, non-null. The screenshot-capture status value.
//!
//! Two clauses are behaviour rather than fields:
//!
//! - **Unknown keys survive.** Hand-edited front matter — reordered keys,
//!   YAML-quoted strings, extra keys, `archived` flipped to `true` — round-
//!   trips through the Python reader; a non-JSON value degrades to its raw
//!   string and parsing never fails.
//! - **No code path rewrites an existing artifact `.md`.** After its atomic
//!   creation an item file is read-only to this app — this crate included,
//!   which no longer opens artifact files at all. A future mutation feature
//!   must round-trip unknown keys and the body byte-exactly outside the keys
//!   it changes.
//!
//! Neither side acts on `archived`: listings and exports include archived
//! items exactly like unarchived ones. It exists for the operator's
//! external tools.
//!
//! This module has already shrunk to the directory-name mapping above, and
//! a later feature may delete it outright. If it goes, the contract's single
//! home is the Python side named above — this mirror is a convenience, never
//! the authority.

use crate::paths::{ACTION_ITEMS_DIR_NAME, FACTS_DIR_NAME};

/// Which kind of extracted artifact — i.e. which reserved directory name.
///
/// Anchor-neutral on purpose: [`ArtifactKind::dir_name`] names the
/// directory, and the caller decides what to join it onto. Extraction joins
/// it onto the meeting folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactKind {
    /// `action items` — the extracted to-dos.
    ActionItems,
    /// `facts` — the extracted facts and answered questions.
    Facts,
}

impl ArtifactKind {
    /// The reserved on-disk directory name for this kind.
    pub fn dir_name(self) -> &'static str {
        match self {
            ArtifactKind::ActionItems => ACTION_ITEMS_DIR_NAME,
            ArtifactKind::Facts => FACTS_DIR_NAME,
        }
    }
}
