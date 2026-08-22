//! Settings (`config.json`) load/save/validate (FR-16..18).
//!
//! Schema v1, as frozen in `specs/tauri-desktop-app/plan.md`'s "Settings
//! contract" section:
//!
//! ```json
//! {
//!   "schema_version": 1,
//!   "meetings_root": "D:\\Meetings",
//!   "service": { "base_url": null },
//!   "model": { "id": "large-v3", "path": "C:\\...\\models" }
//! }
//! ```
//!
//! The config directory is always an injected parameter here — never
//! `%APPDATA%` directly — so these tests (and this module) never touch the
//! real OS app-config directory. The `app_config_dir()` lookup lives in the
//! Tauri wiring (T11); this module only knows about a directory it was
//! handed.
//!
//! Unknown top-level and unknown nested `service`/`model` keys are
//! preserved verbatim across a load -> modify -> save round-trip via
//! `#[serde(flatten)] extra`, so keys F4's installer writes are never
//! destroyed by an app-side settings change.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Map;

use crate::error::AppError;

/// The file name a writability probe creates and immediately deletes inside
/// a candidate meetings root (E2/FR-10: "is writable" must actually be
/// tested, not assumed from `create_dir_all` succeeding on an ancestor).
const WRITE_PROBE_FILE_NAME: &str = ".transcriber_write_probe";

/// The current settings schema version.
pub const SCHEMA_VERSION: u32 = 1;

/// The file name the settings live under, inside the injected config
/// directory.
pub const CONFIG_FILE_NAME: &str = "config.json";

/// `service.*` — currently just the base URL; anything else F4 (or a
/// future version) writes is preserved in `extra`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServiceSettings {
    #[serde(default)]
    pub base_url: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, serde_json::Value>,
}

impl Default for ServiceSettings {
    fn default() -> Self {
        ServiceSettings {
            base_url: None,
            extra: Map::new(),
        }
    }
}

/// `model.*` — passed through to the sidecar; the app never loads a model
/// itself.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelSettings {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, serde_json::Value>,
}

impl Default for ModelSettings {
    fn default() -> Self {
        ModelSettings {
            id: None,
            path: None,
            extra: Map::new(),
        }
    }
}

/// The full settings document, mirroring schema v1 exactly.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Settings {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    #[serde(default)]
    pub meetings_root: Option<String>,
    #[serde(default)]
    pub service: ServiceSettings,
    #[serde(default)]
    pub model: ModelSettings,
    #[serde(flatten)]
    pub extra: Map<String, serde_json::Value>,
}

fn default_schema_version() -> u32 {
    SCHEMA_VERSION
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            schema_version: SCHEMA_VERSION,
            meetings_root: None,
            service: ServiceSettings::default(),
            model: ModelSettings::default(),
            extra: Map::new(),
        }
    }
}

/// The read-only view the frontend's `SettingsView` (minus
/// `supported_extensions`, which comes from `paths.rs` and is assembled by
/// the command handler that owns both) is built from.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SettingsView {
    pub meetings_root: Option<String>,
    pub meetings_root_exists: bool,
    pub service_base_url: Option<String>,
}

/// The path `config.json` lives at, inside `dir`.
pub fn config_path(dir: &Path) -> PathBuf {
    dir.join(CONFIG_FILE_NAME)
}

/// Loads settings from `dir`. An absent file is first-run: returns defaults
/// (`meetings_root: None`) and writes nothing. A malformed file returns a
/// `config`-kind error naming the file; never panics.
pub fn load(dir: &Path) -> Result<Settings, AppError> {
    let path = config_path(dir);
    if !path.exists() {
        return Ok(Settings::default());
    }
    let raw = fs::read_to_string(&path)
        .map_err(|err| AppError::config(format!("could not read {}: {err}", path.display())))?;
    // E16: `Set-Content -Encoding UTF8` (PowerShell 5.1), `Out-File`, .NET
    // `File.WriteAllText(path, s, Encoding.UTF8)` and several NSIS/WiX
    // helpers all prepend a UTF-8 BOM by default -- i.e. what F4's
    // installer will most likely write. Strip it before handing the body to
    // serde so that BOM does not make an otherwise-valid file "malformed".
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(&raw);
    serde_json::from_str(raw).map_err(|err| {
        AppError::config(format!("malformed settings file {}: {err}", path.display()))
    })
}

/// Saves settings to `dir` atomically: writes a temp file in the same
/// directory, then renames it over the real file. Creates `dir` if absent.
pub fn save(dir: &Path, settings: &Settings) -> Result<(), AppError> {
    fs::create_dir_all(dir).map_err(|err| {
        AppError::config(format!(
            "could not create config directory {}: {err}",
            dir.display()
        ))
    })?;
    let body = serde_json::to_string_pretty(settings)
        .map_err(|err| AppError::internal(format!("could not serialize settings: {err}")))?;
    let tmp_path = dir.join(format!("{CONFIG_FILE_NAME}.tmp"));
    fs::write(&tmp_path, body).map_err(|err| {
        AppError::config(format!("could not write {}: {err}", tmp_path.display()))
    })?;
    let final_path = config_path(dir);
    fs::rename(&tmp_path, &final_path).map_err(|err| {
        AppError::config(format!("could not persist {}: {err}", final_path.display()))
    })?;
    Ok(())
}

/// Builds the read-only view from a loaded `Settings`, resolving
/// `meetings_root_exists` against the real filesystem without panicking on
/// a missing or unreadable path.
pub fn settings_view(settings: &Settings) -> SettingsView {
    let meetings_root_exists = settings
        .meetings_root
        .as_deref()
        .map(|root| Path::new(root).is_dir())
        .unwrap_or(false);
    SettingsView {
        meetings_root: settings.meetings_root.clone(),
        meetings_root_exists,
        service_base_url: settings.service.base_url.clone(),
    }
}

/// Whether `candidate` is `app_dir` itself or lies anywhere underneath it,
/// compared case-insensitively (Windows paths, and NTFS is itself
/// case-insensitive) on each path's textual form rather than
/// `Path::canonicalize()`, since `candidate` may not exist yet when this
/// runs. A trailing separator is appended to `app_dir` before the prefix
/// check so a sibling with `app_dir` as a strict string prefix (e.g.
/// `C:\Apps\TranscriberX` against `C:\Apps\Transcriber`) is never a false
/// positive.
fn is_inside(app_dir: &Path, candidate: &Path) -> bool {
    let normalize = |p: &Path| p.to_string_lossy().to_lowercase().replace('/', "\\");
    let app_dir = normalize(app_dir);
    let candidate = normalize(candidate);
    let app_dir_trimmed = app_dir.trim_end_matches('\\');
    candidate == app_dir_trimmed || candidate.starts_with(&format!("{app_dir_trimmed}\\"))
}

/// Creates and immediately deletes a marker file inside `candidate`, proving
/// the installing user can actually write there (E2/FR-10) rather than
/// merely that `create_dir_all` found or made the directory — a directory
/// can exist and be non-writable (e.g. read-only media, a permissions
/// mismatch) with `create_dir_all` still reporting success on an ancestor.
fn probe_writable(candidate: &Path) -> Result<(), AppError> {
    let probe_path = candidate.join(WRITE_PROBE_FILE_NAME);
    fs::write(&probe_path, b"").map_err(|err| {
        AppError::invalid_argument(format!(
            "meetings root is not writable ({}): {err}",
            candidate.display()
        ))
    })?;
    let _ = fs::remove_file(&probe_path);
    Ok(())
}

/// Validates and persists a new meetings-root. Rejects a relative path, an
/// empty string, a path inside the application folder (`app_dir` — FR-10,
/// FR-14: the uninstaller only relocates `models\`/`logs\`/`data\`/
/// `runtime\`, so a vault nested there would be silently destroyed by an
/// uninstall or upgrade), or a path that cannot be created or is not
/// writable; creates the directory if it does not already exist and
/// persists atomically on success.
pub fn set_meetings_root(
    dir: &Path,
    app_dir: &Path,
    settings: &mut Settings,
    path: &str,
) -> Result<(), AppError> {
    if path.trim().is_empty() {
        return Err(AppError::invalid_argument(
            "meetings root path must not be empty",
        ));
    }
    let candidate = Path::new(path);
    if !candidate.is_absolute() {
        return Err(AppError::invalid_argument(format!(
            "meetings root path must be absolute: {path}"
        )));
    }
    if is_inside(app_dir, candidate) {
        return Err(AppError::invalid_argument(format!(
            "meetings root must not be inside the application folder ({}): {path}",
            app_dir.display()
        )));
    }
    fs::create_dir_all(candidate).map_err(|err| {
        AppError::io(format!(
            "could not create meetings root {}: {err}",
            candidate.display()
        ))
    })?;
    probe_writable(candidate)?;
    settings.meetings_root = Some(path.to_string());
    save(dir, settings)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    #[test]
    fn absent_file_loads_first_run_defaults_and_writes_nothing() {
        let dir = tempdir().expect("tempdir");
        let settings = load(dir.path()).expect("load must not fail on absent file");

        assert_eq!(settings.schema_version, SCHEMA_VERSION);
        assert_eq!(settings.meetings_root, None);
        assert_eq!(settings.service.base_url, None);
        assert!(
            !config_path(dir.path()).exists(),
            "load must not write a file on first run"
        );
    }

    #[test]
    fn partial_file_loads_with_defaults_for_the_rest() {
        let dir = tempdir().expect("tempdir");
        fs::write(
            config_path(dir.path()),
            r#"{"schema_version":1,"meetings_root":"D:\\Meetings"}"#,
        )
        .expect("write partial config");

        let settings = load(dir.path()).expect("load must succeed");

        assert_eq!(settings.meetings_root.as_deref(), Some("D:\\Meetings"));
        assert_eq!(settings.service.base_url, None);
        assert_eq!(settings.model.id, None);
        assert_eq!(settings.model.path, None);
    }

    #[test]
    fn unknown_keys_survive_a_load_modify_save_round_trip() {
        let dir = tempdir().expect("tempdir");
        fs::write(
            config_path(dir.path()),
            json!({
                "schema_version": 1,
                "meetings_root": "D:\\Meetings",
                "future_top_level": "keep me",
                "service": {
                    "base_url": null,
                    "future_service_key": 42,
                },
                "model": {
                    "id": "large-v3",
                    "path": "C:\\models",
                    "future_model_key": ["a", "b"],
                },
            })
            .to_string(),
        )
        .expect("write config with unknown keys");

        let mut settings = load(dir.path()).expect("load must succeed");
        assert_eq!(
            settings.extra.get("future_top_level"),
            Some(&json!("keep me"))
        );
        assert_eq!(
            settings.service.extra.get("future_service_key"),
            Some(&json!(42))
        );
        assert_eq!(
            settings.model.extra.get("future_model_key"),
            Some(&json!(["a", "b"]))
        );

        // Modify an unrelated known field, then save.
        settings.meetings_root = Some("D:\\Meetings2".to_string());
        save(dir.path(), &settings).expect("save must succeed");

        let reloaded = load(dir.path()).expect("reload must succeed");
        assert_eq!(reloaded.meetings_root.as_deref(), Some("D:\\Meetings2"));
        assert_eq!(
            reloaded.extra.get("future_top_level"),
            Some(&json!("keep me")),
            "unknown top-level key must survive the round trip"
        );
        assert_eq!(
            reloaded.service.extra.get("future_service_key"),
            Some(&json!(42)),
            "unknown nested service.* key must survive the round trip"
        );
        assert_eq!(
            reloaded.model.extra.get("future_model_key"),
            Some(&json!(["a", "b"])),
            "unknown nested model.* key must survive the round trip"
        );
    }

    #[test]
    fn deleted_meetings_root_reports_not_existing_without_panicking() {
        let dir = tempdir().expect("tempdir");
        let missing_root = dir.path().join("does-not-exist").join("gone");
        fs::write(
            config_path(dir.path()),
            json!({
                "schema_version": 1,
                "meetings_root": missing_root.to_string_lossy(),
            })
            .to_string(),
        )
        .expect("write config");

        let settings = load(dir.path()).expect("load must succeed");
        let view = settings_view(&settings);

        assert_eq!(
            view.meetings_root.as_deref(),
            Some(missing_root.to_string_lossy().as_ref())
        );
        assert!(!view.meetings_root_exists);
    }

    #[test]
    fn malformed_json_returns_config_error_naming_the_file() {
        let dir = tempdir().expect("tempdir");
        fs::write(config_path(dir.path()), "{ not json").expect("write malformed config");

        let err = load(dir.path()).expect_err("malformed JSON must not load");

        assert_eq!(err.kind(), crate::error::ErrorKind::Config);
        assert!(
            err.message().contains("config.json"),
            "message must name the file, got: {}",
            err.message()
        );
    }

    #[test]
    fn a_utf8_bom_before_the_json_is_tolerated_e16_regression() {
        // E16: `Set-Content -Encoding UTF8` (PowerShell 5.1), `Out-File`,
        // .NET `File.WriteAllText(path, s, Encoding.UTF8)` and several
        // NSIS/WiX helpers all prepend a UTF-8 BOM by default -- i.e. what
        // F4's installer will most likely write. `load` must strip it
        // rather than treating the file as malformed and silently
        // discarding the installer's `meetings_root`.
        let dir = tempdir().expect("tempdir");
        let root = dir.path().join("Meetings");
        fs::create_dir_all(&root).expect("create root");
        let body = json!({
            "schema_version": 1,
            "meetings_root": root.to_string_lossy(),
        })
        .to_string();
        let mut bytes = vec![0xEF, 0xBB, 0xBF];
        bytes.extend_from_slice(body.as_bytes());
        fs::write(config_path(dir.path()), bytes).expect("write BOM'd config");

        let settings = load(dir.path()).expect("a leading UTF-8 BOM must not be malformed");
        let view = settings_view(&settings);

        assert_eq!(
            view.meetings_root.as_deref(),
            Some(root.to_string_lossy().as_ref())
        );
    }

    #[test]
    fn set_meetings_root_rejects_a_relative_path() {
        let dir = tempdir().expect("tempdir");
        let app_dir = tempdir().expect("tempdir for app dir");
        let mut settings = Settings::default();

        let err = set_meetings_root(dir.path(), app_dir.path(), &mut settings, "relative\\path")
            .expect_err("relative path must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        assert_eq!(settings.meetings_root, None);
    }

    #[test]
    fn set_meetings_root_rejects_an_empty_string() {
        let dir = tempdir().expect("tempdir");
        let app_dir = tempdir().expect("tempdir for app dir");
        let mut settings = Settings::default();

        let err = set_meetings_root(dir.path(), app_dir.path(), &mut settings, "")
            .expect_err("empty must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
    }

    #[test]
    fn set_meetings_root_rejects_a_path_that_cannot_be_created() {
        let dir = tempdir().expect("tempdir");
        let app_dir = tempdir().expect("tempdir for app dir");
        let mut settings = Settings::default();

        // A regular file in the path where a directory component is
        // needed makes creation impossible.
        let blocker = dir.path().join("blocker-file");
        fs::write(&blocker, b"not a directory").expect("write blocker file");
        let uncreatable = blocker.join("sub-root");

        let err = set_meetings_root(
            dir.path(),
            app_dir.path(),
            &mut settings,
            uncreatable.to_str().expect("valid utf8 path"),
        )
        .expect_err("uncreatable path must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::Io);
        assert_eq!(settings.meetings_root, None);
    }

    #[test]
    fn set_meetings_root_accepts_an_existing_writable_directory_and_persists_atomically() {
        let config_dir = tempdir().expect("tempdir for config");
        let app_dir = tempdir().expect("tempdir for app dir");
        let root_dir = tempdir().expect("tempdir for meetings root");
        let mut settings = Settings::default();

        set_meetings_root(
            config_dir.path(),
            app_dir.path(),
            &mut settings,
            root_dir.path().to_str().expect("valid utf8 path"),
        )
        .expect("existing writable directory must be accepted");

        assert_eq!(
            settings.meetings_root.as_deref(),
            Some(root_dir.path().to_str().expect("valid utf8 path"))
        );

        // No leftover temp file after an atomic rename.
        assert!(!config_dir
            .path()
            .join(format!("{CONFIG_FILE_NAME}.tmp"))
            .exists());

        // The writability probe file must not survive a successful call.
        assert!(!root_dir.path().join(WRITE_PROBE_FILE_NAME).exists());
    }

    #[test]
    fn set_meetings_root_rejects_a_path_inside_the_application_folder() {
        let config_dir = tempdir().expect("tempdir for config");
        let app_dir = tempdir().expect("tempdir for app dir");
        let mut settings = Settings::default();
        let candidate = app_dir.path().join("Meetings");

        let err = set_meetings_root(
            config_dir.path(),
            app_dir.path(),
            &mut settings,
            candidate.to_str().expect("valid utf8 path"),
        )
        .expect_err("a path inside the app folder must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        assert_eq!(settings.meetings_root, None);
        assert!(
            !candidate.exists(),
            "a rejected candidate must not be created on disk"
        );
    }

    #[test]
    fn set_meetings_root_rejects_the_application_folder_itself_case_insensitively() {
        let config_dir = tempdir().expect("tempdir for config");
        let app_dir = tempdir().expect("tempdir for app dir");
        let mut settings = Settings::default();
        // Uppercased relative to the real `app_dir` path, to prove the
        // comparison is case-insensitive (Windows paths).
        let candidate = app_dir.path().to_string_lossy().to_uppercase();

        let err = set_meetings_root(config_dir.path(), app_dir.path(), &mut settings, &candidate)
            .expect_err("the app folder itself must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
    }

    #[test]
    fn set_meetings_root_rejects_a_directory_the_write_probe_cannot_land_in() {
        // Windows' directory `ReadOnly` attribute is cosmetic (Explorer-only)
        // and does not actually block writes underneath it, so it cannot be
        // used to simulate a non-writable directory here. Instead, a
        // subdirectory is pre-created at the exact path the write probe
        // targets: writing a *file* over an existing *directory* fails with
        // "Access is denied" on Windows, deterministically and without
        // needing elevation or ACL manipulation -- exercising the same
        // `probe_writable` failure path a genuinely locked-down ACL would.
        let config_dir = tempdir().expect("tempdir for config");
        let app_dir = tempdir().expect("tempdir for app dir");
        let root_dir = tempdir().expect("tempdir for meetings root");
        fs::create_dir(root_dir.path().join(WRITE_PROBE_FILE_NAME))
            .expect("pre-create a directory at the probe's target path");
        let mut settings = Settings::default();

        let err = set_meetings_root(
            config_dir.path(),
            app_dir.path(),
            &mut settings,
            root_dir.path().to_str().expect("valid utf8 path"),
        )
        .expect_err("a directory whose write probe cannot land must be rejected");

        assert_eq!(err.kind(), crate::error::ErrorKind::InvalidArgument);
        assert_eq!(settings.meetings_root, None);
    }

    #[test]
    fn save_then_load_preserves_the_value_across_a_simulated_restart() {
        let dir = tempdir().expect("tempdir");
        let settings = Settings {
            meetings_root: Some("D:\\Meetings".to_string()),
            service: ServiceSettings {
                base_url: Some("http://127.0.0.1:8756".to_string()),
                ..ServiceSettings::default()
            },
            ..Settings::default()
        };

        save(dir.path(), &settings).expect("save must succeed");

        // Simulate a restart: a brand new load call, no shared state.
        let reloaded = load(dir.path()).expect("reload after restart must succeed");
        assert_eq!(reloaded, settings);
    }
}
