---
slug: 260909-search-index-in-vault
created: 2026-09-09
status: approved
base_ref: <git sha, recorded at blueprint approval>
---

# Blueprint: Search index lives in the vault (residual: retire the app-dir copy)

## Summary

The request — "store the indexes in the vault, not in the app folder" — is **already
implemented on main** since 0.18.0 (commit `1f8407c`, 2026-09-02): the service resolves
`index_db_path` to `<vault_root>/.transcriber/index.sqlite3` whenever a vault root is
configured (`services/transcription/src/transcription/config.py:421-428`), the Rust
sidecar spawner passes `TRANSCRIBER_VAULT_ROOT` from `meetings_root` on every spawn
(`apps/desktop/src-tauri/src/sidecar.rs:113-119, 185-189`), the `index` job and the
standalone `transcriber-mcp` server both open `config.index_db_path` (`jobs.py:388`,
`mcp_server.py:93`), the indexer and MCP listings skip `.transcriber/` as a non-project
dot-dir, and `services/transcription/README.md:92` documents the location. Existing tests
in `tests/test_config.py:117-150` pin all three resolution branches.

The one genuine residual: an install upgraded from 0.17.x or earlier keeps its old
`<app_dir>/data/index.sqlite3` (plus `-wal`/`-shm`) on disk. Nothing reads it any more,
the vault index is rebuilt by the app's startup catch-up pass (`App.tsx:196-205`), and the
orphan simply sits in the app folder forever — which is literally what the operator asked
not to have. This blueprint scopes exactly that: delete the orphaned app-dir index at
service startup once a vault root is configured, and say so in the service README.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists and `apps/desktop/src-tauri/Cargo.toml` depends on `tauri` (Tauri 2 shell); NSIS packaging in `installer/installer_hooks.nsh`.
- `web` — `apps/desktop/package.json` names `react` (18.3) and `vite` (5.4); the UI is the app's own single-user library, i.e. internal-tool UI. No UI is touched by this feature.
- `cli` — `services/transcription/pyproject.toml` `[project.scripts]` declares `transcription-service` and `transcriber-mcp`; `argparse` subcommands in `services/transcription/src/transcription/cli.py`.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2 + Rust | `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/Cargo.toml` |
| UI | React 18 + Vite 5 + TypeScript, vitest | `apps/desktop/package.json` |
| Service | Python >=3.12, FastAPI, argparse CLI, `uv`-managed | `services/transcription/pyproject.toml` |
| Vault rules | Rust library crate | `crates/vault/Cargo.toml` |
| Search index | SQLite + sqlite-vec + FTS5, one file per vault | `services/transcription/src/transcription/search/index_db.py` |
| Tests | cargo test / vitest / pytest (root Makefile fan-out) | `Makefile` |

Makefile QA targets present: format, lint, type, test (no aggregate `qa` target).

## Requirements

- **FR-1** (must): When the service starts with a vault root configured (so `index_db_path` resolves inside the vault), a leftover pre-0.18 index at `<app_dir>/data/index.sqlite3` is deleted, together with its `-wal` and `-shm` sidecars.
  - [ ] Given `<app_dir>/data/index.sqlite3`, `index.sqlite3-wal` and `index.sqlite3-shm` exist and `index_db_path` points inside the vault, after startup none of the three files exist.
  - [ ] The vault index file (`<vault_root>/.transcriber/index.sqlite3`), if present, is untouched by the cleanup (same bytes before and after).
  - [ ] The cleanup runs inside the FastAPI `lifespan` startup of `create_app` (i.e. entering `TestClient(app)` triggers it) — before the job manager starts, so the startup catch-up index job never races it.
  - [ ] A removal is logged once as a structured stderr log record with `event="legacy_index_removed"`; nothing is ever printed to stdout (ready-line contract).
- **FR-2** (must): The cleanup never deletes an index that is still in use.
  - [ ] When `index_db_path` resolves to `<app_dir>/data/index.sqlite3` itself (no vault root, or an explicit `index_db_path` naming that file), the file is left in place and no log record is emitted.
  - [ ] When no legacy file exists, startup proceeds silently (no error, no log record).
  - [ ] A filesystem error while deleting (e.g. the file is locked by another process) is logged as a warning and does not prevent the service from starting — degradation over failure.
- **FR-3** (should): The service README's `index_db_path` row states that a pre-0.18 app-dir index is removed at startup once a vault root is set.
  - [ ] `services/transcription/README.md` line for `index_db_path` mentions the startup removal of the legacy `<app_dir>/data/index.sqlite3`.

**Non-functional**:

- **NFR-1**: The cleanup adds no measurable startup latency — three `unlink(missing_ok=True)` calls, no directory walk, no SQLite open.

## Out of scope

- Moving/migrating the old app-dir index into the vault (see Assumptions — it is text-only by construction and the vault index is rebuilt automatically).
- Changing the vault-less fallback (`<app_dir>/data/index.sqlite3` when no vault root is configured) — index jobs are disabled in that mode anyway.
- Any change to `config.rs`, the sidecar env contract, `docs/config-contract.md` (which deliberately does not list service-only keys), the index-status UI, or the MCP server (read-only by design; benefits after one app launch).
- Removing `.transcriber/` from a vault when the vault root changes — the index is per-vault on purpose.
- macOS-specific handling: `pathlib` unlink is platform-neutral; both platforms are in scope with the same code.

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task; **mandatory** (desktop, web, cli).
- `testing-toolkit:python-testing-patterns` — pytest service tests (desktop/web/cli Tests row, signal: `services/transcription/tests`).
- `frontend-toolkit:internal-ui` — internal-tool UI tasks; **mandatory** per `web` (not installed on this machine; no UI task in this blueprint).
- `frontend-toolkit:ui-ux-pro-max` — internal-tool UI tasks (web UI row; not applicable here).
- `devops-toolkit:devops-rollout-plan` — packaging/release tasks (desktop Packaging row, cli Release row; not applicable here).

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui` (unavailable on this machine and out of domain — no UI task; degrade gracefully)

## Architecture

Single service-side change, no new modules:

- `services/transcription/src/transcription/search/index_db.py` — add a small pure-path helper
  `remove_legacy_app_dir_index(app_dir: Path, index_db_path: str | Path) -> bool` next to the
  existing file-lifecycle code (`_migrate_or_recreate` already knows the `("", "-wal", "-shm")`
  triple). Legacy path = `app_dir / "data" / "index.sqlite3"`. If `Path(index_db_path).resolve()`
  equals the legacy path's `resolve()` → return `False` without touching anything. Otherwise
  unlink the three files with `missing_ok=True`; return `True` iff the main file existed and was
  removed; on `OSError` log `logger.warning(..., extra={"event": "legacy_index_remove_failed"})`
  and return `False`. On `True`, the caller logs `event="legacy_index_removed"` (or the helper
  does — one record either way).
- `services/transcription/src/transcription/app.py` — call the helper as the first statement of
  `lifespan` in `create_app` (before `ledger.reconcile_interrupted()` / `job_manager.start()`),
  using `config.app_dir` and `config.index_db_path`. `create_app` itself stays side-effect-free
  (NFR-1 in the module docstring): only entering the lifespan touches disk.
- `services/transcription/README.md` — extend the `index_db_path` row (line 92).

Data flow is unchanged: Rust `config.rs` → `meetings_root` → `TRANSCRIBER_VAULT_ROOT` →
`config.py` → in-vault `index_db_path` → `JobManager._get_index_db` / MCP `_Vault.index()`.

**Risks**: (1) Deleting a file another process holds open — on Windows the unlink fails with
`PermissionError`; T1 catches `OSError`, warns, and continues (FR-2). (2) A hand-written config
that sets `index_db_path` to the app-dir file must not lose it — the resolve-equality guard in
T1 covers that (FR-2). (3) Anything printed to stdout would break the sidecar ready-line
contract — T1 uses the existing `transcription` logger only (FR-1).

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1 |

## Tasks

### [ ] T1: Delete the orphaned app-dir search index at service startup  [deps: —]

- **Files**: `services/transcription/src/transcription/search/index_db.py`, `services/transcription/src/transcription/app.py`, `services/transcription/README.md`
- **Test first**: `services/transcription/tests/test_legacy_index_cleanup.py` — cases (all model-free, network-free, real tmp filesystem; build `Config` the way `tests/test_api_search.py`/`test_jobs_index.py` do, with `provider="fake"`):
  1. `remove_legacy_app_dir_index` deletes `data/index.sqlite3`, `-wal` and `-shm` when `index_db_path` points at `<vault>/.transcriber/index.sqlite3`, returns `True`, and a pre-existing vault index file keeps its exact bytes (FR-1 c1, c2).
  2. It returns `False` and leaves `data/index.sqlite3` in place when `index_db_path` is that same app-dir file (FR-2 c1) — parametrize over the vault-less default and an explicit `index_db_path` naming it.
  3. It returns `False` with no exception when nothing exists in `data/` (FR-2 c2).
  4. It logs a warning and returns `False` when the unlink raises `OSError` (simulate by making `data/index.sqlite3` a non-empty directory, which `unlink` cannot remove) — the call must not raise (FR-2 c3).
  5. Entering `TestClient(create_app(config))` with a vault-rooted config removes a pre-seeded `data/index.sqlite3` and emits exactly one log record with `extra["event"] == "legacy_index_removed"` on the `transcription` logger (use `caplog`); with no legacy file, no such record is emitted (FR-1 c3, c4; FR-2 c2).
- **Implement**: Add the helper to `search/index_db.py` as described under Architecture (reuse the `("", "-wal", "-shm")` suffix triple; guard with `Path.resolve()` equality; catch `OSError` → warning). Call it first thing inside `lifespan` in `app.py`'s `create_app`, passing `config.app_dir` and `config.index_db_path`, and log `legacy_index_removed` on `True`. Extend the `index_db_path` row in `services/transcription/README.md` with one clause: a pre-0.18 `<app_dir>/data/index.sqlite3` is deleted at service startup once a vault root is set.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: the new test file passes via `uv run --directory services/transcription pytest tests/test_legacy_index_cleanup.py -q`; `make format`, `make lint`, `make type`, `make test` all green; manual check — with `%APPDATA%\com.transcriber.desktop\config.json` carrying a `meetings_root`, drop a dummy `data\index.sqlite3` into that app dir, start the service (`uv run --directory services/transcription transcription-service serve`, or `npm run tauri dev` from `apps/desktop`), and confirm the dummy file is gone, stderr shows one `legacy_index_removed` JSON line, and stdout still carries exactly the single `listening` line.

## QA expectations

No aggregate `make qa`; the gate runs `make format`, `make lint`, `make type`, `make test`
(all four exist, see `Makefile`). `make test` fans out to cargo test, vitest, pytest for the
service and pytest for `scripts/tests`; the service suite is model-free and finishes in
under 30 s. PowerShell 5.1 has no `&&` — run targets one at a time. Nothing known-flaky in
the service suite; `cargo clippy -D warnings` under `make lint` is strict but untouched by
this feature (no Rust changes).

## Assumptions & decisions

- 2026-09-09 — (AUTO: codebase) Is the feature already implemented? → Yes: `config.py:421-428`, `sidecar.rs:113-119`, `jobs.py:388`, `mcp_server.py:93`, `README.md:92`, tests `test_config.py:117-150`; shipped in 0.18.0 (`1f8407c`). Blueprint scoped to the single residual (orphaned app-dir index after upgrade).
- 2026-09-09 — (ASSUMPTION) Migrate the old app-dir index into the vault, or discard it? → **Discard.** The "Enable vector search" download shipped in the same 0.18.0 commit as the move, so every pre-0.18 index is text-only; the app's startup catch-up pass (`App.tsx:196-205`) rebuilds it into the vault in seconds, `IndexDb._migrate_or_recreate` would likely discard a schema-drifted copy anyway, and a move would add cross-drive/locked-WAL edge cases for no retained data. On the operator's own machine the vault index has existed since 0.18.0, so a move would be a no-op there regardless.
- 2026-09-09 — (AUTO: codebase) Where does the cleanup run? → In `create_app`'s `lifespan` startup only (`serve` path). The standalone `transcriber-mcp` opens the index read-only and never writes or deletes; it is left untouched.
- 2026-09-09 — (AUTO: codebase) Should `docs/config-contract.md` change? → No: it documents the app schema and the env contract, neither of which changes; `index_db_path` is a service-only key documented in `services/transcription/README.md`, which T1 updates.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test style → observable filesystem state and emitted log records, real tmp files, no mocks of in-process collaborators; the `OSError` branch is provoked with a real directory-in-place-of-file rather than a patched `unlink`.
- 2026-09-09 — (AUTO: strict-skills) `frontend-toolkit:internal-ui` → listed as a strict development skill but not installed and out of domain (no UI task); T1 carries only the pytest skills.
