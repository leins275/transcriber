//! Loading the ggml compute backends, once per process.
//!
//! ggml is built with `GGML_BACKEND_DL`, so the code that actually does the
//! arithmetic lives in `ggml-cpu-*.dll` (and, when installed, `ggml-cuda.dll`)
//! rather than inside the library. Nothing loads them implicitly: until
//! someone asks, ggml has zero devices registered, and the first model load
//! dies on `GGML_ASSERT(device)` -- a hard abort, not a Rust error. So this
//! runs before any engine touches a model.
//!
//! The indirection is what buys the two properties the whole design rests on:
//! one binary serves CPU-only and NVIDIA machines because the GPU backend is a
//! file that may or may not be there, and whisper.cpp and llama.cpp share one
//! ggml instead of embedding a copy each.

use std::path::{Path, PathBuf};
use std::sync::Once;

static LOADED: Once = Once::new();

/// Where the installer puts the CPU backends, relative to the app folder.
pub const ENGINE_BACKENDS_DIR: &str = "runtime/engine/backends";

/// Where the optional GPU payload lands, relative to the app folder. It is a
/// separate directory so an app update can replace the engine's own backends
/// without touching a 600 MB download the user already paid for.
pub const CUDA_RUNTIME_DIR: &str = "runtime/cuda";

/// Load every backend available to this installation, at most once.
///
/// Directories that do not exist are skipped rather than reported: a machine
/// with no GPU payload is the normal case, not a broken one. If the
/// application folder holds nothing, this falls back to the directory the
/// build wrote its backends into, which is what makes `cargo test` and
/// `cargo run` work without an install.
pub fn ensure_loaded(app_dir: &Path) {
    LOADED.call_once(|| {
        let mut loaded_any = false;
        for dir in candidate_dirs(app_dir) {
            if dir.is_dir() {
                llama_cpp_2::llama_backend::load_backends_from_path(&dir);
                loaded_any = true;
            }
        }
        if !loaded_any {
            // The compile-time directory llama-cpp-sys-2 built into; present
            // in a development tree, absent in an installed app.
            llama_cpp_2::llama_backend::load_backends();
        }
    });
}

fn candidate_dirs(app_dir: &Path) -> Vec<PathBuf> {
    vec![
        app_dir.join(ENGINE_BACKENDS_DIR),
        // Loaded after the CPU backends so that, on a machine with both, the
        // GPU registers as an additional device rather than the only one --
        // ggml still falls back to the CPU for anything CUDA cannot take.
        app_dir.join(CUDA_RUNTIME_DIR),
    ]
}

/// Whether the optional GPU payload is installed.
pub fn cuda_runtime_present(app_dir: &Path) -> bool {
    let dir = app_dir.join(CUDA_RUNTIME_DIR);
    dir.join("ggml-cuda.dll").is_file() && dir.join(".ready").is_file()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_gpu_payload_is_only_present_once_it_is_marked_ready() {
        let dir = tempfile::tempdir().unwrap();
        let cuda = dir.path().join(CUDA_RUNTIME_DIR);
        std::fs::create_dir_all(&cuda).unwrap();
        assert!(!cuda_runtime_present(dir.path()));

        std::fs::write(cuda.join("ggml-cuda.dll"), b"stub").unwrap();
        assert!(
            !cuda_runtime_present(dir.path()),
            "a half-finished download must not count"
        );

        std::fs::write(cuda.join(".ready"), b"").unwrap();
        assert!(cuda_runtime_present(dir.path()));
    }

    #[test]
    fn loading_is_safe_to_call_more_than_once() {
        // Every job asks; only the first does anything. Registering a backend
        // twice would be a mistake ggml has no reason to tolerate.
        let dir = tempfile::tempdir().unwrap();
        ensure_loaded(dir.path());
        ensure_loaded(dir.path());
    }

    #[test]
    fn both_backend_directories_are_considered() {
        let dirs = candidate_dirs(Path::new("C:\\app"));
        assert!(dirs[0].ends_with("backends"));
        assert!(dirs[1].ends_with("cuda"));
    }
}
