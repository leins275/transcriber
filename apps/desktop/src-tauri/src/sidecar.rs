//! Spawns F2, parses its ready line, and terminates it on exit (FR-12,
//! FR-13; plan.md "Sidecar lifecycle (Q1 -> A)").
//!
//! **Real F2 contract, read from `services/transcription/src/transcription/`
//! at implementation time (plan.md's F1/F2-drift risk):**
//! - Ready line (`logging_setup.emit_ready_line`, `server.py`): exactly one
//!   stdout line, `{"event":"listening","port":N,"token":"...","pid":N}`,
//!   emitted once the loopback socket is already bound.
//! - Dev command (`cli.py`): `uv run --directory services/transcription
//!   transcription-service serve --port 0`.
//! - Environment (`config.py::load_config`), layered `defaults < config file
//!   < TRANSCRIBER_* env < CLI overrides`: `TRANSCRIBER_APP_DIR` (resolves
//!   `app_dir`), `TRANSCRIBER_ALLOWED_ROOTS` (`os.pathsep`-joined list),
//!   `TRANSCRIBER_MODEL_PATH`, and — because the dataclass field is named
//!   `model`, not `model_id` — `TRANSCRIBER_MODEL` for the model id. The
//!   config-file path is read from `TRANSCRIBER_CONFIG_PATH`, **not**
//!   `TRANSCRIBER_CONFIG` as plan.md's prose states; `config.py` only reads
//!   `env.get("TRANSCRIBER_CONFIG_PATH")` when `config_path` is not passed
//!   explicitly. This module uses the real names, not the plan's drifted
//!   ones.
//!
//! `SidecarSpawnConfig` is the one place the program, argument vector and
//! environment are decided, so F4 can repoint it at the baked environment
//! (only this struct's construction needs to change). No `shell = true`
//! equivalent exists anywhere here: the program and each argument are
//! separate `String`s in a `Vec`, passed straight to
//! `tokio::process::Command::args`, never joined into one string that a
//! shell would re-parse.

use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, ChildStdout, Command};

use crate::app_paths;
use crate::config::Settings;

/// `CREATE_NO_WINDOW` (winbase.h) — every console-subsystem child this
/// module spawns (the `uv`/bundled-Python sidecar, `taskkill`) must pass
/// this to `creation_flags` so Windows never allocates a new console
/// window for it. Without it, a console-subsystem child spawned from this
/// GUI-subsystem app (see `main.rs`'s `windows_subsystem = "windows"`)
/// still gets its own fresh console, which is exactly the visible flash a
/// field report caught. GUI-subsystem children (none spawned by this
/// module) never need this flag — it only suppresses console
/// *allocation*, which GUI apps never trigger in the first place.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// How long the supervisor waits for F2's ready line before giving up
/// (FR-13). Spawning itself never blocks window creation (NFR-3); this
/// timeout only governs when the "service starting" state degrades into
/// "service unavailable".
pub const READY_TIMEOUT: Duration = Duration::from_secs(30);

/// Best-effort "is there an NVIDIA GPU here" probe: `nvidia-smi` on PATH,
/// the same signal F2's `model_routes._nvidia_gpu_present` reads. A PATH
/// scan only — nothing is spawned.
fn nvidia_gpu_present() -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| dir.join("nvidia-smi.exe").is_file())
}

/// One spawn decision: the program, its argument vector, and the
/// environment variables to set — nothing else decides how F2 is launched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SidecarSpawnConfig {
    pub program: String,
    pub args: Vec<String>,
    pub envs: Vec<(String, String)>,
}

impl SidecarSpawnConfig {
    /// The dev-mode command from plan.md's "Sidecar lifecycle": `uv run
    /// --directory services/transcription transcription-service serve
    /// --port 0`, with F2's real `TRANSCRIBER_*` environment variables
    /// derived from `settings`, `config_path` and `app_dir`.
    ///
    /// Probes for an NVIDIA GPU (the same `nvidia-smi`-on-PATH signal F2's
    /// own `_nvidia_gpu_present` uses) to pick which llama.cpp variant `uv
    /// run` syncs -- see [`SidecarSpawnConfig::dev_with_gpu`].
    pub fn dev(settings: &Settings, config_path: &Path, app_dir: &Path) -> Self {
        Self::dev_with_gpu(settings, config_path, app_dir, nvidia_gpu_present())
    }

    /// The deterministic half of [`SidecarSpawnConfig::dev`], with the GPU
    /// probe injected: `--extra llm-cuda` (the CUDA llama.cpp wheel) on a
    /// machine with an NVIDIA GPU, `--extra llm-cpu` otherwise. One of the
    /// two must always be passed -- they are uv "conflicting extras", and a
    /// bare `uv run` would sync a venv with no llama.cpp at all, making
    /// every LLM job fail with `model_load`.
    pub fn dev_with_gpu(
        settings: &Settings,
        config_path: &Path,
        app_dir: &Path,
        gpu_present: bool,
    ) -> Self {
        let mut envs = vec![
            (
                "TRANSCRIBER_CONFIG_PATH".to_string(),
                config_path.display().to_string(),
            ),
            (
                "TRANSCRIBER_APP_DIR".to_string(),
                app_dir.display().to_string(),
            ),
        ];
        if let Some(root) = settings.meetings_root.as_ref() {
            envs.push(("TRANSCRIBER_ALLOWED_ROOTS".to_string(), root.clone()));
            // The search indexer walks the vault; same source of truth as
            // the allowlist, kept as its own variable because the service
            // treats "where may I read" and "what do I index" as different
            // questions.
            envs.push(("TRANSCRIBER_VAULT_ROOT".to_string(), root.clone()));
        }
        if let Some(path) = settings.model.path.as_ref() {
            envs.push(("TRANSCRIBER_MODEL_PATH".to_string(), path.clone()));
        }
        if let Some(id) = settings.model.id.as_ref() {
            envs.push(("TRANSCRIBER_MODEL".to_string(), id.clone()));
        }

        let llm_extra = if gpu_present { "llm-cuda" } else { "llm-cpu" };
        SidecarSpawnConfig {
            program: "uv".to_string(),
            args: vec![
                "run".to_string(),
                "--directory".to_string(),
                "services/transcription".to_string(),
                "--extra".to_string(),
                llm_extra.to_string(),
                "transcription-service".to_string(),
                "serve".to_string(),
                "--port".to_string(),
                "0".to_string(),
            ],
            envs,
        }
    }

    /// The production command: `<app folder>\pyenv\python\python.exe -m
    /// transcription serve --port 0 --config <config_path>`, launched
    /// through the bundled, relocatable interpreter `app_paths` resolves
    /// (T4's baked runtime -- R2), per the Architecture overview's
    /// Configuration contract. `TRANSCRIBER_APP_DIR` and
    /// `TRANSCRIBER_MODEL_PATH` are derived from `app_dir` via `app_paths`
    /// rather than trusted verbatim from `settings.model.path` -- every
    /// value read back from `config.json` is untrusted input the same way a
    /// caller-supplied IPC argument is (desktop profile).
    ///
    /// Fails with [`SidecarError::MissingRuntime`], naming the exact path
    /// that does not exist, when the baked tree is absent under `app_dir` --
    /// this is what lets [`plan_sidecar`] detect "no bundled runtime here"
    /// and fall back to [`SidecarSpawnConfig::dev`] instead of building a
    /// command that could only ever fail once actually spawned.
    pub fn production(
        settings: &Settings,
        config_path: &Path,
        app_dir: &Path,
    ) -> Result<Self, SidecarError> {
        let python_exe = app_paths::bundled_python_exe(app_dir);
        if !python_exe.is_file() {
            return Err(SidecarError::MissingRuntime {
                path: python_exe.display().to_string(),
            });
        }

        let model_dir = app_paths::model_dir(app_dir, settings.model.path.as_deref());

        let mut envs = vec![
            (
                "TRANSCRIBER_APP_DIR".to_string(),
                app_dir.display().to_string(),
            ),
            (
                "TRANSCRIBER_MODEL_PATH".to_string(),
                model_dir.display().to_string(),
            ),
        ];
        if let Some(root) = settings.meetings_root.as_ref() {
            envs.push(("TRANSCRIBER_ALLOWED_ROOTS".to_string(), root.clone()));
            // Same rationale as the dev spawn: the vault root feeds the
            // service's search indexer.
            envs.push(("TRANSCRIBER_VAULT_ROOT".to_string(), root.clone()));
        }
        if let Some(id) = settings.model.id.as_ref() {
            envs.push(("TRANSCRIBER_MODEL".to_string(), id.clone()));
        }

        Ok(SidecarSpawnConfig {
            program: python_exe.display().to_string(),
            args: vec![
                "-m".to_string(),
                "transcription".to_string(),
                "serve".to_string(),
                "--port".to_string(),
                "0".to_string(),
                "--config".to_string(),
                config_path.display().to_string(),
            ],
            envs,
        })
    }

    /// Builds the real `tokio::process::Command` this config describes.
    /// stdin is closed, stdout/stderr are piped so the supervisor can read
    /// the ready line and drain diagnostics without a shell in between.
    fn to_command(&self) -> Command {
        let mut command = Command::new(&self.program);
        command.args(&self.args);
        command.envs(self.envs.iter().map(|(k, v)| (k.as_str(), v.as_str())));
        command.stdin(Stdio::null());
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());
        // Bug fix (field report): `uv`/the bundled `python.exe` are
        // console-subsystem programs; spawned without this flag from this
        // windows-subsystem app, Windows allocates them a brand-new,
        // visible console window (see the CREATE_NO_WINDOW doc comment
        // above).
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        command
    }
}

/// What `plan_sidecar` decided: spawn our own F2, or connect to an
/// operator-provided URL and never spawn one (FR-12's "expects it running"
/// mode, still required per plan.md's Sidecar lifecycle section).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SidecarPlan {
    Spawn(SidecarSpawnConfig),
    UseExisting { base_url: String },
}

/// Decides whether to spawn F2 or connect to a configured URL. A non-empty
/// `settings.service.base_url` means "do not spawn, use this URL" (FR-12);
/// otherwise picks between the production and dev spawn commands by
/// checking whether T4's baked runtime is actually present under `app_dir`
/// (T9): a real install always has `<app_dir>\pyenv\python\python.exe`, and
/// a `tauri dev` checkout never does, so this detection is robust across
/// both without depending on `cfg!(debug_assertions)` (which only reflects
/// how *this* Rust binary was built, not whether the Python payload was ever
/// baked next to it). `TRANSCRIBER_FAKE_SERVICE`/`--fake-service` short-
/// circuits before this function is ever called (`lib.rs`), so dev mode
/// with the fake service keeps working unchanged.
pub fn plan_sidecar(settings: &Settings, config_path: &Path, app_dir: &Path) -> SidecarPlan {
    match settings.service.base_url.as_deref() {
        Some(base_url) if !base_url.trim().is_empty() => SidecarPlan::UseExisting {
            base_url: base_url.to_string(),
        },
        _ => {
            let config = SidecarSpawnConfig::production(settings, config_path, app_dir)
                .unwrap_or_else(|_| SidecarSpawnConfig::dev(settings, config_path, app_dir));
            SidecarPlan::Spawn(config)
        }
    }
}

/// F2's parsed ready line (F2 FR-14): the port it bound, the bearer token
/// it issued, and its pid.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadyLine {
    pub port: u16,
    pub token: String,
    pub pid: u32,
}

impl ReadyLine {
    /// The loopback base URL derived from the reported port (NFR-5: this
    /// app only ever talks to `127.0.0.1`).
    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

/// The wire shape of a candidate ready line, before it is validated into a
/// [`ReadyLine`]. Every field is optional so a malformed or unrelated JSON
/// line is rejected, never panicked on (NFR-6).
#[derive(Debug, Deserialize)]
struct RawReadyLine {
    event: String,
    port: Option<u16>,
    token: Option<String>,
    pid: Option<u32>,
}

/// Why a single candidate line was not a valid ready line (not a fatal
/// error by itself — [`read_ready_line`] keeps reading past these).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseReadyLineError {
    pub message: String,
}

/// Parses one line of the child's stdout as F2's ready line. Non-JSON, a
/// JSON line with a different `event`, or a `listening` line missing
/// `port`/`token`/`pid` are all rejected here rather than panicking.
pub fn parse_ready_line(line: &str) -> Result<ReadyLine, ParseReadyLineError> {
    let raw: RawReadyLine = serde_json::from_str(line).map_err(|err| ParseReadyLineError {
        message: format!("not a ready line: {err}"),
    })?;

    if raw.event != "listening" {
        return Err(ParseReadyLineError {
            message: format!("expected event \"listening\", got {:?}", raw.event),
        });
    }
    let port = raw.port.ok_or_else(|| ParseReadyLineError {
        message: "ready line is missing \"port\"".to_string(),
    })?;
    let token = raw.token.ok_or_else(|| ParseReadyLineError {
        message: "ready line is missing \"token\"".to_string(),
    })?;
    let pid = raw.pid.ok_or_else(|| ParseReadyLineError {
        message: "ready line is missing \"pid\"".to_string(),
    })?;

    Ok(ReadyLine { port, token, pid })
}

/// Every way waiting for the sidecar to report ready can fail.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SidecarError {
    /// No valid ready line arrived before the deadline, naming the command
    /// that was being waited on (FR-13's "say so explicitly").
    Timeout { command: String },
    /// The stream ended (or a real I/O error occurred) before a ready line
    /// was seen.
    Io { message: String },
    /// [`SidecarSpawnConfig::production`] could not find the bundled
    /// interpreter -- names the exact missing path rather than panicking or
    /// silently falling through to a partially-built command (F3 NFR-6).
    MissingRuntime { path: String },
}

impl std::fmt::Display for SidecarError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SidecarError::Timeout { command } => {
                write!(f, "timed out waiting for \"{command}\" to report ready")
            }
            SidecarError::Io { message } => write!(f, "{message}"),
            SidecarError::MissingRuntime { path } => {
                write!(f, "bundled Python runtime not found at \"{path}\"")
            }
        }
    }
}

impl std::error::Error for SidecarError {}

/// Reads lines from `reader` until one parses as a valid ready line,
/// ignoring malformed/unrelated lines along the way, or until `timeout`
/// elapses — in which case the error names `command` (FR-13). This is a
/// pure function over an injected reader so tests need no real F2 process.
pub async fn read_ready_line<R>(
    reader: R,
    timeout: Duration,
    command: &str,
) -> Result<ReadyLine, SidecarError>
where
    R: AsyncRead + Unpin,
{
    let mut lines = BufReader::new(reader).lines();

    let read_loop = async {
        loop {
            match lines.next_line().await {
                Ok(Some(line)) => {
                    if let Ok(ready) = parse_ready_line(&line) {
                        return Ok(ready);
                    }
                    // Malformed line or a different event: ignored, keep
                    // reading (NFR-6 — never panics on unexpected input).
                }
                Ok(None) => {
                    return Err(SidecarError::Io {
                        message: format!(
                            "\"{command}\" closed stdout before reporting a ready line"
                        ),
                    })
                }
                Err(err) => {
                    return Err(SidecarError::Io {
                        message: format!("error reading \"{command}\"'s stdout: {err}"),
                    })
                }
            }
        }
    };

    match tokio::time::timeout(timeout, read_loop).await {
        Ok(result) => result,
        Err(_) => Err(SidecarError::Timeout {
            command: command.to_string(),
        }),
    }
}

/// Drains a child's stderr to the app log, line by line, until the pipe
/// closes. F2's own diagnostics are structured JSON-lines on stderr
/// (`logging_setup.py`); this module treats them as opaque text — decoding
/// them is not this task's job.
async fn drain_stderr(stderr: tokio::process::ChildStderr) {
    let mut lines = BufReader::new(stderr).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        eprintln!("[sidecar] {line}");
    }
}

/// Owns (at most) one running sidecar child. Killing the previous child
/// before spawning a new one is `restart`'s whole job (used when
/// `set_meetings_root` changes F2's `--allow-root`); the child is also
/// killed on `Drop` and is meant to be killed again explicitly from the
/// app's exit hook (T11).
///
/// ## Why `child.start_kill()` alone is not enough (E6)
///
/// The spawned child is `uv`, which on Windows launches the real F2 Python
/// process as *its own* child -- Windows does not kill process trees by
/// default, so killing only the `uv` process leaves the Python service
/// (with a whisper model resident) running as an orphan. F2's ready line
/// (`ReadyLine.pid`) carries its own real pid precisely so this module can
/// terminate it directly instead: [`Sidecar::record_service_pid`] records it
/// once the ready line is parsed, and [`Sidecar::terminate`] kills that pid's
/// whole process tree (`taskkill /PID <pid> /T /F`) in addition to the `uv`
/// process itself.
pub struct Sidecar {
    child: Option<Child>,
    /// F2's own pid (from its ready line), once known — see the module
    /// docs above for why this, not just `child`, must be killed.
    service_pid: Option<u32>,
}

impl Sidecar {
    pub fn new() -> Self {
        Sidecar {
            child: None,
            service_pid: None,
        }
    }

    /// Spawns `config`, replacing any child already owned by this
    /// `Sidecar` (without killing it — callers that need the "kill before
    /// spawn" behaviour should call [`Sidecar::restart`] instead). Returns
    /// the new child's stdout so the caller can await its ready line (via
    /// [`read_ready_line`]) independently of the spawn call itself.
    pub fn spawn(&mut self, config: &SidecarSpawnConfig) -> Result<ChildStdout, SidecarError> {
        let mut command = config.to_command();
        let mut child = command.spawn().map_err(|err| SidecarError::Io {
            message: format!("failed to spawn \"{}\": {err}", config.program),
        })?;

        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(drain_stderr(stderr));
        }
        let stdout = child.stdout.take().ok_or_else(|| SidecarError::Io {
            message: format!("\"{}\"'s stdout was not piped", config.program),
        })?;

        self.child = Some(child);
        self.service_pid = None;
        Ok(stdout)
    }

    /// Records F2's own pid (from its parsed ready line) against the
    /// currently-owned child, so a later [`Sidecar::terminate`] kills the
    /// real service process tree, not just the `uv` wrapper (E6).
    pub fn record_service_pid(&mut self, pid: u32) {
        self.service_pid = Some(pid);
    }

    /// Kills and waits for any previously spawned child, then spawns
    /// `config`. The kill-then-spawn ordering is what makes this safe to
    /// call from `set_meetings_root`: F2 must never have two instances
    /// racing over the same `--allow-root`.
    pub async fn restart(
        &mut self,
        config: &SidecarSpawnConfig,
    ) -> Result<ChildStdout, SidecarError> {
        self.terminate().await;
        self.spawn(config)
    }

    /// Kills the owned child (if any) and waits for it to exit, and (E6)
    /// kills F2's own process tree by pid if it was ever recorded. A no-op
    /// if nothing is running. Used by `restart` and by the app's exit hook.
    pub async fn terminate(&mut self) {
        if let Some(pid) = self.service_pid.take() {
            kill_process_tree_by_pid(pid).await;
        }
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
    }
}

impl Default for Sidecar {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let _ = child.start_kill();
        }
        // `Drop` cannot `.await`, so the `service_pid` process tree cannot
        // be reaped here the way `terminate()` does -- `lib.rs`'s exit hook
        // always calls `terminate()` explicitly before the process exits,
        // which is the real safety net; this is a best-effort fallback for
        // the `uv` wrapper only.
    }
}

/// Kills `pid` and its whole descendant process tree via the Windows
/// `taskkill` utility (E6) -- the only reliable way to reach a grandchild
/// process (F2's real Python process, spawned by `uv`) without adding a
/// Job Object dependency. Errors (the pid already gone, `taskkill` itself
/// missing, ...) are swallowed: this is exit-time best-effort cleanup, never
/// something a caller should have to handle.
#[cfg(windows)]
async fn kill_process_tree_by_pid(pid: u32) {
    let result = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        // Bug fix (field report): taskkill.exe is a console-subsystem
        // program -- see the CREATE_NO_WINDOW doc comment above.
        .creation_flags(CREATE_NO_WINDOW)
        .status()
        .await;
    if let Err(err) = result {
        eprintln!("[sidecar] failed to run taskkill for pid {pid}: {err}");
    }
}

/// Unix counterpart of the Windows tree kill above. In production on macOS
/// the recorded pid *is* the bundled-python service itself (no `uv` wrapper
/// in between) and F2 spawns no children of its own, so a plain SIGKILL to
/// that one pid is sufficient. Same best-effort contract: every failure
/// (pid already gone, `kill` missing) is swallowed.
#[cfg(unix)]
async fn kill_process_tree_by_pid(pid: u32) {
    let result = Command::new("kill")
        .args(["-9", &pid.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;
    if let Err(err) = result {
        eprintln!("[sidecar] failed to run kill for pid {pid}: {err}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Settings;
    use std::path::PathBuf;
    use std::time::{Duration, Instant};

    // -- CREATE_NO_WINDOW (field report: a console window flashed on launch) --

    #[cfg(windows)]
    #[test]
    fn create_no_window_is_the_documented_win32_flag_value() {
        // winbase.h's CREATE_NO_WINDOW -- a typo'd magic number here would
        // silently stop suppressing the console window without any other
        // test noticing.
        assert_eq!(CREATE_NO_WINDOW, 0x0800_0000);
    }

    #[cfg(windows)]
    #[test]
    fn every_console_subsystem_child_this_module_spawns_uses_create_no_window() {
        // Source-contract regression test (this crate is Windows-only, so
        // there is no cross-platform harness to spawn a child and inspect
        // its console state from the outside): every call site in this file
        // that builds a `Command` for a real console-subsystem program
        // (`uv`/the bundled python.exe via `to_command`, `taskkill` via
        // `kill_process_tree_by_pid`) must apply `.creation_flags(CREATE_NO_WINDOW)`
        // on the same builder, or the fix silently regresses if a future
        // edit adds a new spawn site without it.
        let source = include_str!("sidecar.rs");

        let to_command_body = source
            .split("fn to_command(&self) -> Command {")
            .nth(1)
            .expect("expected to find to_command's body")
            .split("\n    }\n")
            .next()
            .expect("expected to find to_command's closing brace");
        assert!(
            to_command_body.contains(".creation_flags(CREATE_NO_WINDOW)"),
            "to_command() must apply CREATE_NO_WINDOW to the uv/python spawn"
        );

        let kill_fn_body = source
            .split("async fn kill_process_tree_by_pid(pid: u32) {")
            .nth(1)
            .expect("expected to find kill_process_tree_by_pid's body")
            .split("\n}\n")
            .next()
            .expect("expected to find kill_process_tree_by_pid's closing brace");
        assert!(
            kill_fn_body.contains(".creation_flags(CREATE_NO_WINDOW)"),
            "kill_process_tree_by_pid must apply CREATE_NO_WINDOW to the taskkill spawn"
        );
    }

    fn block_on<F: std::future::Future>(fut: F) -> F::Output {
        tokio::runtime::Runtime::new()
            .expect("tokio runtime")
            .block_on(fut)
    }

    // -- parse_ready_line -----------------------------------------------

    #[test]
    fn parse_ready_line_accepts_a_valid_line_and_yields_base_url_and_token() {
        let line = r#"{"event":"listening","port":51234,"token":"abc","pid":9001}"#;

        let ready = parse_ready_line(line).expect("valid ready line must parse");

        assert_eq!(ready.port, 51234);
        assert_eq!(ready.token, "abc");
        assert_eq!(ready.pid, 9001);
        assert_eq!(ready.base_url(), "http://127.0.0.1:51234");
    }

    #[test]
    fn parse_ready_line_rejects_non_json_without_panicking() {
        let err = parse_ready_line("not json at all").expect_err("non-JSON must be rejected");
        assert!(!err.message.is_empty());
    }

    #[test]
    fn parse_ready_line_rejects_a_different_event_without_panicking() {
        let line = r#"{"event":"log","level":"info","msg":"starting"}"#;
        parse_ready_line(line).expect_err("a non-listening event must be rejected");
    }

    #[test]
    fn parse_ready_line_rejects_a_line_missing_port_without_panicking() {
        let line = r#"{"event":"listening","token":"abc","pid":9001}"#;
        parse_ready_line(line).expect_err("a ready line missing port must be rejected");
    }

    // -- read_ready_line (timeout) ----------------------------------------

    #[test]
    fn read_ready_line_times_out_naming_the_command_when_the_stream_never_emits_one() {
        // A duplex pipe whose write half we keep alive without ever writing
        // to it: reads on the other end pend forever, exactly like a child
        // process that never prints a ready line but also never exits.
        let (_keep_alive, reader) = tokio::io::duplex(64);

        let result = block_on(read_ready_line(
            reader,
            Duration::from_millis(50),
            "uv run --directory services/transcription transcription-service serve",
        ));

        match result.expect_err("a stream with no ready line must time out") {
            SidecarError::Timeout { command } => {
                assert_eq!(
                    command,
                    "uv run --directory services/transcription transcription-service serve"
                );
            }
            other => panic!("expected a Timeout error, got {other:?}"),
        }
    }

    #[test]
    fn read_ready_line_ignores_malformed_lines_and_returns_the_first_valid_one() {
        let (mut writer, reader) = tokio::io::duplex(4096);

        block_on(async {
            use tokio::io::AsyncWriteExt;
            writer.write_all(b"not json\n").await.unwrap();
            writer
                .write_all(b"{\"event\":\"log\",\"msg\":\"starting\"}\n")
                .await
                .unwrap();
            writer
                .write_all(b"{\"event\":\"listening\",\"port\":4000,\"token\":\"t\",\"pid\":1}\n")
                .await
                .unwrap();
        });

        let ready = block_on(read_ready_line(
            reader,
            Duration::from_secs(2),
            "test command",
        ))
        .expect("a valid line after malformed ones must still be found");

        assert_eq!(ready.port, 4000);
    }

    // -- SidecarSpawnConfig / plan_sidecar --------------------------------

    fn settings_with(meetings_root: Option<&str>, base_url: Option<&str>) -> Settings {
        Settings {
            meetings_root: meetings_root.map(str::to_string),
            service: crate::config::ServiceSettings {
                base_url: base_url.map(str::to_string),
                ..Default::default()
            },
            model: crate::config::ModelSettings {
                id: Some("large-v3".to_string()),
                path: Some("C:\\models".to_string()),
                ..Default::default()
            },
            ..Default::default()
        }
    }

    #[test]
    fn dev_command_selects_the_llm_extra_by_gpu_presence() {
        // One of the two conflicting extras must ALWAYS be passed: a bare
        // `uv run` would sync a venv holding no llama.cpp at all.
        let settings = settings_with(Some("D:\\Meetings"), None);
        let config_path = PathBuf::from("C:\\cfg\\config.json");
        let app_dir = PathBuf::from("C:\\cfg");

        let with_gpu =
            SidecarSpawnConfig::dev_with_gpu(&settings, &config_path, &app_dir, true).args;
        let without_gpu =
            SidecarSpawnConfig::dev_with_gpu(&settings, &config_path, &app_dir, false).args;

        assert_eq!(
            with_gpu,
            vec![
                "run",
                "--directory",
                "services/transcription",
                "--extra",
                "llm-cuda",
                "transcription-service",
                "serve",
                "--port",
                "0",
            ]
        );
        assert_eq!(
            without_gpu,
            vec![
                "run",
                "--directory",
                "services/transcription",
                "--extra",
                "llm-cpu",
                "transcription-service",
                "serve",
                "--port",
                "0",
            ]
        );
    }

    #[test]
    fn plan_sidecar_renders_the_documented_dev_command_with_env_from_settings() {
        let settings = settings_with(Some("D:\\Meetings"), None);
        let config_path = PathBuf::from("C:\\AppData\\com.transcriber.desktop\\config.json");
        let app_dir = PathBuf::from("C:\\AppData\\com.transcriber.desktop");

        let plan = plan_sidecar(&settings, &config_path, &app_dir);

        let spawn_config = match plan {
            SidecarPlan::Spawn(config) => config,
            SidecarPlan::UseExisting { .. } => panic!("expected a Spawn plan"),
        };

        assert_eq!(spawn_config.program, "uv");
        // The llm extra depends on this machine's GPU probe; the exact
        // per-variant argument vectors are pinned in
        // `dev_command_selects_the_llm_extra_by_gpu_presence` below.
        let args: Vec<&str> = spawn_config.args.iter().map(String::as_str).collect();
        assert_eq!(args[..3], ["run", "--directory", "services/transcription"]);
        assert_eq!(args[3], "--extra");
        assert!(args[4] == "llm-cpu" || args[4] == "llm-cuda");
        assert_eq!(args[5..], ["transcription-service", "serve", "--port", "0"]);

        let env: std::collections::HashMap<_, _> = spawn_config.envs.into_iter().collect();
        assert_eq!(
            env.get("TRANSCRIBER_CONFIG_PATH").map(String::as_str),
            Some("C:\\AppData\\com.transcriber.desktop\\config.json")
        );
        assert_eq!(
            env.get("TRANSCRIBER_APP_DIR").map(String::as_str),
            Some("C:\\AppData\\com.transcriber.desktop")
        );
        assert_eq!(
            env.get("TRANSCRIBER_ALLOWED_ROOTS").map(String::as_str),
            Some("D:\\Meetings")
        );
        assert_eq!(
            env.get("TRANSCRIBER_VAULT_ROOT").map(String::as_str),
            Some("D:\\Meetings")
        );
        assert_eq!(
            env.get("TRANSCRIBER_MODEL_PATH").map(String::as_str),
            Some("C:\\models")
        );
        assert_eq!(
            env.get("TRANSCRIBER_MODEL").map(String::as_str),
            Some("large-v3")
        );
    }

    // -- SidecarSpawnConfig::production (T9) ------------------------------

    /// Creates the platform's bundled-interpreter path under `app_dir` (an
    /// empty stand-in file -- `production` only checks for its existence, it
    /// never executes it in these tests) so `production`/`plan_sidecar` can
    /// be exercised as if a real installed app folder were present.
    fn write_fake_bundled_python(app_dir: &Path) {
        let python_exe = app_paths::bundled_python_exe(app_dir);
        let python_dir = python_exe
            .parent()
            .expect("bundled python has a parent dir");
        std::fs::create_dir_all(python_dir).expect("create fake pyenv python dir");
        std::fs::write(&python_exe, b"not a real interpreter").expect("write fake bundled python");
    }

    #[test]
    fn production_builds_the_documented_argv_and_env_when_the_bundled_runtime_exists() {
        let app_dir = tempfile::tempdir().expect("tempdir");
        write_fake_bundled_python(app_dir.path());

        // No `model.path` override here (unlike `settings_with`'s dev-test
        // default of "C:\\models") -- this exercises production's *default*
        // model directory, computed from `app_dir` via `app_paths::model_dir`
        // rather than trusted verbatim from settings.
        let mut settings = settings_with(Some("D:\\Meetings"), None);
        settings.model.path = None;
        let config_path = app_dir.path().join("config.json");

        let config = SidecarSpawnConfig::production(&settings, &config_path, app_dir.path())
            .expect("production must succeed when the bundled runtime exists");

        assert_eq!(
            config.program,
            app_paths::bundled_python_exe(app_dir.path())
                .display()
                .to_string()
        );
        assert_eq!(
            config.args,
            vec![
                "-m",
                "transcription",
                "serve",
                "--port",
                "0",
                "--config",
                &config_path.display().to_string(),
            ]
        );

        let env: std::collections::HashMap<_, _> = config.envs.into_iter().collect();
        assert_eq!(
            env.get("TRANSCRIBER_APP_DIR").map(String::as_str),
            Some(app_dir.path().display().to_string()).as_deref()
        );
        assert_eq!(
            env.get("TRANSCRIBER_MODEL_PATH").map(String::as_str),
            Some(
                app_dir
                    .path()
                    .join("models")
                    .join("faster-whisper-large-v3")
                    .display()
                    .to_string()
            )
            .as_deref()
        );
        assert_eq!(
            env.get("TRANSCRIBER_ALLOWED_ROOTS").map(String::as_str),
            Some("D:\\Meetings")
        );
        assert_eq!(
            env.get("TRANSCRIBER_VAULT_ROOT").map(String::as_str),
            Some("D:\\Meetings")
        );
        assert_eq!(
            env.get("TRANSCRIBER_MODEL").map(String::as_str),
            Some("large-v3")
        );
    }

    #[test]
    fn production_never_lets_a_crafted_model_path_escape_the_app_folder() {
        let app_dir = tempfile::tempdir().expect("tempdir");
        write_fake_bundled_python(app_dir.path());

        let mut settings = settings_with(Some("D:\\Meetings"), None);
        settings.model.path = Some(r"..\..\..\Windows\System32".to_string());
        let config_path = app_dir.path().join("config.json");

        let config = SidecarSpawnConfig::production(&settings, &config_path, app_dir.path())
            .expect("production must succeed");

        let env: std::collections::HashMap<_, _> = config.envs.into_iter().collect();
        let model_path = env
            .get("TRANSCRIBER_MODEL_PATH")
            .expect("TRANSCRIBER_MODEL_PATH must be set");
        assert!(
            Path::new(model_path).starts_with(app_dir.path().join("models")),
            "expected {model_path:?} to stay under {:?}",
            app_dir.path().join("models")
        );
    }

    #[test]
    fn production_fails_with_a_typed_error_naming_the_missing_path_when_pyenv_is_absent() {
        let app_dir = tempfile::tempdir().expect("tempdir");
        // Deliberately no `pyenv/python/python.exe` written under app_dir.

        let settings = settings_with(Some("D:\\Meetings"), None);
        let config_path = app_dir.path().join("config.json");

        let err = SidecarSpawnConfig::production(&settings, &config_path, app_dir.path())
            .expect_err("production must fail when the bundled runtime is missing");

        match err {
            SidecarError::MissingRuntime { path } => {
                let expected = app_paths::bundled_python_exe(app_dir.path())
                    .display()
                    .to_string();
                assert_eq!(path, expected);
            }
            other => panic!("expected MissingRuntime, got {other:?}"),
        }
    }

    #[test]
    fn plan_sidecar_spawns_the_production_command_when_the_bundled_runtime_exists() {
        let app_dir = tempfile::tempdir().expect("tempdir");
        write_fake_bundled_python(app_dir.path());

        let settings = settings_with(Some("D:\\Meetings"), None);
        let config_path = app_dir.path().join("config.json");

        let plan = plan_sidecar(&settings, &config_path, app_dir.path());

        match plan {
            SidecarPlan::Spawn(config) => {
                assert_eq!(
                    config.program,
                    app_paths::bundled_python_exe(app_dir.path())
                        .display()
                        .to_string()
                );
            }
            SidecarPlan::UseExisting { .. } => {
                panic!("expected a Spawn plan against the bundled runtime")
            }
        }
    }

    #[test]
    fn plan_sidecar_falls_back_to_dev_when_no_bundled_runtime_is_present() {
        let app_dir = tempfile::tempdir().expect("tempdir");
        // No pyenv/python/python.exe written -- this is the dev-machine case.

        let settings = settings_with(Some("D:\\Meetings"), None);
        let config_path = app_dir.path().join("config.json");

        let plan = plan_sidecar(&settings, &config_path, app_dir.path());

        match plan {
            SidecarPlan::Spawn(config) => assert_eq!(config.program, "uv"),
            SidecarPlan::UseExisting { .. } => panic!("expected a Spawn plan"),
        }
    }

    #[test]
    fn plan_sidecar_with_a_configured_base_url_does_not_spawn() {
        let settings = settings_with(Some("D:\\Meetings"), Some("http://127.0.0.1:8756"));
        let config_path = PathBuf::from("C:\\AppData\\com.transcriber.desktop\\config.json");
        let app_dir = PathBuf::from("C:\\AppData\\com.transcriber.desktop");

        let plan = plan_sidecar(&settings, &config_path, &app_dir);

        match plan {
            SidecarPlan::UseExisting { base_url } => {
                assert_eq!(base_url, "http://127.0.0.1:8756");
            }
            SidecarPlan::Spawn(_) => panic!("a configured base_url must not spawn a sidecar"),
        }
    }

    // -- Sidecar::restart --------------------------------------------------

    #[cfg(windows)]
    #[test]
    fn restart_terminates_the_previous_child_before_spawning() {
        block_on(async {
            let mut sidecar = Sidecar::new();

            // A long-running "previous" process: if restart() merely waited
            // for it to exit naturally instead of killing it, this test
            // would take a long time (or hit the outer timeout below).
            let old_config = SidecarSpawnConfig {
                program: "ping".to_string(),
                args: vec!["-n".to_string(), "120".to_string(), "127.0.0.1".to_string()],
                envs: vec![],
            };
            sidecar
                .spawn(&old_config)
                .expect("spawning the long-running placeholder must succeed");

            let new_config = SidecarSpawnConfig {
                program: "cmd".to_string(),
                args: vec!["/C".to_string(), "exit".to_string(), "0".to_string()],
                envs: vec![],
            };

            let start = Instant::now();
            let restart_result =
                tokio::time::timeout(Duration::from_secs(10), sidecar.restart(&new_config)).await;
            let elapsed = start.elapsed();

            restart_result
                .expect("restart must not hang waiting for the old child")
                .expect("restart must succeed");

            assert!(
                elapsed < Duration::from_secs(5),
                "restart took {elapsed:?}; the old (120s) child was not killed before spawning"
            );
        });
    }

    /// Unix twin of the windows restart test above, with `/bin/sleep` and
    /// `sh -c` standing in for `ping -n` and `cmd /C`.
    #[cfg(unix)]
    #[test]
    fn restart_terminates_the_previous_child_before_spawning() {
        block_on(async {
            let mut sidecar = Sidecar::new();

            // A long-running "previous" process: if restart() merely waited
            // for it to exit naturally instead of killing it, this test
            // would take a long time (or hit the outer timeout below).
            let old_config = SidecarSpawnConfig {
                program: "/bin/sleep".to_string(),
                args: vec!["120".to_string()],
                envs: vec![],
            };
            sidecar
                .spawn(&old_config)
                .expect("spawning the long-running placeholder must succeed");

            let new_config = SidecarSpawnConfig {
                program: "/bin/sh".to_string(),
                args: vec!["-c".to_string(), "exit 0".to_string()],
                envs: vec![],
            };

            let start = Instant::now();
            let restart_result =
                tokio::time::timeout(Duration::from_secs(10), sidecar.restart(&new_config)).await;
            let elapsed = start.elapsed();

            restart_result
                .expect("restart must not hang waiting for the old child")
                .expect("restart must succeed");

            assert!(
                elapsed < Duration::from_secs(5),
                "restart took {elapsed:?}; the old (120s) child was not killed before spawning"
            );
        });
    }

    // -- terminate() kills the recorded service pid too (E6) --------------

    /// Whether a process with `pid` currently exists, per `tasklist` --
    /// used instead of trying to `wait()` on a process this test does not
    /// own (the whole point here is that `terminate()` reaches a pid that
    /// was never a *direct* child of this `Sidecar`, exactly like F2's real
    /// pid inside `uv`'s process tree).
    #[cfg(windows)]
    async fn process_exists(pid: u32) -> bool {
        let output = Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}")])
            .stdin(Stdio::null())
            .output()
            .await
            .expect("run tasklist");
        String::from_utf8_lossy(&output.stdout).contains(&pid.to_string())
    }

    #[cfg(windows)]
    #[test]
    fn terminate_kills_the_recorded_service_pid_not_just_the_spawned_child() {
        // E6: the real F2 process is a *grandchild* of the `uv` process
        // `Sidecar` directly spawns -- `child.start_kill()` alone never
        // reaches it. Standing in for that: a long-running process this
        // `Sidecar` never spawned itself, recorded the same way F2's real
        // ready-line pid would be.
        block_on(async {
            let mut sidecar = Sidecar::new();

            let uv_stub = SidecarSpawnConfig {
                program: "cmd".to_string(),
                args: vec!["/C".to_string(), "exit".to_string(), "0".to_string()],
                envs: vec![],
            };
            sidecar
                .spawn(&uv_stub)
                .expect("spawning the uv-stand-in must succeed");

            let mut grandchild_stand_in = std::process::Command::new("ping")
                .args(["-n", "120", "127.0.0.1"])
                .spawn()
                .expect("spawn the grandchild stand-in");
            let grandchild_pid = grandchild_stand_in.id();
            sidecar.record_service_pid(grandchild_pid);

            assert!(
                process_exists(grandchild_pid).await,
                "test setup: the grandchild stand-in must actually be running first"
            );

            let terminate_result =
                tokio::time::timeout(Duration::from_secs(10), sidecar.terminate()).await;
            terminate_result.expect("terminate() must not hang");

            // Windows can take a beat to reflect the kill in `tasklist`.
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline && process_exists(grandchild_pid).await {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            assert!(
                !process_exists(grandchild_pid).await,
                "terminate() must kill the recorded service pid, not just the spawned uv child"
            );

            // Best-effort cleanup in case the assertion above already
            // failed and the process is somehow still alive.
            let _ = grandchild_stand_in.kill();
            let _ = grandchild_stand_in.wait();
        });
    }

    /// Unix twin of the windows terminate test above. The stand-in is a
    /// direct child of the *test* process (never of `Sidecar`), so instead
    /// of `tasklist` its exit is observed via `try_wait` and the SIGKILL
    /// `terminate()` delivers is asserted on the exit status itself.
    #[cfg(unix)]
    #[test]
    fn terminate_kills_the_recorded_service_pid_not_just_the_spawned_child() {
        use std::os::unix::process::ExitStatusExt;

        block_on(async {
            let mut sidecar = Sidecar::new();

            let uv_stub = SidecarSpawnConfig {
                program: "/bin/sh".to_string(),
                args: vec!["-c".to_string(), "exit 0".to_string()],
                envs: vec![],
            };
            sidecar
                .spawn(&uv_stub)
                .expect("spawning the uv-stand-in must succeed");

            let mut service_stand_in = std::process::Command::new("/bin/sleep")
                .arg("120")
                .spawn()
                .expect("spawn the service stand-in");
            sidecar.record_service_pid(service_stand_in.id());

            let terminate_result =
                tokio::time::timeout(Duration::from_secs(10), sidecar.terminate()).await;
            terminate_result.expect("terminate() must not hang");

            let deadline = Instant::now() + Duration::from_secs(5);
            let status = loop {
                if let Some(status) = service_stand_in.try_wait().expect("try_wait") {
                    break status;
                }
                if Instant::now() >= deadline {
                    let _ = service_stand_in.kill();
                    let _ = service_stand_in.wait();
                    panic!("terminate() must kill the recorded service pid");
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            };
            assert_eq!(
                status.signal(),
                Some(9),
                "the recorded service pid must have been SIGKILLed by terminate()"
            );
        });
    }
}
