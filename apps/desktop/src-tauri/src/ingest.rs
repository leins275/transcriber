//! Wrapper over F1's vault crate, off the UI thread (FR-9, FR-10).
//!
//! This module owns no naming, date-parsing or path-joining rule of its
//! own — every one of those decisions is F1's (`crates/vault`). This file
//! only: (1) refuses a directory or an unsupported extension before any
//! vault call, so a `.txt` (or a directory) drop never even opens the
//! vault (FR-6); (2) calls F1's real API (`vault::Vault::open` /
//! `Vault::ingest`) and translates its typed result into this crate's
//! `IngestOutcome` / `AppError`; (3) re-verifies, with this app's own
//! `paths::ensure_inside`, that every path F1 handed back resolves inside
//! the configured meetings root before treating the file as ingested
//! (FR-11, defense in depth); (4) runs that blocking work under
//! `tokio::task::spawn_blocking` so a multi-gigabyte copy never freezes the
//! UI thread (FR-10, NFR-2).
//!
//! ## Why `source` itself is never containment-checked against the root
//!
//! FR-4 requires accepting a drop from anywhere on disk (a Downloads
//! folder, a USB drive, a network share) — `source` is never expected to
//! already live inside the meetings root, so `paths::ensure_inside` is not
//! applied to it. FR-11's "every filesystem path derived from a dropped
//! file" instead means the *destination* F1 computes from the dropped
//! file's classified name, which is what gets verified here. F1 already
//! enforces this itself before creating a single directory
//! (`VaultError::PathEscapesVault`, mapped below) — in practice this path
//! is unreachable through the public classification surface, because
//! `vault::title::validate` and the project-code pattern both reject every
//! path-separator character before containment is ever considered (see
//! `crates/vault/src/error.rs`'s `Rejection` docs) — so the check here is a
//! second, independent line of defense against a path-joining bug in
//! either crate, not a scenario this app can trigger through F1's real API.

use std::path::{Path, PathBuf};

use vault::{Classification, CollisionOutcome, Vault, VaultError};

use crate::error::AppError;
use crate::paths;

/// The result of one successful ingest, mirroring F1's [`vault::Ingested`]
/// but expressed in this crate's own vocabulary so callers (T10's job
/// pipeline) never need to depend on `vault` directly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestOutcome {
    /// Whether the recording matched F1's naming convention, and the
    /// parsed components or F1's rejection reason (FR-9).
    pub classification: Classification,
    /// The absolute meeting folder the recording (and later artifacts)
    /// live in. Exists at return time.
    pub meeting_dir: PathBuf,
    /// The absolute path to the recording inside `meeting_dir`.
    pub source_dest: PathBuf,
    /// How a destination-name collision, if any, was resolved (FR-9): a
    /// suffixed folder or a no-op duplicate re-drop, never a silent
    /// overwrite of an existing `source.*`.
    pub collision: CollisionOutcome,
}

/// Ingests `source` into the vault rooted at `root`, off the UI thread
/// (FR-10, NFR-2).
///
/// `source` is checked with `paths::classify_drop` (directory / extension)
/// before F1's vault crate is consulted at all (FR-6); the destination F1
/// computes is checked with `paths::ensure_inside` before this call
/// reports success (FR-11). A drag-drop payload is untrusted input and a
/// file dialog result is not validation.
pub async fn ingest(root: &Path, source: &Path) -> Result<IngestOutcome, AppError> {
    let root = root.to_path_buf();
    let source = source.to_path_buf();
    match tokio::task::spawn_blocking(move || ingest_blocking(&root, &source)).await {
        Ok(result) => result,
        Err(join_err) => Err(AppError::internal(format!(
            "ingest task panicked: {join_err}"
        ))),
    }
}

/// The synchronous core [`ingest`] runs under `spawn_blocking`, exercised
/// directly by this module's tests against a real `tempfile` vault root.
fn ingest_blocking(root: &Path, source: &Path) -> Result<IngestOutcome, AppError> {
    // FR-6: reject a directory or an unsupported extension before any
    // vault call, so a `.txt` (or a directory) drop never opens the vault
    // at all.
    paths::classify_drop(source, paths::supported_extensions())?;

    let vault = Vault::open(root).map_err(vault_error_to_app_error)?;
    let ingested = vault.ingest(source).map_err(vault_error_to_app_error)?;

    // FR-11 defense in depth: re-verify, using this app's own
    // `paths::ensure_inside`, that the destination F1 computed from the
    // dropped file's classified name actually resolves inside the
    // configured meetings root before this app ever treats the file as
    // landed. See the module docs for why F1's own equivalent check
    // cannot be exercised through the real classification API.
    paths::ensure_inside(root, &ingested.meeting_dir)?;

    Ok(IngestOutcome {
        classification: ingested.classification,
        meeting_dir: ingested.meeting_dir,
        source_dest: ingested.source_path,
        collision: ingested.collision,
    })
}

/// Translates one of F1's typed [`VaultError`] variants into this crate's
/// [`AppError`].
///
/// Deliberately has no wildcard arm: adding a variant to `VaultError`
/// without adding it here is a compile error, not a silently swallowed
/// case (FR-9's "no re-derived vault rule" acceptance bullet extends to
/// "no re-derived vault error taxonomy" either).
pub(crate) fn vault_error_to_app_error(err: VaultError) -> AppError {
    let message = err.to_string();
    match err {
        VaultError::RootIsNotADirectory => AppError::vault(message),
        VaultError::SourceMissing => AppError::not_a_file(message),
        VaultError::SourceNotAFile => AppError::not_a_file(message),
        VaultError::UnsupportedMediaType { .. } => AppError::unsupported_extension(message),
        VaultError::PathEscapesVault => AppError::outside_root(message),
        VaultError::PathTooLong { .. } => AppError::vault(message),
        VaultError::SuffixLimitExceeded => AppError::collision(message),
        VaultError::AppDataUnavailable => AppError::vault(message),
        // The three `vault::manage` variants (rename/re-file/delete of an
        // already-ingested meeting). The first two are the caller's
        // request being wrong -- an id that does not name a meeting
        // folder, or a project/date/title that fails the naming rules --
        // so they map to `invalid_argument`, the same kind `reveal_job`
        // uses for an id it cannot resolve. A recycle-bin refusal is an
        // environment failure, not a bad request, so it maps to `io`.
        VaultError::NotAMeetingDirectory => AppError::invalid_argument(message),
        VaultError::InvalidMeetingName { .. } => AppError::invalid_argument(message),
        VaultError::TrashUnavailable { .. } => AppError::io(message),
        VaultError::Io { .. } => AppError::io(message),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn correctly_named_recording_lands_at_project_date_title_source(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&source, b"recording bytes")?;

        let outcome = ingest_blocking(root.path(), &source)?;

        assert!(matches!(
            outcome.classification,
            Classification::Sorted { ref project, ref date, ref title }
                if project == "ELS" && date == "260812" && title == "Security issue"
        ));
        assert!(outcome.meeting_dir.is_absolute());
        assert!(outcome.meeting_dir.is_dir());
        assert!(outcome.source_dest.is_absolute());
        assert!(outcome.source_dest.is_file());
        assert!(outcome
            .meeting_dir
            .ends_with("ELS/260812 - Security issue".replace('/', std::path::MAIN_SEPARATOR_STR)));
        assert!(outcome.source_dest.ends_with("source.mp4"));
        assert_eq!(outcome.collision, CollisionOutcome::Fresh);
        Ok(())
    }

    #[test]
    fn badly_named_recording_lands_under_unsorted_with_reason_surfaced(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("random meeting.mp4");
        fs::write(&source, b"recording bytes")?;

        let outcome = ingest_blocking(root.path(), &source)?;

        match outcome.classification {
            Classification::Unsorted { reason } => {
                // F1's rejection reason is surfaced verbatim as the job
                // message (FR-9) — not re-derived or re-worded here.
                assert!(!reason.to_string().is_empty());
            }
            Classification::Sorted { .. } => panic!("expected an unsorted classification"),
        }
        assert!(outcome
            .meeting_dir
            .to_string_lossy()
            .contains(vault::UNSORTED_DIR_NAME));
        assert!(outcome.source_dest.is_file());
        Ok(())
    }

    #[test]
    fn text_file_is_refused_before_any_vault_call() -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("notes.txt");
        fs::write(&source, b"not a recording")?;

        let err = ingest_blocking(root.path(), &source).expect_err("must be refused");

        assert_eq!(err.kind(), crate::error::ErrorKind::UnsupportedExtension);
        // The vault was never opened: F1's `Vault::open` would have
        // created `<root>/unsorted/` unconditionally.
        assert!(!root.path().join("unsorted").exists());
        Ok(())
    }

    #[test]
    fn directory_is_refused_before_any_vault_call() -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("a-directory");
        fs::create_dir(&source)?;

        let err = ingest_blocking(root.path(), &source).expect_err("must be refused");

        assert_eq!(err.kind(), crate::error::ErrorKind::NotAFile);
        assert!(!root.path().join("unsorted").exists());
        Ok(())
    }

    #[test]
    fn a_source_located_outside_the_meetings_root_still_ingests_successfully(
    ) -> Result<(), Box<dyn std::error::Error>> {
        // FR-4 requires accepting a drop from anywhere on disk. `source`
        // lives in a tempdir entirely unrelated to `root` here (as it
        // always does in real use — a user's Downloads folder is never
        // inside the vault) and must not be rejected on that basis alone.
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&source, b"recording bytes")?;

        let outcome = ingest_blocking(root.path(), &source)?;

        assert!(outcome.source_dest.is_file());
        Ok(())
    }

    #[test]
    fn the_returned_destination_resolves_inside_the_meetings_root(
    ) -> Result<(), Box<dyn std::error::Error>> {
        // FR-11 defense in depth: the destination F1 computed from the
        // dropped file's classified name is independently re-verified by
        // this module against `paths::ensure_inside` — the same
        // containment helper T4's own exhaustive test suite covers for
        // escape attempts (`..`, another drive, a UNC path, a `\\?\`
        // device path, a mere string-prefix match).
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&source, b"recording bytes")?;

        let outcome = ingest_blocking(root.path(), &source)?;

        crate::paths::ensure_inside(root.path(), &outcome.meeting_dir)
            .expect("ingest_blocking's own destination must already be contained");
        Ok(())
    }

    #[test]
    fn destination_collision_is_suffixed_and_never_overwrites_the_existing_source(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;

        let first_source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&first_source, b"AAAA")?;
        let first = ingest_blocking(root.path(), &first_source)?;
        assert_eq!(first.collision, CollisionOutcome::Fresh);

        // A different recording that happens to classify to the same
        // project/date/title, dropped a second time.
        let second_source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&second_source, b"BBBBBBBB")?;
        let second = ingest_blocking(root.path(), &second_source)?;

        assert_eq!(second.collision, CollisionOutcome::SuffixedFolder(2));
        assert_ne!(first.meeting_dir, second.meeting_dir);

        // The first recording's `source.*` was never overwritten.
        assert_eq!(fs::read(&first.source_dest)?, b"AAAA");
        assert_eq!(fs::read(&second.source_dest)?, b"BBBBBBBB");
        Ok(())
    }

    #[test]
    fn vault_error_variants_translate_through_an_exhaustive_match(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let downloads = tempdir()?;
        // A vault "root" that is actually a plain file: `Vault::open`
        // reports `VaultError::RootIsNotADirectory`, exercising this
        // module's translation match without a catch-all arm swallowing
        // it.
        let fake_root = downloads.path().join("root.mp4");
        fs::write(&fake_root, b"not a directory")?;

        let err = ingest_blocking(&fake_root, &fake_root).expect_err("must be refused");

        assert_eq!(err.kind(), crate::error::ErrorKind::Vault);
        Ok(())
    }

    #[test]
    fn async_ingest_wraps_the_blocking_call_off_the_runtime(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let root = tempdir()?;
        let downloads = tempdir()?;
        let source = downloads.path().join("ELS - 260812 - Security issue.mp4");
        fs::write(&source, b"recording bytes")?;

        let runtime = tokio::runtime::Runtime::new()?;
        let outcome = runtime.block_on(ingest(root.path(), &source))?;

        assert!(outcome.source_dest.is_file());
        Ok(())
    }
}
