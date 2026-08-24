//! Engine configuration: defaults < `config.json` < `TRANSCRIBER_*` env.
//!
//! Port of `services/transcription/src/transcription/config.py`, minus
//! everything that only existed to reach a network:
//!
//! - `provider` / `cloud_model` / `provider_api_key` / `max_cloud_upload_mb`
//!   (cloud STT), and `llm_provider` / `llm_base_url` / `llm_api_key` (the
//!   OpenAI-compatible LLM engine). Inference is local, so there is nothing
//!   left to point at a server or authenticate to.
//! - `host` / `port` / `token`. They existed because the service was a
//!   separate process reached over loopback HTTP; in-process there is no
//!   socket to bind and no bearer token to check.
//! - `hf_token`. It gated the pyannote models on the Hugging Face hub; the
//!   ONNX diarization models that replace them are ungated and ship with the
//!   installer.
//!
//! What is kept is kept faithfully, including the shape of the config file,
//! because the desktop app writes it and users edit it.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

/// Why configuration could not be loaded.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("failed to read config file {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("failed to parse config file {path}: {source}")]
    Parse {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },
    #[error("config file {path} must contain a JSON object")]
    NotAnObject { path: PathBuf },
}

/// Fully resolved, immutable configuration for one run.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub app_dir: PathBuf,
    pub config_path: PathBuf,

    // --- speech to text ---
    /// Display id of the whisper model.
    pub model: String,
    /// Directory holding model files; defaults to `<app_dir>/models`.
    pub model_path: PathBuf,
    pub device: String,
    /// `None` lets the engine pick per device.
    pub compute_type: Option<String>,
    pub language: Option<String>,
    pub filter_hallucinations: bool,
    /// Word timestamps feed re-segmentation and the diarization vote, so they
    /// are on by default even though they cost a little decode time.
    pub word_timestamps: bool,
    /// How much silence ends a speech chunk. Lower breaks segments at
    /// conversational pauses instead of bridging them.
    pub vad_min_silence_ms: u32,
    /// A pause between words at least this long starts a new utterance, in
    /// addition to sentence-ending punctuation.
    pub resegment_gap_sec: f64,

    // --- diarization ---
    pub diarize: bool,
    pub diarization_model: String,
    /// Empty means the models bundled under `<app_dir>/models/diarization`.
    pub diarization_model_path: PathBuf,
    /// Upper bound on distinct speakers. The embedding matcher needs one: a
    /// voice that drifts would otherwise keep creating new speakers until the
    /// transcript is unreadable.
    pub diarization_max_speakers: u32,

    // --- local LLM ---
    pub llm_model: String,
    /// Where GGUF snapshots live; defaults to `<app_dir>/models/llm`.
    pub llm_model_path: PathBuf,
    pub llm_model_repo: String,
    pub llm_model_revision: String,
    pub llm_model_file: String,
    /// Context window the chunker budgets against and llama.cpp allocates.
    pub llm_ctx: u32,
    /// `-1` auto-fits whole layers to free VRAM (measured via NVML, layer
    /// count read from the GGUF header); `0` disables offload; a positive
    /// number pins the count.
    pub llm_gpu_layers: i32,
    /// `None` lets llama.cpp pick (physical cores).
    pub llm_threads: Option<u32>,
    pub llm_temperature: f64,
    pub llm_max_output_tokens: u32,
    /// Keep the GGUF resident between LLM jobs. Off by default so the large
    /// working set is released and never sits next to a loaded whisper model.
    pub llm_keep_loaded: bool,

    // --- jobs ---
    pub db_path: PathBuf,
    /// Directories the engine will read from or write to. Everything else is
    /// refused, whoever asked.
    pub allowed_roots: Vec<PathBuf>,
    pub job_timeout_sec: Option<u64>,
    pub log_level: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            app_dir: PathBuf::new(),
            config_path: PathBuf::new(),
            model: "large-v3".to_string(),
            model_path: PathBuf::new(),
            device: "auto".to_string(),
            compute_type: None,
            language: None,
            filter_hallucinations: true,
            word_timestamps: true,
            vad_min_silence_ms: 500,
            resegment_gap_sec: 0.6,
            diarize: false,
            diarization_model: "pyannote/segmentation-3.0".to_string(),
            diarization_model_path: PathBuf::new(),
            diarization_max_speakers: 10,
            llm_model: "qwen3.6-35b-a3b".to_string(),
            llm_model_path: PathBuf::new(),
            llm_model_repo: "ggml-org/Qwen3.6-35B-A3B-GGUF".to_string(),
            llm_model_revision: "baec3ebee244827cda0f4557eafa8b28f7545fa6".to_string(),
            llm_model_file: "Qwen3.6-35B-A3B-Q4_K_M.gguf".to_string(),
            llm_ctx: 16384,
            llm_gpu_layers: -1,
            llm_threads: None,
            llm_temperature: 0.3,
            llm_max_output_tokens: 4096,
            llm_keep_loaded: false,
            db_path: PathBuf::new(),
            allowed_roots: Vec::new(),
            job_timeout_sec: None,
            log_level: "INFO".to_string(),
        }
    }
}

/// The environment a load reads from, so tests never touch the real one.
pub type Env = HashMap<String, String>;

/// Read the process environment into a map.
pub fn process_env() -> Env {
    std::env::vars().collect()
}

impl Config {
    /// Load configuration, layering defaults < config file < `TRANSCRIBER_*`.
    ///
    /// `config_path` overrides both `TRANSCRIBER_CONFIG_PATH` and the default
    /// `<app_dir>/config.json`.
    pub fn load(config_path: Option<&Path>, env: &Env) -> Result<Self, ConfigError> {
        let app_dir = resolve_app_dir(env);
        let config_path = config_path.map(Path::to_path_buf).unwrap_or_else(|| {
            env.get("TRANSCRIBER_CONFIG_PATH")
                .map(PathBuf::from)
                .unwrap_or_else(|| app_dir.join("config.json"))
        });

        let file = read_config_file(&config_path)?;
        let mut config = Config {
            app_dir: app_dir.clone(),
            config_path,
            ..Default::default()
        };

        config.apply_file(&file);
        config.apply_env(env);
        config.fill_derived_defaults();
        Ok(config)
    }

    fn apply_file(&mut self, file: &serde_json::Map<String, Value>) {
        // The desktop app nests the model choice as `{"id": ..., "path": ...}`
        // (docs/config-contract.md). Unpacking it is not cosmetic: a generic
        // copy once assigned the whole object to the flat `model` field, and
        // the type error only surfaced much later as a failed ledger insert on
        // every job. Anything that is not an object or a non-empty string is
        // ignored rather than coerced.
        match file.get("model") {
            Some(Value::Object(model)) => {
                if let Some(id) = model
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                {
                    self.model = id.to_string();
                }
                if let Some(path) = model
                    .get("path")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                {
                    self.model_path = PathBuf::from(path);
                }
            }
            Some(Value::String(id)) if !id.is_empty() => self.model = id.clone(),
            _ => {}
        }

        if let Some(roots) = file.get("allowed_roots").and_then(Value::as_array) {
            self.allowed_roots = roots
                .iter()
                .filter_map(Value::as_str)
                .map(PathBuf::from)
                .collect();
        }
        // The vault root is always allowed; it is where every job reads and
        // writes.
        if let Some(root) = file
            .get("vault_root")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            self.allowed_roots.push(PathBuf::from(root));
        }

        set_string(&mut self.device, file.get("device"));
        set_opt_string(&mut self.compute_type, file.get("compute_type"));
        set_opt_string(&mut self.language, file.get("language"));
        set_bool(
            &mut self.filter_hallucinations,
            file.get("filter_hallucinations"),
        );
        set_bool(&mut self.word_timestamps, file.get("word_timestamps"));
        set_u32(&mut self.vad_min_silence_ms, file.get("vad_min_silence_ms"));
        set_f64(&mut self.resegment_gap_sec, file.get("resegment_gap_sec"));

        set_bool(&mut self.diarize, file.get("diarize"));
        set_string(&mut self.diarization_model, file.get("diarization_model"));
        set_path(
            &mut self.diarization_model_path,
            file.get("diarization_model_path"),
        );
        set_u32(
            &mut self.diarization_max_speakers,
            file.get("diarization_max_speakers"),
        );

        set_string(&mut self.llm_model, file.get("llm_model"));
        set_path(&mut self.llm_model_path, file.get("llm_model_path"));
        set_string(&mut self.llm_model_repo, file.get("llm_model_repo"));
        set_string(&mut self.llm_model_revision, file.get("llm_model_revision"));
        set_string(&mut self.llm_model_file, file.get("llm_model_file"));
        set_u32(&mut self.llm_ctx, file.get("llm_ctx"));
        set_i32(&mut self.llm_gpu_layers, file.get("llm_gpu_layers"));
        set_opt_u32(&mut self.llm_threads, file.get("llm_threads"));
        set_f64(&mut self.llm_temperature, file.get("llm_temperature"));
        set_u32(
            &mut self.llm_max_output_tokens,
            file.get("llm_max_output_tokens"),
        );
        set_bool(&mut self.llm_keep_loaded, file.get("llm_keep_loaded"));

        set_path(&mut self.db_path, file.get("db_path"));
        set_opt_u64(&mut self.job_timeout_sec, file.get("job_timeout_sec"));
        set_string(&mut self.log_level, file.get("log_level"));
        set_path(&mut self.model_path, file.get("model_path"));
    }

    fn apply_env(&mut self, env: &Env) {
        let get = |key: &str| env.get(&format!("TRANSCRIBER_{}", key.to_uppercase()));

        // Path-separated, matching how the desktop app passes the vault root
        // and the app folder together.
        if let Some(roots) = env.get("TRANSCRIBER_ALLOWED_ROOTS") {
            self.allowed_roots = std::env::split_paths(roots).collect();
        }

        if let Some(v) = get("model") {
            self.model = v.clone();
        }
        if let Some(v) = get("model_path") {
            self.model_path = PathBuf::from(v);
        }
        if let Some(v) = get("device") {
            self.device = v.clone();
        }
        if let Some(v) = get("compute_type") {
            self.compute_type = (!v.is_empty()).then(|| v.clone());
        }
        if let Some(v) = get("language") {
            self.language = (!v.is_empty()).then(|| v.clone());
        }
        if let Some(v) = get("filter_hallucinations") {
            self.filter_hallucinations = parse_bool(v);
        }
        if let Some(v) = get("word_timestamps") {
            self.word_timestamps = parse_bool(v);
        }
        if let Some(v) = get("vad_min_silence_ms").and_then(|v| v.parse().ok()) {
            self.vad_min_silence_ms = v;
        }
        if let Some(v) = get("resegment_gap_sec").and_then(|v| v.parse().ok()) {
            self.resegment_gap_sec = v;
        }
        if let Some(v) = get("diarize") {
            self.diarize = parse_bool(v);
        }
        if let Some(v) = get("diarization_model") {
            self.diarization_model = v.clone();
        }
        if let Some(v) = get("diarization_model_path") {
            self.diarization_model_path = PathBuf::from(v);
        }
        if let Some(v) = get("llm_model") {
            self.llm_model = v.clone();
        }
        if let Some(v) = get("llm_model_path") {
            self.llm_model_path = PathBuf::from(v);
        }
        if let Some(v) = get("llm_ctx").and_then(|v| v.parse().ok()) {
            self.llm_ctx = v;
        }
        if let Some(v) = get("llm_gpu_layers").and_then(|v| v.parse().ok()) {
            self.llm_gpu_layers = v;
        }
        if let Some(v) = get("llm_threads") {
            self.llm_threads = v.parse().ok();
        }
        if let Some(v) = get("llm_temperature").and_then(|v| v.parse().ok()) {
            self.llm_temperature = v;
        }
        if let Some(v) = get("llm_max_output_tokens").and_then(|v| v.parse().ok()) {
            self.llm_max_output_tokens = v;
        }
        if let Some(v) = get("llm_keep_loaded") {
            self.llm_keep_loaded = parse_bool(v);
        }
        if let Some(v) = get("db_path") {
            self.db_path = PathBuf::from(v);
        }
        if let Some(v) = get("job_timeout_sec") {
            self.job_timeout_sec = v.parse().ok();
        }
        if let Some(v) = get("log_level") {
            self.log_level = v.clone();
        }
    }

    /// Everything that hangs off `app_dir` once every layer has had its say.
    fn fill_derived_defaults(&mut self) {
        if self.db_path.as_os_str().is_empty() {
            self.db_path = self.app_dir.join("data").join("jobs.sqlite3");
        }
        if self.model_path.as_os_str().is_empty() {
            self.model_path = self.app_dir.join("models");
        }
        if self.llm_model_path.as_os_str().is_empty() {
            self.llm_model_path = self.app_dir.join("models").join("llm");
        }
        if self.diarization_model_path.as_os_str().is_empty() {
            self.diarization_model_path = self.app_dir.join("models").join("diarization");
        }
    }
}

/// `<app_dir>` from the environment, else the directory holding this
/// executable.
///
/// The Python service went two levels up from `sys.executable`, because its
/// interpreter lived at `<app_dir>/pyenv/python/python.exe`. The engine is the
/// application binary itself, so it is one level: `<app_dir>/Transcriber.exe`.
fn resolve_app_dir(env: &Env) -> PathBuf {
    if let Some(raw) = env.get("TRANSCRIBER_APP_DIR").filter(|v| !v.is_empty()) {
        return PathBuf::from(raw);
    }
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(Path::to_path_buf))
        .unwrap_or_default()
}

fn read_config_file(path: &Path) -> Result<serde_json::Map<String, Value>, ConfigError> {
    if !path.exists() {
        return Ok(serde_json::Map::new());
    }
    let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
        path: path.to_path_buf(),
        source,
    })?;
    let value: Value = serde_json::from_str(&text).map_err(|source| ConfigError::Parse {
        path: path.to_path_buf(),
        source,
    })?;
    match value {
        Value::Object(map) => Ok(map),
        _ => Err(ConfigError::NotAnObject {
            path: path.to_path_buf(),
        }),
    }
}

/// Accepts the JSON boolean *and* the string forms an environment variable can
/// carry, the way the Python loader did.
fn parse_bool(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn set_string(target: &mut String, value: Option<&Value>) {
    if let Some(v) = value.and_then(Value::as_str).filter(|s| !s.is_empty()) {
        *target = v.to_string();
    }
}

fn set_opt_string(target: &mut Option<String>, value: Option<&Value>) {
    match value {
        Some(Value::String(s)) => *target = (!s.is_empty()).then(|| s.clone()),
        Some(Value::Null) => *target = None,
        _ => {}
    }
}

fn set_path(target: &mut PathBuf, value: Option<&Value>) {
    if let Some(v) = value.and_then(Value::as_str).filter(|s| !s.is_empty()) {
        *target = PathBuf::from(v);
    }
}

fn set_bool(target: &mut bool, value: Option<&Value>) {
    match value {
        Some(Value::Bool(b)) => *target = *b,
        Some(Value::String(s)) => *target = parse_bool(s),
        _ => {}
    }
}

fn set_u32(target: &mut u32, value: Option<&Value>) {
    if let Some(v) = value.and_then(Value::as_u64) {
        *target = v as u32;
    }
}

fn set_i32(target: &mut i32, value: Option<&Value>) {
    if let Some(v) = value.and_then(Value::as_i64) {
        *target = v as i32;
    }
}

fn set_f64(target: &mut f64, value: Option<&Value>) {
    if let Some(v) = value.and_then(Value::as_f64) {
        *target = v;
    }
}

fn set_opt_u32(target: &mut Option<u32>, value: Option<&Value>) {
    match value {
        Some(Value::Null) => *target = None,
        Some(v) => {
            if let Some(n) = v.as_u64() {
                *target = Some(n as u32);
            }
        }
        None => {}
    }
}

fn set_opt_u64(target: &mut Option<u64>, value: Option<&Value>) {
    match value {
        Some(Value::Null) => *target = None,
        Some(v) => {
            if let Some(n) = v.as_u64() {
                *target = Some(n);
            }
        }
        None => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn env_with(app_dir: &Path) -> Env {
        let mut env = Env::new();
        env.insert(
            "TRANSCRIBER_APP_DIR".to_string(),
            app_dir.display().to_string(),
        );
        env
    }

    fn write_config(dir: &Path, value: Value) -> PathBuf {
        let path = dir.join("config.json");
        std::fs::write(&path, serde_json::to_string_pretty(&value).unwrap()).unwrap();
        path
    }

    /// A side-by-side test install shares the release install's downloaded
    /// weights by pointing these two at it -- ~23 GB that is otherwise
    /// re-fetched per install. The desktop injects `TRANSCRIBER_APP_DIR` into
    /// the same environment on every start, so what this pins is that the
    /// injected app dir does not win: `fill_derived_defaults` must leave an
    /// already-set path alone rather than re-deriving it.
    #[test]
    fn an_explicit_model_path_survives_the_injected_app_dir() {
        let dir = tempfile::tempdir().unwrap();
        let shared = tempfile::tempdir().unwrap();
        let mut env = env_with(dir.path());
        env.insert(
            "TRANSCRIBER_MODEL_PATH".to_string(),
            shared.path().join("models").display().to_string(),
        );
        env.insert(
            "TRANSCRIBER_LLM_MODEL_PATH".to_string(),
            shared.path().join("models/llm").display().to_string(),
        );

        let config = Config::load(None, &env).unwrap();

        assert_eq!(config.model_path, shared.path().join("models"));
        assert_eq!(config.llm_model_path, shared.path().join("models/llm"));
        // Everything else still belongs to this install: sharing the weights
        // must not silently share the job ledger too.
        assert_eq!(config.db_path, dir.path().join("data/jobs.sqlite3"));
        assert_eq!(
            config.diarization_model_path,
            dir.path().join("models/diarization")
        );
    }

    #[test]
    fn defaults_hang_off_the_app_dir() {
        let dir = tempfile::tempdir().unwrap();
        let config = Config::load(None, &env_with(dir.path())).unwrap();

        assert_eq!(config.db_path, dir.path().join("data/jobs.sqlite3"));
        assert_eq!(config.model_path, dir.path().join("models"));
        assert_eq!(config.llm_model_path, dir.path().join("models/llm"));
        assert_eq!(
            config.diarization_model_path,
            dir.path().join("models/diarization")
        );
        assert_eq!(config.model, "large-v3");
    }

    #[test]
    fn a_missing_config_file_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let config = Config::load(None, &env_with(dir.path())).unwrap();
        assert_eq!(config.config_path, dir.path().join("config.json"));
    }

    #[test]
    fn the_nested_model_object_unpacks_onto_flat_fields() {
        // The shape the desktop app actually writes. Copying the object
        // verbatim once produced a job-submission failure far from here.
        let dir = tempfile::tempdir().unwrap();
        write_config(
            dir.path(),
            json!({"model": {"id": "large-v3", "path": "D:\\models"}}),
        );
        let config = Config::load(None, &env_with(dir.path())).unwrap();
        assert_eq!(config.model, "large-v3");
        assert_eq!(config.model_path, PathBuf::from("D:\\models"));
    }

    #[test]
    fn a_model_value_of_the_wrong_shape_is_ignored_not_coerced() {
        let dir = tempfile::tempdir().unwrap();
        write_config(dir.path(), json!({"model": ["large-v3"]}));
        let config = Config::load(None, &env_with(dir.path())).unwrap();
        assert_eq!(config.model, "large-v3", "falls back to the default");
        assert_eq!(config.model_path, dir.path().join("models"));
    }

    #[test]
    fn the_vault_root_joins_the_allowed_roots() {
        let dir = tempfile::tempdir().unwrap();
        write_config(
            dir.path(),
            json!({"vault_root": "D:\\Meetings", "allowed_roots": ["D:\\Other"]}),
        );
        let config = Config::load(None, &env_with(dir.path())).unwrap();
        assert_eq!(
            config.allowed_roots,
            vec![PathBuf::from("D:\\Other"), PathBuf::from("D:\\Meetings")]
        );
    }

    #[test]
    fn env_overrides_the_config_file() {
        let dir = tempfile::tempdir().unwrap();
        write_config(dir.path(), json!({"device": "cpu", "llm_ctx": 8192}));

        let mut env = env_with(dir.path());
        env.insert("TRANSCRIBER_DEVICE".to_string(), "cuda".to_string());
        env.insert("TRANSCRIBER_LLM_CTX".to_string(), "32768".to_string());

        let config = Config::load(None, &env).unwrap();
        assert_eq!(config.device, "cuda");
        assert_eq!(config.llm_ctx, 32768);
    }

    #[test]
    fn env_booleans_accept_the_string_forms_a_shell_can_carry() {
        let dir = tempfile::tempdir().unwrap();
        let mut env = env_with(dir.path());
        env.insert("TRANSCRIBER_DIARIZE".to_string(), "true".to_string());
        env.insert("TRANSCRIBER_WORD_TIMESTAMPS".to_string(), "0".to_string());
        let config = Config::load(None, &env).unwrap();
        assert!(config.diarize);
        assert!(!config.word_timestamps);
    }

    #[test]
    fn allowed_roots_from_env_are_path_separated() {
        let dir = tempfile::tempdir().unwrap();
        let mut env = env_with(dir.path());
        let joined = std::env::join_paths(["D:\\A", "D:\\B"])
            .unwrap()
            .into_string()
            .unwrap();
        env.insert("TRANSCRIBER_ALLOWED_ROOTS".to_string(), joined);

        let config = Config::load(None, &env).unwrap();
        assert_eq!(
            config.allowed_roots,
            vec![PathBuf::from("D:\\A"), PathBuf::from("D:\\B")]
        );
    }

    #[test]
    fn unknown_keys_are_ignored() {
        // config.json carries desktop-only keys the engine never reads, and
        // hand-edited files carry typos. Neither may fail a load.
        let dir = tempfile::tempdir().unwrap();
        write_config(
            dir.path(),
            json!({"theme": "dark", "llm_base_url": "http://localhost:1234"}),
        );
        assert!(Config::load(None, &env_with(dir.path())).is_ok());
    }

    #[test]
    fn a_malformed_config_file_fails_loudly() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("config.json"), "{not json").unwrap();
        assert!(matches!(
            Config::load(None, &env_with(dir.path())),
            Err(ConfigError::Parse { .. })
        ));
    }

    #[test]
    fn a_config_file_that_is_not_an_object_fails_loudly() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("config.json"), "[1, 2]").unwrap();
        assert!(matches!(
            Config::load(None, &env_with(dir.path())),
            Err(ConfigError::NotAnObject { .. })
        ));
    }

    #[test]
    fn an_explicit_path_beats_the_env_and_the_default() {
        let dir = tempfile::tempdir().unwrap();
        let elsewhere = dir.path().join("elsewhere.json");
        std::fs::write(&elsewhere, r#"{"device": "cpu"}"#).unwrap();
        write_config(dir.path(), json!({"device": "cuda"}));

        let mut env = env_with(dir.path());
        env.insert(
            "TRANSCRIBER_CONFIG_PATH".to_string(),
            dir.path().join("config.json").display().to_string(),
        );

        let config = Config::load(Some(&elsewhere), &env).unwrap();
        assert_eq!(config.device, "cpu");
        assert_eq!(config.config_path, elsewhere);
    }
}
