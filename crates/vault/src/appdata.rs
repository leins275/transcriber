//! Application-data directory as a concept distinct from the vault (FR-16).
//!
//! Owned by T7. The vault holds only meeting artifacts — no models, no
//! logs, no databases, no config. This module resolves the default
//! `%LOCALAPPDATA%\<AppName>\` location and creates nothing; the concrete
//! install location is F4's decision.
//!
//! This module deliberately does not call [`crate::paths`] — that module
//! runs concurrently in the same wave, and the app-name validation here is
//! a much smaller, local rule than full vault-path containment.

use std::path::PathBuf;

use crate::error::VaultError;

/// The default application name used when the caller has no other
/// preference. Valid by the same rule [`app_data_dir`] applies to any
/// other app name.
pub const DEFAULT_APP_NAME: &str = "Transcriber";

/// Resolve the application-data directory for `app_name`, without creating
/// it (FR-16).
///
/// Reads `%LOCALAPPDATA%` and joins `app_name` onto it. Returns
/// [`VaultError::AppDataUnavailable`] if the environment variable is
/// unset, or if `app_name` is not a single safe path component (empty, or
/// containing `..`, `\`, `/` or `:`). Never touches the filesystem beyond
/// reading the environment.
pub fn app_data_dir(app_name: &str) -> Result<PathBuf, VaultError> {
    validate_app_name(app_name)?;

    let local_appdata = std::env::var_os("LOCALAPPDATA").ok_or(VaultError::AppDataUnavailable)?;

    Ok(PathBuf::from(local_appdata).join(app_name))
}

/// Validate `app_name` as a single, safe path component.
///
/// This is deliberately a local, minimal check — not a delegation to
/// `paths::contained_child` (see the module docs) — since an app name has
/// none of the vault's separator- or drive-prefix concerns to worry about
/// beyond staying a single, unremarkable component.
fn validate_app_name(app_name: &str) -> Result<(), VaultError> {
    if app_name.is_empty()
        || app_name.contains("..")
        || app_name.contains('\\')
        || app_name.contains('/')
        || app_name.contains(':')
    {
        return Err(VaultError::AppDataUnavailable);
    }
    Ok(())
}
