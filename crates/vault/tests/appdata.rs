//! FR-16: the application-data directory is a concept distinct from the
//! vault. These tests mutate the process-wide `LOCALAPPDATA` environment
//! variable, so they serialize on `ENV_LOCK` to stay safe under the
//! default parallel test runner.

use std::path::PathBuf;
use std::sync::Mutex;

use vault::appdata::{app_data_dir, DEFAULT_APP_NAME};
use vault::error::VaultError;

static ENV_LOCK: Mutex<()> = Mutex::new(());

struct EnvGuard {
    original: Option<std::ffi::OsString>,
}

impl EnvGuard {
    fn set(value: &std::path::Path) -> Self {
        let original = std::env::var_os("LOCALAPPDATA");
        // SAFETY: serialized by ENV_LOCK, held for the guard's whole lifetime.
        unsafe { std::env::set_var("LOCALAPPDATA", value) };
        EnvGuard { original }
    }

    fn unset() -> Self {
        let original = std::env::var_os("LOCALAPPDATA");
        // SAFETY: serialized by ENV_LOCK, held for the guard's whole lifetime.
        unsafe { std::env::remove_var("LOCALAPPDATA") };
        EnvGuard { original }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        // SAFETY: serialized by ENV_LOCK, held for the guard's whole lifetime.
        match &self.original {
            Some(v) => unsafe { std::env::set_var("LOCALAPPDATA", v) },
            None => unsafe { std::env::remove_var("LOCALAPPDATA") },
        }
    }
}

#[test]
fn returns_localappdata_joined_with_app_name() {
    let _lock = ENV_LOCK.lock().unwrap();
    let _env = EnvGuard::set(std::path::Path::new(r"C:\Users\example\AppData\Local"));

    let dir = app_data_dir("Transcriber").expect("should resolve");

    assert_eq!(
        dir,
        PathBuf::from(r"C:\Users\example\AppData\Local\Transcriber")
    );
    assert!(dir.is_absolute());
}

#[test]
fn creates_nothing_on_disk() {
    let _lock = ENV_LOCK.lock().unwrap();
    let temp = tempfile::tempdir().expect("tempdir");
    let _env = EnvGuard::set(temp.path());

    let dir = app_data_dir("SomeApp").expect("should resolve");

    assert!(
        !dir.exists(),
        "app_data_dir must not create the directory: {}",
        dir.display()
    );
}

#[test]
fn errors_when_localappdata_unset() {
    let _lock = ENV_LOCK.lock().unwrap();
    let _env = EnvGuard::unset();

    let result = app_data_dir("Transcriber");

    assert_eq!(result, Err(VaultError::AppDataUnavailable));
}

#[test]
fn rejects_unsafe_app_names() {
    let _lock = ENV_LOCK.lock().unwrap();
    let _env = EnvGuard::set(std::path::Path::new(r"C:\Users\example\AppData\Local"));

    for bad in ["..", r"..\evil", "a/b", r"a\b", "a:b", r"..\.."] {
        let result = app_data_dir(bad);
        assert!(result.is_err(), "expected {bad:?} to be rejected");
    }
}

#[test]
fn default_app_name_is_valid() {
    let _lock = ENV_LOCK.lock().unwrap();
    let _env = EnvGuard::set(std::path::Path::new(r"C:\Users\example\AppData\Local"));

    let result = app_data_dir(DEFAULT_APP_NAME);

    assert!(
        result.is_ok(),
        "DEFAULT_APP_NAME should be a valid app name: {result:?}"
    );
}
