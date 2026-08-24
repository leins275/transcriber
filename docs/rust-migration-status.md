# Rust-only migration — state as of 2026-08-24

The migration is **paused, not abandoned**. `feat/rust-only` is published and
green in CI terms (tests, clippy, fmt), but it is **not releasable**: the first
LLM job run against the real model kills the process. See "Why it is paused".

`main` is untouched and still ships the Python service. Nothing here blocks
releasing from `main`.

## What the branch does

Replaces the `services/transcription` FastAPI sidecar with an in-process Rust
engine. 13 commits, `ac6469a..aea3490`.

- `crates/wire` — the on-disk formats, byte-compatible with what Python wrote.
  Ten real-vault transcripts round-trip byte for byte, including CPython float
  `repr` rules and CRLF in `.md` files.
- `crates/whisper-sys` — vendored whisper.cpp v1.9.2 against the ggml that
  `llama-cpp-sys-2` vendors (0.18.0). One shared ggml with `GGML_BACKEND_DL`;
  see `crates/whisper-sys/PINS.md` for why the three pins must agree.
- `crates/engine` — STT, LLM, diarization, media, PDF (Typst), ledger, job
  queue. One worker thread owns all models; panics are caught per job and the
  runner rebuilt.
- `crates/fetcher` — the only crate with outbound TLS, host-allowlisted.
- `services/transcription/` (83 files), `sidecar.rs`, `service/http.rs`,
  `build_pyenv.py` and the NSIS python-kill hooks are deleted.

Installer: 94 MB, engine payload 184 MB (was ~420 MB of baked pyenv).

## Why it is paused

**The first LLM job crashes the whole process.** Reproduced 2026-08-24 in a dev
build on a real meeting: a `facts` job ran ~100 s and the process died with no
Rust error. The ledger row is `failed` / `internal` with "job was interrupted by
a service restart", which `Ledger::reconcile_interrupted` writes at the *next*
startup — i.e. the process died mid-job rather than failing the job.

Not yet diagnosed. What is known:

- It was `job-00000001`: the only job ever run in that dev build. So it is not
  established that `facts` is special — summarize and action-items were never
  exercised in the Rust build at all.
- ~100 s is about the time it takes to load the 19 GB GGUF, so the crash is at
  or shortly after model load.
- Machine had 63 GB RAM, 37 GB free — plain OOM is unlikely.
- `llm_gpu_layers` defaults to `-1` (offload everything) while `.devapp` ships
  only CPU ggml backends. Untested interaction, and the first thing to check.
- This is the residual risk the design accepted: `catch_unwind` catches Rust
  panics, not `GGML_ABORT`, SEGV, or a C++ `abort()`. The sidecar survived
  those. The escape hatch was designed but not built — `engine` does not depend
  on Tauri and speaks serde types, so an `engine-host.exe` talking serde_json
  over stdio would restore full isolation behind the same `EngineHandle`.

To get the actual failure, capture the process's stderr (the crash message goes
there and is lost when the app is launched from a shortcut), or drive the
extraction path from an `#[ignore]` test against the real model. There is no
dev CLI — `transcriber-cli` was planned and never built, which is precisely
what made this hard to diagnose. **Build it first if this is picked up again.**

## Other things not verified

- **GPU has never been run.** Needs a `ggml-cuda.dll` built against ggml 0.18.0;
  llama.cpp's own release binaries target a different ggml and will not
  register. `cuda_runtime_present` / `llm_gpu_build_present` still return
  `None`, so the settings UI has no GPU story.
- **Diarization has never been quality-tested.** `pyannote-rs` uses greedy
  cosine assignment instead of pyannote.audio's clustering and has no
  min/max-speaker controls. The phase-D bake-off against the Python pipeline
  was never run.
- **STT quality is close but not equal.** A/B on a real 8-minute Russian
  recording: 85.7 % word agreement, 1087 words vs 1024, 1.27× realtime on CPU.
  Duration matches exactly. Note the VAD model is not optional — without it
  whisper transcribes silence and agreement drops to 73.5 %.
- **Side-by-side install was verified by reading the generated NSIS script**,
  not by installing next to a release.

## A real bug found on the way, worth keeping even if the migration is dropped

`+crt-static` never reached the shipped binary. `apps/desktop/src-tauri/.cargo/
config.toml` sets it, but cargo discovers `.cargo/config.toml` only from its
working directory upward:

- `tauri dev` runs cargo from `src-tauri` → applied.
- `tauri build` — what `scripts/build_installer.py` runs, i.e. what ships → not
  applied. `dumpbin /dependents` on the built exe shows 9-10 dynamic-CRT
  imports, so the installed app needs the VC++ redistributable that FR-9 exists
  to avoid.
- CI's root-level `cargo test` / `clippy` → not applied either, so CI never
  exercises the linkage that ships.

`aea3490` fixes the half that broke the build (`knf-rs-sys` picks its runtime
from its own `KNF_STATIC_CRT` and ignores `crt-static`). **The scope problem is
not fixed** — the file has to move to the workspace root, which changes what
ships and how CI links, so it was left for a decision. This applies to `main`
too, which has the same config in the same place.

## Running what exists

```bash
make dev                    # assembles .devapp/ and starts tauri dev
make installer              # needs TAURI_SIGNING_PRIVATE_KEY
uv run scripts/build_installer.py --no-updater      # local, no key
uv run scripts/build_installer.py --side-by-side    # installs beside a release
```

`scripts/dev_app_dir.py` builds `.devapp/` by hardlinking the staged payload and
the whisper weights, and points `TRANSCRIBER_LLM_MODEL_PATH` at an existing
install's GGUF rather than copying 19 GB.

Watch out: the Rust engine's ready-marker is `<file>.ready`, while the Python
service wrote one bare `.ready` per directory. A Python-era install therefore
looks entirely un-downloaded to the Rust engine and re-fetches ~23 GB.

Two traps in the loop: `npm ci` cannot run while `tauri dev` holds
`esbuild.exe`, and `scripts/tests/test_version.py` mutates the real tree and
left it at `9.9.9` once when a write failed during a concurrent build.

## Test counts at the pause

700 Rust, 268 frontend, 157 build-script. clippy `-D warnings` and fmt clean.
