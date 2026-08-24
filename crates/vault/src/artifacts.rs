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
