# Setup — Transcriber desktop app

This is the setup guide for `apps/desktop` (Tauri 2 + React) and the Rust
workspace it lives in. It states what this feature's implementation actually
verified on the operator's machine — not a generic Tauri guide.

## Host prerequisites

| Prerequisite | State on this host | Notes |
|---|---|---|
| MSVC toolchain | present — `C:\Program Files\Microsoft Visual Studio\18\Community` | The spec's Problem & context section records this as `...\2022`; the directory actually present on this machine is `18\Community` (a newer Visual Studio release that uses `18` as its top-level version folder, not `2022`). Either MSVC edition works for the `x86_64-pc-windows-msvc` target; what matters is that the "Desktop development with C++" workload (link.exe, Windows SDK libs) is installed under it. |
| Windows SDK | present — `10.0.26100.0` (`C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0`) | |
| WebView2 runtime | present — `151.0.4129.93` (`C:\Program Files (x86)\Microsoft\EdgeWebView\Application\151.0.4129.93`) | Tauri 2 on Windows renders through the installed WebView2 runtime; no bundled Chromium. |
| Node / npm | present — Node v22.17.1, npm 11.5.1 | `npm` is the package manager for this feature (not pnpm/bun, per FR-1); `bun` 1.3.14 is also present on this host but unused. |
| `uv` | present — `C:\Users\feitr\.local\bin\uv.exe` (`uv 0.8.17`) | Used to run F2 (`services/transcription`) as the dev-mode sidecar; see `docs/config-contract.md` for the exact command. |
| **rustup / cargo** | **installed** (`~/.cargo/bin`, `stable-x86_64-pc-windows-msvc`) as of this task | The spec recorded rustup as **absent** at spec time — "must be installed before any work starts". It has since been installed (verified: `rustup show` reports `stable-x86_64-pc-windows-msvc` as the active, default toolchain with that target). A clean checkout on a machine without it must still install it first; see below. |

### Installing rustup (only if `cargo`/`rustup` are not already on `PATH`)

1. Install the MSVC "Desktop development with C++" workload (Visual Studio
   Installer — Community edition is sufficient) if not already present.
2. Install `rustup` from https://rustup.rs (or the standalone
   `rustup-init.exe`) and select the **`stable-x86_64-pc-windows-msvc`**
   toolchain — this is the default on Windows and is what this workspace
   builds against; no other toolchain or target is required.
3. Confirm with `rustup show`: it should report
   `stable-x86_64-pc-windows-msvc` as the active, default toolchain with
   target `x86_64-pc-windows-msvc` installed.
4. Confirm `cargo --version` and `rustc --version` resolve on `PATH` (a new
   shell may be needed after install so `~/.cargo/bin` is picked up).

## Clean-checkout sequence to a running window (FR-2)

From the repository root:

```
git clone <repo-url>
cd transcriber
cargo build -p transcriber-desktop
cd apps/desktop
npm install
npm run tauri dev
```

- `cargo build -p transcriber-desktop` is optional as a standalone step —
  `npm run tauri dev` triggers the same Rust build via
  `beforeDevCommand`/the Tauri CLI — but running it first surfaces any
  toolchain problem with a plain Rust error instead of interleaved
  Rust+Vite output, and it is a useful first-run sanity check.
- The **first** `npm run tauri dev` (or `cargo build`) is slow — Tauri's
  dependency tree is large — this is minutes, not seconds, and is expected;
  it is a one-time cost per machine/target directory, not a violation of
  NFR-3 (NFR-3's 3 s cold-start budget is about the **built** app's launch
  time, not a `cargo`/`vite` cold compile).
- `npm run tauri dev` opens the `Transcriber` window. With no
  `%APPDATA%\com.transcriber.desktop\config.json` present yet, the app shows
  the first-run folder-picker state (FR-18) rather than a drop zone.
- No F2 (Python transcription service) process needs to be running or
  installed for this to work: `npm run tauri dev` spawns it as a background
  sidecar (see `docs/config-contract.md`); if it never becomes ready the app
  still opens and shows the service as unavailable rather than failing to
  start (FR-13, NFR-3).

## QA commands

See `apps/desktop/README.md` for the full FR-19 command reference
(`format`/`lint`/`type`/`test`, Rust and frontend). In short, from the
repository root:

```
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

and from `apps/desktop/`:

```
npm run format:check
npm run lint
npm run type
npm run test
```

All eight commands above were run against this checkout as part of this
task and passed cleanly (see `apps/desktop/README.md` for the one caveat
around `npm run format:check` and the gitignored `src-tauri/gen/` directory
that a prior `tauri dev`/`tauri build` leaves behind).

## Known gaps

- There is **no root `Makefile`** and **no `make`** on this host
  (`make -n format` etc. all fail with `make: command not found`, per the
  spec). FR-19's four command names exist as the cargo/npm commands above.
  A root `Makefile` wrapping them into `make format`/`make lint`/`make
  type`/`make test` is **F4's** deliverable, not created here.
- `installer/`, `scripts/`, and packaging (`tauri.conf.json`'s bundle block
  beyond the identity fields fixed here) are **F4's** scope.
