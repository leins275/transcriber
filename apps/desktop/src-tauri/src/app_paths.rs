//! Resolves the application folder ("app folder" in plan.md's Architecture
//! overview) and every path that hangs off it: the bundled Python runtime,
//! the model directory, and the writable `models\`/`logs\`/`data\`
//! subdirectories the installer's post-install hook creates empty (T7,
//! FR-8).
//!
//! ## Production vs. dev resolution (FR-8, FR-11-as-superseded)
//!
//! In a real install the app folder is simply the directory the running
//! executable lives in (`%LOCALAPPDATA%\Programs\Transcriber\`, Q4-A) — that
//! is also where Tauri's `bundle.resources` mapping
//! (`apps/desktop/src-tauri/tauri.conf.json`: `"resources/pyenv/": "pyenv/"`)
//! lands its payload, i.e. `<app folder>\pyenv\python\python.exe`,
//! `<app folder>\pyenv\site-packages\` and `<app folder>\pyenv\service\` (T4's
//! bake output copied wholesale into `resources/pyenv/` before `tauri
//! build`, T8). This module therefore resolves the bundled interpreter under
//! `pyenv\python\python.exe`, **not** `resources\pyenv\python\python.exe` as
//! plan.md's prose states — that prose predates T6's actual
//! `bundle.resources` target mapping, which nests everything directly under
//! `pyenv/` at the install root rather than under a `resources/` folder in
//! the installed layout. `sidecar.rs`'s tests assert against this real
//! mapping.
//!
//! Under `tauri dev`, the running executable sits under
//! `target/debug/`, which is never a sane place to create `models\`/`logs\`/
//! `data\` or to look for a baked `pyenv\` tree (there isn't one — dev uses
//! `uv run`, see `sidecar::SidecarSpawnConfig::dev`). Setting
//! `TRANSCRIBER_DEV_APP_DIR` overrides [`app_dir`] with an explicit path for
//! that case; [`resolve_app_dir`] is the pure, directly-testable form of the
//! same decision.

use std::path::{Component, Path, PathBuf};

/// Overrides [`app_dir`]'s resolution in development (`tauri dev`), where
/// the running executable's own directory (`target/debug/`) is not a
/// meaningful app folder.
pub const DEV_APP_DIR_ENV: &str = "TRANSCRIBER_DEV_APP_DIR";

/// The model this feature ships by default (spec Q3, operator override:
/// full `large-v3`, GPU-first).
pub const DEFAULT_MODEL_NAME: &str = "faster-whisper-large-v3";

/// Where the resources T6's `bundle.resources` mapping deposits the baked
/// Python runtime, relative to the app folder — see the module docs for why
/// this is `pyenv`, not `resources\pyenv`.
const PYENV_DIR_NAME: &str = "pyenv";

/// Resolves the application folder from the running executable's path,
/// unless `dev_override` names an explicit directory (used by `tauri dev` —
/// see the module docs). Pure and total: falls back to `.` only if `exe_path`
/// somehow has no parent (never true for a real absolute executable path).
pub fn resolve_app_dir(exe_path: &Path, dev_override: Option<&Path>) -> PathBuf {
    if let Some(dir) = dev_override {
        return dir.to_path_buf();
    }
    exe_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Production entry point: resolves the app folder from
/// `std::env::current_exe()`, honouring [`DEV_APP_DIR_ENV`] when set.
/// `current_exe()` failing at all is exceptional enough (a broken process
/// image) that falling back to `.` — same as [`resolve_app_dir`]'s own
/// fallback — is preferable to panicking during startup (NFR-6).
pub fn app_dir() -> PathBuf {
    let dev_override = std::env::var_os(DEV_APP_DIR_ENV).map(PathBuf::from);
    let exe_path = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    resolve_app_dir(&exe_path, dev_override.as_deref())
}

/// `<app folder>\models\` — writable without elevation, created empty by the
/// installer's post-install hook (T7, FR-8).
pub fn models_dir(app_dir: &Path) -> PathBuf {
    app_dir.join("models")
}

/// `<app folder>\logs\` — same contract as [`models_dir`].
pub fn logs_dir(app_dir: &Path) -> PathBuf {
    app_dir.join("logs")
}

/// `<app folder>\data\` — same contract as [`models_dir`] (F2's SQLite
/// database).
pub fn data_dir(app_dir: &Path) -> PathBuf {
    app_dir.join("data")
}

/// `<app folder>\pyenv\` — the whole baked tree (`python\`, `site-packages\`,
/// `service\`) T6's `bundle.resources` deposits at the install root.
pub fn pyenv_dir(app_dir: &Path) -> PathBuf {
    app_dir.join(PYENV_DIR_NAME)
}

/// `<app folder>\pyenv\python\python.exe` — the bundled interpreter
/// `sidecar::SidecarSpawnConfig::production` launches.
pub fn bundled_python_exe(app_dir: &Path) -> PathBuf {
    pyenv_dir(app_dir).join("python").join("python.exe")
}

/// Resolves the whisper model directory, always inside
/// `<app folder>\models\` (FR-8, FR-11-as-superseded) — never a path outside
/// the app folder, even when `requested` is a crafted, attacker-controlled
/// value (e.g. a tampered `config.json`'s `model.path`, the "every command
/// argument is untrusted" rule the desktop profile applies here).
///
/// `requested` is treated as a *relative* location under `models\`, not an
/// arbitrary filesystem path: it is lexically resolved (`.`/`..`/absolute
/// prefixes are stripped or collapsed, never allowed to walk above
/// `models\` itself) before being joined onto [`models_dir`]. A `None` or
/// empty/root-only `requested` yields [`DEFAULT_MODEL_NAME`].
pub fn model_dir(app_dir: &Path, requested: Option<&str>) -> PathBuf {
    let base = models_dir(app_dir);
    let components = sanitize_relative_components(requested.unwrap_or(""));
    if components.is_empty() {
        return base.join(DEFAULT_MODEL_NAME);
    }
    let mut resolved = base;
    for component in components {
        resolved.push(component);
    }
    resolved
}

/// Lexically resolves `raw` into a list of path components that are safe to
/// join onto a fixed root: drive/UNC/verbatim prefixes and root markers are
/// dropped (never treated as "start over from here"), and `..` pops the most
/// recent component instead of being carried through — so it can never
/// accumulate into a net escape above the root it is eventually joined onto,
/// no matter how many `..` segments `raw` contains. Never touches the
/// filesystem.
fn sanitize_relative_components(raw: &str) -> Vec<String> {
    let mut components: Vec<String> = Vec::new();
    for component in Path::new(raw).components() {
        match component {
            Component::Normal(part) => {
                let part = part.to_string_lossy();
                if !part.is_empty() {
                    components.push(part.into_owned());
                }
            }
            Component::ParentDir => {
                components.pop();
            }
            // Prefix (drive letter, UNC, verbatim `\\?\`) and RootDir/CurDir
            // are all dropped: `requested` is never allowed to name a
            // different root than `models_dir(app_dir)` itself.
            Component::Prefix(_) | Component::RootDir | Component::CurDir => {}
        }
    }
    components
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- resolve_app_dir ---------------------------------------------------

    #[test]
    fn resolve_app_dir_uses_the_running_executables_directory_by_default() {
        let exe = Path::new(r"C:\Users\op\AppData\Local\Programs\Transcriber\Transcriber.exe");

        let resolved = resolve_app_dir(exe, None);

        assert_eq!(
            resolved,
            PathBuf::from(r"C:\Users\op\AppData\Local\Programs\Transcriber")
        );
    }

    #[test]
    fn resolve_app_dir_honours_a_dev_override_regardless_of_the_exe_path() {
        let exe = Path::new(
            r"D:\Local\Git\transcriber\apps\desktop\src-tauri\target\debug\transcriber-desktop.exe",
        );
        let dev_override = Path::new(r"D:\Local\Git\transcriber\.dev-app-dir");

        let resolved = resolve_app_dir(exe, Some(dev_override));

        assert_eq!(resolved, dev_override);
    }

    // -- models_dir / logs_dir / data_dir / pyenv_dir ----------------------

    #[test]
    fn models_logs_data_and_pyenv_dirs_hang_off_the_app_folder() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        assert_eq!(models_dir(&app_dir), app_dir.join("models"));
        assert_eq!(logs_dir(&app_dir), app_dir.join("logs"));
        assert_eq!(data_dir(&app_dir), app_dir.join("data"));
        assert_eq!(pyenv_dir(&app_dir), app_dir.join("pyenv"));
    }

    #[test]
    fn bundled_python_exe_resolves_under_pyenv_not_resources_pyenv() {
        // See the module docs: T6's real `bundle.resources` target mapping
        // ("resources/pyenv/" -> "pyenv/") lands the baked tree at
        // `<app folder>\pyenv\...`, not `<app folder>\resources\pyenv\...`.
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = bundled_python_exe(&app_dir);

        assert_eq!(
            resolved,
            PathBuf::from(r"C:\Apps\Transcriber\pyenv\python\python.exe")
        );
    }

    // -- model_dir ----------------------------------------------------------

    #[test]
    fn model_dir_defaults_to_the_shipped_model_name() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, None);

        assert_eq!(
            resolved,
            PathBuf::from(r"C:\Apps\Transcriber\models\faster-whisper-large-v3")
        );
    }

    #[test]
    fn model_dir_accepts_a_plain_alternate_name_under_models() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some("distil-large-v3"));

        assert_eq!(
            resolved,
            PathBuf::from(r"C:\Apps\Transcriber\models\distil-large-v3")
        );
    }

    #[test]
    fn model_dir_never_escapes_the_app_folder_via_parent_traversal() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some(r"..\..\..\Windows\System32"));

        assert!(
            resolved.starts_with(models_dir(&app_dir)),
            "expected {resolved:?} to stay under {:?}",
            models_dir(&app_dir)
        );
    }

    #[test]
    fn model_dir_never_escapes_the_app_folder_via_a_different_drive() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some(r"D:\evil\payload"));

        assert!(
            resolved.starts_with(models_dir(&app_dir)),
            "expected {resolved:?} to stay under {:?}",
            models_dir(&app_dir)
        );
        assert_eq!(resolved, models_dir(&app_dir).join("evil").join("payload"));
    }

    #[test]
    fn model_dir_never_escapes_the_app_folder_via_a_unc_path() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some(r"\\attacker\share\evil"));

        assert!(resolved.starts_with(models_dir(&app_dir)));
    }

    #[test]
    fn model_dir_treats_an_all_traversal_override_as_empty() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some(r"..\.."));

        assert_eq!(resolved, models_dir(&app_dir).join(DEFAULT_MODEL_NAME));
    }

    #[test]
    fn model_dir_treats_an_empty_override_as_the_default() {
        let app_dir = PathBuf::from(r"C:\Apps\Transcriber");

        let resolved = model_dir(&app_dir, Some(""));

        assert_eq!(resolved, models_dir(&app_dir).join(DEFAULT_MODEL_NAME));
    }
}
