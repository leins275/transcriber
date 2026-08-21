# Transcriber desktop app

Tauri 2 (Rust) + React/TypeScript/Vite frontend. See
`../docs/setup.md` for host prerequisites and the clean-checkout sequence,
and `../docs/config-contract.md` for the settings file F4 must also write to.

## Running it

```
npm install
npm run tauri dev
```

## QA commands (FR-19)

There is **no `Makefile` and no `make`** on this host, and a root `Makefile`
wrapping these into `make format`/`make lint`/`make type`/`make test` is
**F4's** deliverable, not created here. FR-19's four names exist as the
commands below.

| FR-19 name | Rust (run from the repo root)                           | Frontend (run from `apps/desktop/`)                           |
| ---------- | ------------------------------------------------------- | ------------------------------------------------------------- |
| `format`   | `cargo fmt` (`cargo fmt --check` for CI/verification)   | `npm run format` (`npm run format:check` for CI/verification) |
| `lint`     | `cargo clippy --workspace --all-targets -- -D warnings` | `npm run lint`                                                |
| `type`     | — (the compiler is the type check)                      | `npm run type` (`tsc --noEmit`)                               |
| `test`     | `cargo test --workspace`                                | `npm run test` (`vitest run`)                                 |

All eight commands (the four Rust ones from the repo root, the four npm ones
from `apps/desktop/`) were run against this checkout as part of this task
and passed as written:

- `cargo fmt --check` — clean, no output.
- `cargo clippy --workspace --all-targets -- -D warnings` — clean.
- `cargo test --workspace` — all suites passed (vault, transcriber-desktop
  unit tests, and both crates' doc-tests).
- `npm run lint`, `npm run type`, `npm run test` — all clean/passing.
- `npm run format:check` — passes, including after `npm run tauri dev`/
  `npm run tauri build` has generated `src-tauri/gen/` (Tauri-generated
  capability/schema JSON): that directory is listed in `.prettierignore`,
  so it is never scanned regardless of build state.

## Scope note

`cargo test -p transcriber-desktop <module>::` is what individual T-series
tasks in `specs/tauri-desktop-app/plan.md` reference for their own module's
tests; `cargo test --workspace` (above) is the full gate and also covers
`crates/vault` (F1).
