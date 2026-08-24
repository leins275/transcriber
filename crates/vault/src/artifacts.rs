//! The project-level artifact directories (action items, facts) that F2's
//! LLM extraction jobs write into.
//!
//! Nothing here touches the filesystem. The vault owns the reserved
//! directory names (see [`crate::paths`]); this module only maps an
//! extraction kind onto its name so a caller can build the job's output
//! path. Enumerating and reading those directories from inside the app was
//! removed together with the project view's artifact and report tabs — the
//! operator reads the vault folder with external tools instead.

use crate::paths::{ACTION_ITEMS_DIR_NAME, FACTS_DIR_NAME};

/// Which project-level artifact directory an extraction job writes into.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactKind {
    /// `<PROJECT>/action items/`
    ActionItems,
    /// `<PROJECT>/facts/`
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
