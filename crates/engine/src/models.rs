//! Where model files live, and how "installed" is decided.
//!
//! One module owns the layout because three things depend on agreeing about
//! it: the downloader that writes the files, the engines that load them, and
//! the settings UI that offers a download when they are missing.
//!
//! **The whisper model changes format in this rewrite.** faster-whisper loaded
//! a CTranslate2 *snapshot directory*; whisper.cpp loads a single GGML file.
//! So a machine upgrading from the Python build will find its whisper model
//! "missing" and be offered a download -- that is correct, not a bug, and the
//! stale CTranslate2 snapshot is dead weight to be cleaned up separately.
//!
//! A payload counts as installed only when a `.ready` marker sits beside it.
//! The file appearing is not enough: a half-finished download is a file too,
//! and the marker is written last, after the digest is verified.

use std::path::{Path, PathBuf};

use crate::config::Config;

/// Suffix of the marker written after a payload is fully downloaded and
/// verified.
pub const READY_SUFFIX: &str = ".ready";

/// `<model_path>/whisper/ggml-<model>.bin` -- the GGML weights whisper.cpp
/// loads.
pub fn whisper_model_file(config: &Config) -> PathBuf {
    config
        .model_path
        .join("whisper")
        .join(format!("ggml-{}.bin", config.model))
}

/// `<model_path>/whisper/ggml-silero-v5.1.2.bin` -- the VAD model that decides
/// where speech chunks end.
pub fn whisper_vad_model_file(config: &Config) -> PathBuf {
    config
        .model_path
        .join("whisper")
        .join("ggml-silero-v5.1.2.bin")
}

/// The pyannote segmentation model: when someone is speaking.
///
/// Bundled by the installer rather than downloaded. Together the two
/// diarization models are small enough that a download flow -- and the gated
/// Hugging Face token the torch models needed -- would cost more than they
/// save.
pub fn diarization_segmentation_model(config: &Config) -> PathBuf {
    config.diarization_model_path.join("segmentation-3.0.onnx")
}

/// The speaker embedding model: who is speaking.
pub fn diarization_embedding_model(config: &Config) -> PathBuf {
    config
        .diarization_model_path
        .join("wespeaker_en_voxceleb_CAM++.onnx")
}

/// The ONNX Runtime shared library, addressed by absolute path so another
/// application's copy on `PATH` cannot be loaded instead.
pub fn onnx_runtime_library(config: &Config) -> PathBuf {
    config
        .app_dir
        .join("runtime")
        .join("onnx")
        .join(if cfg!(windows) {
            "onnxruntime.dll"
        } else if cfg!(target_os = "macos") {
            "libonnxruntime.dylib"
        } else {
            "libonnxruntime.so"
        })
}

/// `<llm_model_path>/<llm_model_file>` -- the GGUF the local LLM loads.
pub fn llm_model_file(config: &Config) -> PathBuf {
    config.llm_model_path.join(&config.llm_model_file)
}

/// The marker that says `payload` finished downloading and verified.
pub fn ready_marker(payload: &Path) -> PathBuf {
    let mut marker = payload.as_os_str().to_os_string();
    marker.push(READY_SUFFIX);
    PathBuf::from(marker)
}

/// Whether a payload is present *and* was verified.
pub fn is_installed(payload: &Path) -> bool {
    payload.is_file() && ready_marker(payload).exists()
}

/// Mark a payload as complete. Called only after its digest checks out.
pub fn mark_installed(payload: &Path) -> std::io::Result<()> {
    std::fs::write(ready_marker(payload), b"")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_in(dir: &Path) -> Config {
        let mut env = crate::config::Env::new();
        env.insert("TRANSCRIBER_APP_DIR".to_string(), dir.display().to_string());
        Config::load(None, &env).expect("config")
    }

    #[test]
    fn model_paths_hang_off_the_configured_directories() {
        let dir = tempfile::tempdir().unwrap();
        let config = config_in(dir.path());

        assert_eq!(
            whisper_model_file(&config),
            dir.path().join("models/whisper/ggml-large-v3.bin")
        );
        assert_eq!(
            llm_model_file(&config),
            dir.path().join("models/llm/Qwen3.6-35B-A3B-Q4_K_M.gguf")
        );
    }

    #[test]
    fn a_file_without_its_marker_is_not_installed() {
        // The half-download case: bytes on disk prove nothing until the digest
        // has been checked and the marker written.
        let dir = tempfile::tempdir().unwrap();
        let payload = dir.path().join("ggml-large-v3.bin");
        std::fs::write(&payload, b"partial").unwrap();
        assert!(!is_installed(&payload));

        mark_installed(&payload).unwrap();
        assert!(is_installed(&payload));
    }

    #[test]
    fn a_marker_without_its_file_is_not_installed() {
        let dir = tempfile::tempdir().unwrap();
        let payload = dir.path().join("ggml-large-v3.bin");
        mark_installed(&payload).unwrap();
        assert!(!is_installed(&payload));
    }
}
