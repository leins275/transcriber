# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Transcriber: a local-first, single-user desktop app that turns dropped meeting recordings into transcripts (faster-whisper large-v3, CUDA-first) plus an LLM-generated summary (which carries the notable facts and the action items as sections), filed into a per-project meetings vault. (Separate facts and action-items extractions existed once; both were retired in favour of the summary, and legacy `facts/`, `action items/` and `exports/` trees stay on disk unread.) **Everything runs locally — no cloud LLM, no cloud STT, no external integrations, ever.** KISS/YAGNI; Windows is the primary platform (macOS installer exists but is less tested).

## Monorepo layout — three payloads

| Path | Payload | Toolchain |
|---|---|---|
| `apps/desktop/` | Tauri 2 + React UI; `src-tauri/` is the Rust shell | npm + cargo |
| `services/transcription/` | Python transcription/LLM service (FastAPI + CLI) | `uv` only — never system `python` |
| `crates/vault/` | Rust library: vault naming/routing rules | cargo |

Supporting: `installer/` (NSIS hooks), `scripts/` (bootstrap, build, version sync — has its own pytest suite in `scripts/tests/`), `docs/` (setup, config contract, releasing, smoke checklists), `specs/` (per-feature spec/plan/verification documents — the design history).

Local-only, not part of the build: `vexa/` (gitignored reference repo), `local/` (zips), `crates/whisper-sys` (empty untracked leftover of the paused Rust-only migration — main still ships the Python service).

## Commands

QA fans out across all three payloads via the root `Makefile` (`make` exists only after `scripts/bootstrap.ps1` has run once; every target's direct equivalents are commented in the Makefile):

```
make format   # cargo fmt --all | prettier | ruff format
make lint     # clippy -D warnings | eslint | ruff check | sync_version --check | verify_locks --check
make type     # cargo check | tsc --noEmit | mypy src
make test     # cargo test --workspace | vitest | pytest | pytest scripts/tests
```

PowerShell 5.1 has no `&&` — run chained commands one at a time.

Single tests:

- Python: `uv run --directory services/transcription pytest tests/test_llm_units.py -q` (add `-k name` to narrow). The default run is model-free, GPU-free and network-free (<30 s); the opt-in GPU integration test is `uv run pytest -m gpu` from `services/transcription/` and self-skips without a configured sample.
- Rust: `cargo test -p vault <name>` / `cargo test -p transcriber-desktop <name>` (workspace members are `vault` and `transcriber-desktop`)
- TS: `npm --prefix apps/desktop run test -- src/App.test.tsx`
- Build scripts: `uv run --with pytest -- pytest scripts/tests -q`

Dev inner loop (spawns the Python sidecar automatically; app opens even if the sidecar never gets ready):

```
cd apps/desktop && npm run tauri dev
```

Installer build: `make installer` → `dist/Transcriber_<version>_x64-setup.exe` (+ `.sha256`, `build-manifest.json`). It bakes a relocatable CPython into `apps/desktop/src-tauri/resources/pyenv/` and runs `tauri build -- --locked`. The bake is CPU-only by design — the CUDA runtime (~1.4 GB of `nvidia-*-cu12` wheels) is downloaded at first run into the install dir's `runtime\`, because 32-bit `makensis` cannot compile the CUDA-baked payload.

## Architecture

**App ↔ service split.** The Tauri Rust shell (`apps/desktop/src-tauri/src/`) spawns `services/transcription` as a localhost-HTTP sidecar (`sidecar.rs`) and talks to it from React via Tauri commands (`commands/`). A dropped recording runs the drop-to-insights chain automatically: transcribe → summarize → export, chained app-side in `jobs.rs` (the service refuses a derived job until `transcript.json` exists, so stages submit as their predecessor lands `Done`). The summarize stage is skipped when no LLM model is installed (which also ends the chain before export), the chain advances only off a successful stage, and it never fires for a manual re-transcribe. The export (`export.md` + a share-named PDF) lands in the meeting folder itself under stable names, overwritten on re-export. The service does all ML work: transcription (faster-whisper), diarization (optional pyannote extra), LLM summarization (bundled llama.cpp), PDF/report export. `config.json`'s `service.base_url` set to a URL means "connect, don't spawn" — the dev/ops mode for running the service by hand. The Python package is deliberately self-contained: it imports nothing from the rest of the repo and QAs standalone.

**Sidecar handshake (ready-line contract).** `serve` prints exactly **one** JSON line to stdout for the whole process lifetime — `{"event": "listening", "port": ..., "token": ..., "pid": ...}` — after the socket is bound; every other log line goes to stderr as JSON. The Rust sidecar spawner depends on this; never add stdout prints to the service. It binds `127.0.0.1` only, with a bearer token.

**One serial worker.** All job types (transcribe, summarize, export, index) share a single serial queue — whisper, the LLM and the search embedder never infer concurrently, and with the default `llm_keep_loaded: false` the GGUF working set is released after each LLM job (the chat route deliberately keeps it loaded between turns; the embedder always unloads after an index pass). Search queries and chat completions also run through this queue (`run_serial`). Degradation over failure is the pattern throughout: diarization failures, PDF render errors, a missing embedding model or an unloadable sqlite-vec extension all record warnings and still deliver the primary artifact (text-only search included).

**Hybrid search + chat + MCP.** The service owns a rebuildable search index (`<vault_root>/.transcriber/index.sqlite3` — it travels with its vault; app-dir fallback only when no vault root is set; sqlite-vec + FTS5, bge-m3 GGUF embeddings on CPU) filled by the `index` job, which the app fires quietly after every finished job and note save. Settings offers the bge-m3 download ("Enable vector search", the `embedding_model_download` command trio); docs indexed text-only re-embed automatically on the first pass after the model lands. `POST /v1/search` fuses vector/BM25/exact-title/trigram channels with weighted RRF; `POST /v1/chat` streams a RAG answer (SSE) that the Rust shell forwards to React over a `tauri::ipc::Channel`. `transcriber-mcp` (a console script in the Python package) is a standalone stdio MCP server over the same vault+index for Claude Desktop — works with the app closed. Per-meeting `note.md` (edited in the app) sits on top. The chat lives as the library's third tab (redesign turn 9 — there are no project pages): project selector in the tab, an index-status chip/panel backed by `GET /v1/index/status`, and conversations persisted as JSON in the vault's reserved `<PROJECT>/chats/` directory (list/read/save/rename/delete via `commands/chats.rs`; sources stored by vault-relative dir, resolved to entry ids on read). `speakers.json` names feed project-level suggestions, and diarization stores per-speaker voice embeddings in `transcript.json`; after a diarized transcription, cross-meeting speaker recognition (`speaker_matching.py`, threshold `speaker_match_threshold`) pre-names voices already named in sibling meetings — additive only, operator assignments always win. Decode languages: ru/en/tr (constrained auto-detect).

**Service config layering** (lowest to highest): built-in defaults < `config.json` < `TRANSCRIBER_*` env vars < CLI flags. Full key table in `services/transcription/README.md`. Secrets (`token`, `hf_token`) never travel via argv and never appear in `/health` output.

**Shared config file.** App and service share `%APPDATA%\com.transcriber.desktop\config.json` (schema in `docs/config-contract.md` — the code in `config.rs` is the contract's authority). Key mechanism: the Rust `Settings` structs `#[serde(flatten)]` unknown keys at every level, so the Python service reads service-only flat keys (`diarize`, `llm_model`, `llm_ctx`, `llm_gpu_layers`, …) from the same file without the app schema knowing them.

**LLM catalog.** `services/transcription/src/transcription/llm_catalog.py` pins exactly one GGUF model: Qwen3.5-9B (fits a 12 GB GPU fully). There is deliberately no model switching (the old 35B option and the `select_llm_model` command were removed); config load migrates a retired `llm_model` id to the default, and the hand-picked-GGUF escape hatch (`llm_model_file` etc.) still wins over the catalog. `llama-cpp-python` ships as two mutually exclusive uv extras of the same pinned version — `llm-cpu` (what the installer bakes) and `llm-cuda`; because the wheels share name+version, switching by hand needs `uv sync --extra llm-cuda --reinstall-package llama-cpp-python` once. `llm_gpu_layers: -1` auto-fits whole layers to free VRAM via NVML.

**Whisper weights are a prerequisite, not the service's job.** The local provider always loads with `local_files_only=True` and never downloads weights; a missing `large-v3` snapshot under `model_path` fails every job with `error_kind="model_load"`. Model/CUDA-runtime downloads happen through the app's first-run wizard and download endpoints, not inside the provider.

**Vault rules live in Rust.** `crates/vault` owns naming/routing of recordings and artifacts into the meetings vault; the app consumes it. Don't reimplement path logic elsewhere.

**Version.** `version.txt` is the single source of truth. Never hand-edit versions in the five manifests or `Cargo.lock` — use `uv run scripts/sync_version.py --set X.Y.Z`; `--check` (run by `make lint`) fails on drift.

## Releasing — commit subjects are load-bearing

Merging to `main` **is** the ship decision (no release PRs). `tag.yml` computes the next version from conventional-commit subjects since the last `v*` tag: `feat:` → minor, `fix:`/`perf:` → patch, `feat!:`/`BREAKING CHANGE:` → major, everything else → no release. CI then commits the bump + CHANGELOG to `main`, tags `vX.Y.Z`, and `release.yml` builds both installers (Windows NSIS + macOS dmg) and publishes one GitHub Release. A failed build leaves a tag with no release — re-run `release.yml` on the tag, it's idempotent. Details in `docs/releasing.md`.

## Specs

Each feature under `specs/<name>/` carries spec/plan/verification docs. When code and a spec disagree, code wins, but the docs in `docs/` are kept current — update them when behavior they describe changes.
