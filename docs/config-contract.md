# Config contract (FR-17) — for F4 to consume

This reproduces the settings contract from `specs/tauri-desktop-app/plan.md`
("Settings contract" section), **verified against the actual code** in
`apps/desktop/src-tauri/src/config.rs` and `apps/desktop/src-tauri/src/sidecar.rs`
as of T14. Where the plan's prose and the merged code disagree, this document
follows the code — the code is the contract F4 must interoperate with.

## File location

```
%APPDATA%\com.transcriber.desktop\config.json
```

This is Tauri's `app_config_dir()` for this app, i.e. `%APPDATA%\<bundle
identifier>`. The app never reads or writes any other location for settings.

**Encoding: UTF-8, BOM optional.** The app reads the file as UTF-8 and
strips a single leading byte-order mark (`EF BB BF`) before parsing, so an
installer that writes the file with PowerShell's `Set-Content -Encoding
UTF8` (PowerShell 5.1 default), `Out-File`, .NET's
`File.WriteAllText(path, s, Encoding.UTF8)`, or an NSIS/WiX helper that
BOM-prefixes UTF-8 by default all load correctly. Any other encoding
(UTF-16, ANSI/code-page) is not supported and will be reported as a
malformed file.

## Fixed identity (NFR-8 — changing any of these breaks installed settings)

- bundle identifier: `com.transcriber.desktop`
- productName / window title: `Transcriber`
- app-config directory name: `com.transcriber.desktop` (derived from the
  identifier above by Tauri, not set independently)

## Schema v1

```json
{
  "schema_version": 1,
  "meetings_root": "D:\\Meetings",
  "service": { "base_url": null },
  "model": { "id": "large-v3", "path": "C:\\Users\\<user>\\AppData\\Local\\Programs\\Transcriber\\models" }
}
```

Field semantics, matching `config.rs`'s `Settings`/`ServiceSettings`/`ModelSettings`:

- `schema_version` (`u32`, default `1`) — present for forward compatibility;
  the app does not currently branch on its value.
- `meetings_root` (`string | null`, default `null`) — absolute path to the
  vault root. `null`/absent means first-run (FR-18): the app shows the
  folder-picker state and refuses drops until this is set. A value pointing
  at a folder that no longer exists is tolerated — the app reports
  `meetings_root_exists: false` rather than crashing.
- `service.base_url` (`string | null`, default `null`) — `null` means
  "app-managed sidecar" (the app spawns F2 itself). A non-empty string, e.g.
  `"http://127.0.0.1:8756"`, means "connect to this URL, do not spawn a
  sidecar" — this is the "expect it running" development/ops mode.
- `model.id` (`string | null`, default `null`) and `model.path`
  (`string | null`, default `null`) — passed through to the sidecar's
  environment (see below). The app itself never loads a model.
- **Unknown keys are preserved.** Every level (`Settings`, `ServiceSettings`,
  `ModelSettings`) carries `#[serde(flatten)] extra: serde_json::Map<...>`,
  so any additional top-level key, or additional key nested under `service`
  or `model`, that F4's installer (or a future version) writes survives an
  app-side load → modify → save round-trip byte-for-byte in value. This is
  covered by `config.rs`'s `unknown_keys_survive_a_load_modify_save_round_trip`
  test.
- **Missing known keys fall back to their defaults** on load; a malformed
  JSON file returns a typed `config`-kind error naming the file, never a
  panic.
- **Writes are atomic**: a temp file (`config.json.tmp`) is written in the
  same directory and then renamed over `config.json`; the directory is
  created if absent (`config.rs::save`).

## Sidecar environment handshake (verified against `sidecar.rs` and F2's real
config module, `services/transcription/`)

**Correction to the plan's prose:** `specs/tauri-desktop-app/plan.md`'s
"Sidecar lifecycle" section names the env vars as `TRANSCRIBER_CONFIG`,
`TRANSCRIBER_APP_DIR`, `TRANSCRIBER_ALLOWED_ROOTS`, `TRANSCRIBER_MODEL_PATH`,
`TRANSCRIBER_MODEL_ID`. The names **actually implemented and read by F2**
(and used verbatim by `sidecar.rs::SidecarSpawnConfig::dev`) are:

| Env var | Set from | Notes |
|---|---|---|
| `TRANSCRIBER_CONFIG_PATH` | the app-config `config.json` path | **Not** `TRANSCRIBER_CONFIG` as the plan's prose states. F2's `config.py::load_config` only reads `env.get("TRANSCRIBER_CONFIG_PATH")` when no `config_path` is passed explicitly. |
| `TRANSCRIBER_APP_DIR` | the app's config directory (`app_config_dir()`) | Matches the plan. |
| `TRANSCRIBER_ALLOWED_ROOTS` | `settings.meetings_root` (only set when a root is configured) | Single path today; F2's own field accepts an `os.pathsep`-joined list, but this app only ever supplies one root. |
| `TRANSCRIBER_MODEL_PATH` | `settings.model.path` (only set when present) | Matches the plan. |
| `TRANSCRIBER_MODEL` | `settings.model.id` (only set when present) | **Not** `TRANSCRIBER_MODEL_ID` as the plan's prose states — F2's config dataclass field is named `model`, not `model_id`, so the env var mirrors that name. |

These five are the complete set this app currently sets; F2 layers
`defaults < config file < TRANSCRIBER_* env < CLI overrides`.

## Dev-mode sidecar command (verified against `sidecar.rs`)

```
uv run --directory services/transcription transcription-service serve --port 0
```

`SidecarSpawnConfig` (`apps/desktop/src-tauri/src/sidecar.rs`) is the single
place this program, its argument vector, and its environment are decided.
F4 repoints the sidecar at the baked/installed environment by changing only
that struct's construction — no other module needs to change.

The child's stdout is read for exactly one ready line:

```json
{"event":"listening","port":51234,"token":"...","pid":9001}
```

from which the app derives `http://127.0.0.1:<port>` and the bearer token
used for `Authorization: Bearer <token>` on every request to F2. A
`service.base_url` set in `config.json` bypasses all of the above: no
sidecar is spawned, and that URL is used directly (with no token, since none
was issued by a sidecar this app did not start).

## What F4 must do with this

- Write the initial `config.json` at
  `%APPDATA%\com.transcriber.desktop\config.json` (creating the directory)
  at install time, setting at least `meetings_root` and, if pointing at an
  externally-managed service, `service.base_url`.
- Any additional keys F4 needs to persist (e.g. an installer-only bookkeeping
  field) can be added at the top level or under `service`/`model` — the
  app's own reads/writes will never delete them.
- If F4 changes the sidecar program from the `uv run ...` dev command shown
  above to a baked-environment launcher, that change belongs entirely inside
  `SidecarSpawnConfig`'s construction, per the plan's stated design.
