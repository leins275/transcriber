---
slug: windows-installer-build
created: 2026-08-21
status: approved
---

# Spec: Windows installer and build system

## Summary

Build the delivery layer for the whole MVP: a repeatable build that turns this monorepo into a single Windows installer, and the installer itself. The installer puts the Tauri 2 desktop app (F3) and the Python transcription service (F2) into one self-contained application folder, gets the whisper large-v3 weights onto disk, and captures the user's meetings vault root into a config file that both the app and the service read. The operator is the only user; the target is a lean, pragmatic MVP that works on their Windows 11 machine, not a distributable commercial product.

## Problem & context

The repository is greenfield. Today it contains only `D:\Local\Git\transcriber\IDEA.md`, the `specs/` tree, and a gitignored read-only clone of vexa at `D:\Local\Git\transcriber\vexa\`. There is no `Cargo.toml`, no `package.json`, no `pyproject.toml`, no `Makefile` and no CI configuration — every artifact this spec describes has to be created.

Three facts about the operator's machine, probed directly, shape this feature:

- **No Rust toolchain**: `cargo`, `rustc` and `rustup` are all absent from PATH. Tauri (F3) cannot be built until this is fixed.
- **No `make`**: neither `make` nor `mingw32-make` is on PATH. The SDD pipeline probes `make -n format|lint|type|test`, so the build system must supply both the `Makefile` and a way to get GNU Make installed.
- **No real Python; `uv` is present**: `python --version` prints an empty version — the Microsoft Store stub. `uv 0.8.17` is installed at `C:\Users\<user>\.local\bin\uv.exe`. Available and usable: `node v22.17.1`, `npm 11.5.1`, `git 2.47.0`, `curl 8.10.1`. Absent: `pnpm`, `aria2c`, `makensis`.

That last point generalizes to the end user: the installer cannot assume a Python interpreter exists on the target machine. The Python runtime is part of the payload, not a prerequisite.

The two-folder split comes from `IDEA.md`: "1 папка приложения, где хранятся скрипты, модели, все что нужно для работы но о чем пользователю знать не нужно" — an application folder holding scripts, models and internals, plus the user-visible meetings vault (F1). The installer is where that split is first materialized, and where the vault path is first captured.

The model payload dominates every sizing decision. The reference implementation in the vexa clone loads its weights through faster-whisper/CTranslate2 and selects them by name (`core/meetings/services/transcription/src/transcription/main.py:32` reads `MODEL_SIZE` with a `large-v3-turbo` default, and the comment at line 39 records "large-v3-turbo + INT8 = ~2.1 GB VRAM (validated)"). Weights of roughly 1.6–3.1 GB are downloaded at runtime from Hugging Face rather than baked into the image. Vexa's own build system is Docker Compose end to end and offers nothing reusable for a Windows installer — only this model-acquisition pattern carries over.

## Users

- **Operator (installing user)** — runs the setup `.exe` on their own Windows 11 machine, picks a meetings vault folder, waits for the model, and starts using the app. Wants zero manual toolchain setup and no command line.
- **Operator (developer)** — clones the repo on a fresh machine, runs one bootstrap command and one build command, and gets a signed-or-not `.exe` out. Also runs `make format|lint|type|test` on every SDD task, across three languages.

## Profiles

Detection probes were run against the repository as it stands. **No profile matches today** — there is no `src-tauri/tauri.conf.json`, no `Cargo.toml`, no `package.json` and no `pyproject.toml` anywhere outside the gitignored vexa clone. The profiles below are recorded as *anticipated*, on the strength of sibling specs in the same batch, and must be re-detected once F2 and F3 land code.

- `desktop` — anticipated. Will match on `apps/desktop/src-tauri/tauri.conf.json` and its `bundle` block once F3 exists. This feature *is* that profile's Packaging layer, so its packaging and cross-platform rules govern here. No file proves it yet.
- `web` — anticipated. Will match on `apps/desktop/package.json` naming `react` and `vite` once F3 exists. Relevant to this feature only if the first-run setup wizard is built as app UI (see Open questions).
- `cli` — anticipated. Will match on the transcription service's `pyproject.toml` once F2 exists; its Release row is the same packaging concern as `desktop`'s. No file proves it yet.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Repository | Greenfield, git, `master` branch | `D:\Local\Git\transcriber\.git`, working tree holds only `IDEA.md`, `.gitignore`, `specs/`, `vexa/` |
| Desktop app | Tauri 2 + Rust + React (planned, F3) | No code yet; `cargo`, `rustc`, `rustup` all absent from PATH |
| Service | Python + uv (planned, F2) | No code yet; `uv 0.8.17` present, system `python` is the Store stub with no version |
| Node toolchain | Node 22.17.1, npm 11.5.1 | `node --version`, `npm --version` |
| Build orchestration | none | No `Makefile`, no `make`/`mingw32-make` on PATH, no `.github/workflows` |
| Installer tooling | none | `makensis` absent; Tauri's bundler vendors its own NSIS, so this is not blocking |
| Reference only | vexa (Docker Compose monorepo) | `D:\Local\Git\transcriber\vexa\Makefile`, `D:\Local\Git\transcriber\vexa\deploy\transcription\docker-compose.yml` — gitignored, read-only, not a build dependency |

Makefile QA targets present: **none** — there is no Makefile, and `make` itself is not installed. Creating both is FR-2 and FR-3 of this feature.

## Functional requirements

### Build system

- **FR-1** (must): The repository is laid out as a three-payload monorepo with a documented root structure — `apps/desktop/` (F3), `services/transcription/` (F2), `installer/` (NSIS hooks and resources), `scripts/` (build and bootstrap), plus root `Makefile`. Every path this spec references resolves under that layout.
- **FR-2** (must): A root `Makefile` exposes `format`, `lint`, `type`, `test`, each fanning out across all three languages (Rust via `cargo fmt`/`clippy`/`test`, TypeScript via the app's npm scripts, Python via `uv run` with the linter and type checker chosen by F2). Each target fails the build on the first failing sub-target and works from a clean clone after bootstrap.
- **FR-3** (must): A single bootstrap command prepares a developer machine: it detects and reports every missing prerequisite — Rust MSVC toolchain, Node, `uv`, GNU Make, and the Tauri CLI — and either installs it or prints the exact command to install it. Running it on the operator's current machine must surface the three known gaps (Rust, GNU Make, and the fact that system `python` is a non-functional Store stub) rather than failing with a confusing error.
- **FR-4** (must): Dependency versions are pinned and committed — `Cargo.lock`, `package-lock.json`, `uv.lock` — and release builds install from the locks in frozen mode, never resolving fresh.
- **FR-5** (must): A single source of truth for the product version propagates to the Tauri config, the installer artifact filename, the app's about/version surface, and the service. Bumping it in one place is sufficient.
- **FR-6** (must): One command from a clean clone produces the release installer at a deterministic output path, with no interactive prompts. The command is exposed as a `Makefile` target.
- **FR-15** (should): The build emits, alongside the installer, a SHA-256 checksum file and a build manifest recording the product version, git commit, and the resolved versions of the three payloads.

### Installer

- **FR-7** (must): The build produces a single self-contained Windows x64 installer executable, named with the product version, that requires no other downloaded file to install the application itself.
- **FR-8** (must): The installer creates the application folder containing: the Tauri app executable and its webview assets, a bundled Python runtime and the transcription service's dependency environment, the service code, and empty `models/`, `logs/` and `data/` subdirectories. The application folder is writable by the installing user without elevation, because the model download and the service's SQLite database write into it.
- **FR-9** (must): The installer handles Windows runtime prerequisites — the WebView2 runtime (detect, and install if missing) and the MSVC C runtime (bundled, statically linked, or installed) — so that the app launches on a clean Windows 11 machine with no manual prerequisite install.
- **FR-10** (must): The user chooses the meetings vault root during setup, with a folder-picker dialog and a sane default. The chosen path is validated (exists or can be created, is writable, is not inside the application folder) and rejected with a clear message if it is not.
- **FR-11** (must): The vault root is persisted to a single JSON configuration file in the application folder, with a documented, versioned schema. Both the desktop app (F3) and the transcription service (F2) resolve their configuration from that one file; the application folder is located by an environment variable the app sets when it spawns the service, falling back to the directory of the running executable. This file is the cross-feature contract and its schema is owned by this spec.
- **FR-12** (must): whisper large-v3 weights are acquired into the application folder's `models/` directory, with a visible progress indicator showing bytes and percentage, resume across interruption, integrity verification of the completed download, and working cancel and retry. A download that is killed part-way and restarted resumes rather than starting over.
- **FR-13** (must): Setup creates a Start Menu shortcut and optionally a desktop shortcut, and offers to launch the app on completion.
- **FR-14** (must): The uninstaller removes the application payload and shortcuts. It **never** touches the meetings vault, under any code path. It handles the multi-gigabyte model directory and the configuration file explicitly — either preserving them or removing them on an explicit opt-in, but never silently orphaning gigabytes on disk with no way to find them.
- **FR-16** (should): Installing a newer version over an existing installation preserves the configuration file, the chosen vault root, and the already-downloaded model — no re-download, no re-picking the folder.
- **FR-17** (should): The product is installable and launchable with no network and no model present. The app detects the missing model, states so plainly, and offers a retry — a failed or skipped download never leaves a broken install.
- **FR-18** (should): The installer supports a silent/unattended mode with the vault root and install directory supplied as arguments, so the operator can reinstall repeatedly during development without clicking through the UI.
- **FR-19** (could): A CI workflow builds the installer on a Windows runner for tagged commits and attaches the artifact and its checksum to a release.

## Non-functional requirements

- **NFR-1**: The installer artifact is **≤ 1.5 GB** excluding model weights. Revised at the spec gate: the operator chose GPU-first inference, so the pre-baked uv environment ships the CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) that CTranslate2 needs — roughly 700 MB–1 GB compressed. PyTorch remains excluded (faster-whisper/CTranslate2 does not need it); a backend that pulls PyTorch still breaks the budget.
- **NFR-2**: Installation completes in **under 2 minutes** on the operator's machine, excluding the model download.
- **NFR-3**: The model download sustains resume: after a hard interruption at any point, a restarted download re-transfers at most one chunk of already-fetched data, and the completed file is verified against a published digest before being marked usable.
- **NFR-4**: Installation requires **no administrator elevation** and produces **no UAC prompt** for the default install scope.
- **NFR-5**: A release build from a clean clone on a bootstrapped machine completes in **under 20 minutes** and is non-interactive.
- **NFR-6**: `make format`, `make lint`, `make type`, `make test` each run to completion from a clean clone on the operator's machine, exiting `0` on success and non-zero on any sub-target failure.
- **NFR-7**: Cross-platform is out of scope for the MVP (Windows only), but the build scripts must not hard-code Windows-only assumptions into the *shared* configuration schema or the repo layout — only into the installer and bootstrap layers. macOS/Linux bundling is expected to be a later addition, not a rewrite.

## Acceptance criteria

- **FR-1 / FR-6**:
  - [ ] A clean clone plus bootstrap plus one documented build command yields the installer at the documented output path.
  - [ ] The build command is non-interactive and exits non-zero if any payload fails to build.
- **FR-2 / NFR-6**:
  - [ ] `make -n format`, `make -n lint`, `make -n type`, `make -n test` all resolve (the SDD detection probe passes).
  - [ ] Each target visibly executes the Rust, TypeScript and Python sub-steps.
  - [ ] Introducing a lint error in any one of the three languages makes `make lint` exit non-zero.
- **FR-3**:
  - [ ] Run on the operator's current machine, bootstrap reports Rust and GNU Make as missing and does not silently fall back to the Store-stub `python`.
  - [ ] After bootstrap, `cargo --version` and `make --version` both succeed.
- **FR-4**:
  - [ ] `Cargo.lock`, `package-lock.json` and `uv.lock` are committed.
  - [ ] A release build with the network disabled after the first successful build still succeeds from cache, and fails loudly rather than silently resolving new versions if a lock is out of date.
- **FR-5**:
  - [ ] Changing the version in its single source of truth changes the installer filename, the Tauri bundle version and the app's reported version together.
- **FR-7 / FR-8**:
  - [ ] The produced `.exe` installs on a Windows 11 machine that has never had this app, Python, or Rust on it.
  - [ ] After install, the application folder contains the app executable, the Python runtime, the service code and its environment, and empty `models/`, `logs/`, `data/`.
  - [ ] A non-elevated process running as the installing user can create a file inside the application folder and inside `models/`.
- **FR-9**:
  - [ ] On a machine with WebView2 absent, the installer installs it and the app still launches.
  - [ ] The app launches with no "VCRUNTIME140.dll was not found" or equivalent missing-DLL dialog.
- **FR-10 / FR-11**:
  - [ ] Setup presents a folder picker for the vault root with a default path pre-filled.
  - [ ] Choosing a path inside the application folder is rejected with an explanatory message.
  - [ ] Choosing a non-writable path is rejected with an explanatory message.
  - [ ] After setup, the configuration file exists, contains the chosen vault root and a schema version, and is valid JSON.
  - [ ] A script reading the config the way the service (F2) will read it resolves the same vault root the user picked.
  - [ ] The app (F3), started fresh, displays or otherwise acts on that same vault root.
- **FR-12 / NFR-3**:
  - [ ] The model download shows progress in bytes and percent and updates at least once a second.
  - [ ] Killing the download at roughly 50%, then restarting it, resumes near 50% rather than at 0%.
  - [ ] A corrupted or truncated file fails verification and is not marked as usable.
  - [ ] Cancel stops the transfer and leaves the install in a defined, retryable state.
  - [ ] On success, the weights are under the application folder's `models/` directory and the service loads them without any further download.
- **FR-13**:
  - [ ] A Start Menu entry exists after install and launches the app.
  - [ ] The launch-on-finish option starts the app.
- **FR-14**:
  - [ ] Uninstall removes the app executable, shortcuts and the Add/Remove Programs entry.
  - [ ] A vault folder populated with meeting subfolders is byte-for-byte unchanged after uninstall.
  - [ ] The model directory's fate is explicitly presented to the user, and whichever branch is chosen, the resulting on-disk state matches what was stated.
- **FR-16**:
  - [ ] Installing a bumped version over an existing install leaves the config file's vault root unchanged and does not re-download the model.
- **FR-17**:
  - [ ] With networking disabled, the installer completes and the app launches.
  - [ ] The app reports the model as missing in plain language and exposes a retry that succeeds once networking is restored.
- **FR-18**:
  - [ ] A silent install with vault root and install directory passed as arguments completes with no UI and produces the same on-disk result as the interactive path.
- **NFR-1**:
  - [ ] The produced installer file is ≤ 1.5 GB (excluding model weights), and its dependency tree contains no PyTorch.
  - [ ] After install, a local transcription job on the operator's machine runs on `cuda` (verified via the service's `/health` device report).
- **NFR-4**:
  - [ ] Installing as a standard (non-admin) user produces no UAC prompt and succeeds.

## Out of scope

- **macOS and Linux packaging.** Windows only for the MVP, per the operator decision and `IDEA.md` ("Кроссплатформа, но начать можно с винды").
- **Code signing and an EV certificate.** The installer will be unsigned and will trigger a SmartScreen warning that the operator clicks through. Signing is a distribution concern; this is a personal tool.
- **Auto-update.** No update server, no differential updates, no `tauri-plugin-updater`. Reinstalling over the top (FR-16) is the update mechanism.
- **An app store or winget/Chocolatey package.**
- **Model management UI** — browsing, comparing, or holding multiple whisper models. One model, one download.
- **CPU-only optimization.** MVP is **GPU-first** (operator decision at the spec gate): CUDA inference on the operator's RTX 4070 is the primary path, and the cuBLAS/cuDNN runtime wheels ship inside the baked uv environment. CPU fallback is best-effort (whatever faster-whisper's `device=auto` gives), not a tested or optimized target.
- **Docker or any container-based delivery.** Vexa's Compose-based build is a reference only, not a target.
- **Telemetry, crash reporting, and installer analytics.**
- **Multi-user / per-machine deployment scenarios**, roaming profiles, and Group Policy deployment.
- **Migrating an existing `Meetings` folder into vault layout.** Choosing the root is in scope; reorganizing its contents belongs to F1.

## Applicable toolkits

Under the strict rule — keep only rows whose Signal is observed in this repository — **no toolkit row currently qualifies**, because no payload code exists yet. One row is listed anyway, because this feature is precisely the layer that creates its signal:

- `devops-toolkit:devops-rollout-plan` — Packaging/Release layer. Selected by the `desktop` profile's "installer / bundle config" row and the `cli` profile's "published package or binary" row. The signal (a bundle configuration) does not exist yet; FR-7 creates it. **This plugin is not installed** — `C:\Users\<user>\.claude\plugins\cache\its-marketplace\` contains only `sdd` and `workflow-toolkit`. Downstream agents should degrade gracefully and the final report should surface the gap.

The `web` profile's UI rows do not apply to this feature as scoped: the installer's UI is native (NSIS dialogs), not React. If Q1 is resolved toward a first-run wizard built as app UI, that one task moves into `web`'s UI domain and picks up its mandatory skill.

**Mandatory skills**: none for this feature.

- `desktop` contributes none.
- `cli` contributes none.
- `web` would contribute `frontend-toolkit:internal-ui` — mandatory on internal-tool UI tasks — but only if Q1 places setup UI inside the React app. Flagged conditionally so the architect does not miss it.

## Open questions

*All resolved by the operator at the spec gate (2026-08-21): Q1 → A, Q2 → A, Q3 → **operator override: GPU-first, full `large-v3`, CPU optional**, Q4 → A. Retained below for the record.*

**Q1 — Where does the model download and vault selection happen?**

Extending Tauri's bundled NSIS installer with a genuinely custom page (a folder picker, a progress bar) is not a small change: Tauri 2 exposes only pre/post install and uninstall *hook macros*, so any real custom dialog means overriding the entire NSIS template and re-maintaining it against every Tauri release. That, plus the fragility of pulling gigabytes inside an installer transaction, biases the recommendation strongly.

- **A. First-run setup wizard in the app** *(recommended)* — the installer stays a stock Tauri NSIS bundle; on first launch the app asks for the vault folder and downloads the model with progress/resume/verify via the Python side's existing Hugging Face download path. Free resume, free checksums, cancel/retry is trivial, the user can change the folder later through the same UI, and the installer stays small and boring. Cost: the operator's literal reading of "установщик, который… сможет скачать whisper3" is satisfied by the product rather than by the setup executable itself.
- **B. Custom pages inside the installer** — a fully custom NSIS template (or a separate Inno Setup script wrapping the Tauri build output). Matches the request most literally, one single flow. Cost: maintaining a forked installer template, hand-rolled resume and verification logic in NSIS/Pascal, and a multi-gigabyte download that can fail an otherwise fine installation.
- **C. Ship the model inside the installer** — one ~2–4 GB `.exe`, fully offline, nothing to download ever. Cost: violates NFR-1 by an order of magnitude, every version bump re-ships the weights, and the build produces multi-gigabyte artifacts.
- **D. Split — vault picker in the installer, model download in the app** — the vault picker is a single folder page, cheap enough to justify a template override; the heavy download stays where resume works. Cost: still a forked NSIS template, for one dialog.

**Q2 — How is the Python runtime and the service delivered?**

The end user has no Python; the operator's own machine only has a Store stub. Something must ship.

- **A. Pre-baked uv environment in the bundle** *(recommended)* — the build runs `uv python install` plus a frozen `uv sync` into a relocatable directory inside `apps/desktop`'s bundle resources; the installer just copies it. Install is offline and fast, the environment is identical to what was tested, and `uv` stays the single tool for dev and release. Cost: bundle size is whatever the dependency tree weighs, and the environment must be verified as relocatable (absolute paths in the venv are the classic failure).
- **B. Bootstrap with `uv` at first run** — ship only the lockfile and a small `uv` binary; the first launch downloads CPython and the wheels. Smallest installer. Cost: first run needs network and takes minutes, and the failure surface moves onto the user's machine where you cannot debug it.
- **C. Freeze the service with PyInstaller** — one `service.exe`, no Python concept exposed at all. Cost: a second packaging toolchain to learn and maintain, notorious friction with ML native extensions, and antivirus false positives on frozen binaries.
- **D. Rewrite the service as a Rust sidecar** — no Python at all. Cost: contradicts F2's litellm/whisper design; not an MVP-scale move.

**Q3 — Which whisper large-v3 variant and backend is the default?**

This is the single biggest lever on installer size (NFR-1) and on whether CPU transcription is tolerable. The MVP has no GPU support, and large-v3 on CPU is slow enough to matter. Note that the vexa reference service already defaults to the turbo variant. Shared with F2, which owns the runtime API; answered here because the download size and dependency weight are this feature's constraint.

- **A. faster-whisper / CTranslate2, `large-v3-turbo`, int8** *(recommended for MVP)* — roughly 1.6 GB of weights, no PyTorch in the dependency tree, and several times faster than full large-v3 on CPU. Cost: a distilled/pruned decoder, so accuracy is somewhat below full large-v3, particularly on hard audio and less-common languages.
- **B. faster-whisper / CTranslate2, full `large-v3`** — roughly 3 GB of weights, still no PyTorch, full large-v3 accuracy. Cost: the download doubles and CPU transcription gets substantially slower.
- **C. openai-whisper reference implementation, `large-v3`** — the exact model the operator named, reference behavior. Cost: pulls PyTorch, adding roughly 2–2.5 GB of wheels to the *installer* and blowing NFR-1 apart; slowest CPU path of the three.
- **D. Ship turbo, make the model swappable in config** — default to A, let the config file name a different model that the app downloads on demand. Cost: a little extra config plumbing in F2 and this feature's schema; otherwise strictly a superset of A.

**Q4 — Install scope and where the application folder lives?**

- **A. Per-user install to `%LOCALAPPDATA%\Programs\Transcriber`** *(recommended)* — no UAC prompt (NFR-4), and the application folder is writable by the app, which is what makes downloading a multi-gigabyte model and writing the SQLite log in-place work without elevation. Matches `IDEA.md`'s single "папка приложения". Cost: installed for one user only; the folder is buried where a user would not browse to it.
- **B. Per-machine install to `Program Files`, data split to `%LOCALAPPDATA%`** — the conventional Windows layout; program files are protected from accidental modification. Cost: requires elevation, and it splits the "one application folder" concept into two, so the model and the service database no longer sit next to the binaries.
- **C. Per-machine install with a fully writable `Program Files` subfolder** — keeps one folder and installs for all users. Cost: loosening ACLs under `Program Files` is a genuine local privilege-escalation pattern; not worth it.
- **D. Let the user choose the install directory freely** — maximum control, e.g. onto a large data drive, which matters when the payload is gigabytes. Cost: more install-time surface to validate, and the writability guarantee now depends on wherever they pointed it.

## Decisions log

- 2026-08-21 — Batch scope limited to `## MVP`; `# Планы` items deferred → per operator notes at intake.
- 2026-08-21 — Split approved as proposed: 4 features (F1 → F2 → F3 → F4) → operator at split gate.
- 2026-08-21 — Which platforms does the MVP target? → Windows only. macOS/Linux packaging deferred.
- 2026-08-21 — Which Python tooling does the project standardize on? → `uv`, for both development and the shipped runtime environment.
- 2026-08-21 — Q1 setup flow → **A: first-run wizard in the app** — stock Tauri NSIS bundle; vault-folder pick and model download (progress/resume/verify) happen on first launch. (Operator, spec gate.)
- 2026-08-21 — Q2 Python delivery → **A: pre-baked relocatable uv environment** (uv-managed CPython + frozen `uv sync`) inside the bundle. (Operator, spec gate.)
- 2026-08-21 — Q3 model/backend → **operator override: GPU-first**. Default is faster-whisper **full `large-v3`** on CUDA (RTX 4070); the baked uv env ships `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` wheels; CPU support is optional best-effort fallback, not a tuned target. Model remains swappable via F2's config (model id/path). NFR-1 revised from 150 MB to 1.5 GB accordingly. (Operator, spec gate.)
- 2026-08-21 — Q4 install scope → **A: per-user `%LOCALAPPDATA%\Programs\Transcriber`**, no elevation, writable app folder. (Operator, spec gate.)
