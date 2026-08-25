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
  test. F2 reads *service-only* keys straight out of this same file through
  that mechanism — e.g. a top-level `"diarize": true` (plus the other
  `diarization_*` keys, see `services/transcription/README.md`) enables
  speaker diarization without the app's `Settings` schema knowing the key
  exists. The LLM feature's `llm_*` keys (`llm_model`, `llm_model_path`,
  `llm_ctx`, `llm_gpu_layers`, ... — same README) travel the same way; a
  future hardware "preset" is just a named bundle of these flat keys.
  (`llm_model` used to be written by the app's `select_llm_model` command;
  the model switcher is gone — the catalog is a single model,
  `services/transcription/src/transcription/llm_catalog.py` — and a stale
  `llm_model` naming a retired id migrates to the default on F2's config
  load.)
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

## Supersession note (F4 — windows-installer-build spec FR-11)

`specs/windows-installer-build/spec.md`'s **FR-11** originally says the vault
root is "persisted to a single JSON configuration file **in the application
folder**". That text is superseded, per the batch decision recorded in
`specs/windows-installer-build/plan.md`'s Architecture overview, by what this
document already describes above and what the merged code actually
implements: the one config file lives at
`%APPDATA%\com.transcriber.desktop\config.json` — **not** anywhere under the
per-user application folder (`%LOCALAPPDATA%\Programs\Transcriber\`, Q4-A).
F4 does not introduce a second, application-folder-local config file.

What replaces FR-11's original mechanism:

- The Rust app resolves the application folder itself (`app_paths.rs`,
  `app_dir()`) and passes it to the sidecar explicitly at spawn time via the
  `TRANSCRIBER_APP_DIR` env var documented above, plus
  `TRANSCRIBER_MODEL_PATH` derived from `app_paths::model_dir()` — the
  service never guesses or independently locates the application folder.
  `app_paths::model_dir()` also never resolves outside
  `<app folder>\models\`, even given a crafted `model.path` value from
  `config.json` (path-traversal hardening, since every config value is
  untrusted input to a native-command surface).
- The env var handshake this file already documents
  (`TRANSCRIBER_CONFIG_PATH`, `TRANSCRIBER_APP_DIR`, `TRANSCRIBER_ALLOWED_ROOTS`,
  `TRANSCRIBER_MODEL_PATH`, `TRANSCRIBER_MODEL`) is exactly F2's accepted
  `TRANSCRIBER_*` override contract — this feature adds no new env vars of
  its own for the production/installed sidecar.
- The service's fallback, used only when `TRANSCRIBER_APP_DIR` is absent
  (a developer running the service standalone, outside the app), is the
  directory of the running executable — the same rule `app_paths::app_dir()`
  applies on the Rust side.
- **The installer's silent mode** (`installer/installer_hooks.nsh`,
  `/VAULT=<path>`, see `installer/README.md`) writes directly into this same
  `%APPDATA%\com.transcriber.desktop\config.json` file — a full overwrite
  (schema v1, `meetings_root` set, `service`/`model` left `null`) matching
  the schema above exactly, so a silent install lands in precisely the state
  the in-app first-run wizard would produce. A plain reinstall/upgrade
  *without* `/VAULT=` never touches `config.json` at all, because the file
  lives outside `$INSTDIR` and nothing in the installer's upgrade path
  reaches it — this is what makes the vault root and the downloaded model
  survive a version bump unconditionally (FR-16).
- **Installed runtime layout deviation to note here too:** the baked Python
  runtime and service tree land at `<install dir>\pyenv\python\`,
  `\pyenv\site-packages\`, `\pyenv\service\` — under `pyenv\` directly at the
  install root, not under a `resources\pyenv\` subfolder, despite the source
  tree being `apps/desktop/src-tauri/resources/pyenv/` before bundling (see
  `docs/setup.md`'s "Known gaps"). `TRANSCRIBER_APP_DIR` and
  `TRANSCRIBER_MODEL_PATH` are unaffected by this — both are resolved
  relative to the application folder itself, not to `resources\`.

## What F4 must do with this

*(Status: done, verified against `installer/installer_hooks.nsh` and
`apps/desktop/src-tauri/src/sidecar.rs` as of this task — see the
supersession note above for the specifics of how each bullet below was
actually satisfied.)*

- Write the initial `config.json` at
  `%APPDATA%\com.transcriber.desktop\config.json` (creating the directory),
  setting at least `meetings_root` and, if pointing at an externally-managed
  service, `service.base_url`. **Two paths satisfy this, not one:** the
  installer itself writes it at install time only when `/VAULT=` is
  supplied (silent or interactive — `installer_hooks.nsh` does not gate the
  write on `IfSilent`); an interactive install with no `/VAULT=` leaves
  `config.json` absent, and F3's already-built first-run folder-picker
  writes it the moment the user picks a vault folder (`docs/setup.md`'s dev
  inner loop describes this: "With no `config.json` present yet, the app
  shows the first-run folder-picker state"). Both paths produce the same
  schema.
- Any additional keys F4 needs to persist (e.g. an installer-only bookkeeping
  field) can be added at the top level or under `service`/`model` — the
  app's own reads/writes will never delete them.
- If F4 changes the sidecar program from the `uv run ...` dev command shown
  above to a baked-environment launcher, that change belongs entirely inside
  `SidecarSpawnConfig`'s construction, per the plan's stated design.
