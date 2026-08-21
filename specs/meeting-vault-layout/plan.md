---
slug: meeting-vault-layout
status: approved   # draft | approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Meeting vault layout and naming convention

All paths in this plan are **repo-relative** (this feature is implemented in its own git worktree, so the absolute prefix differs per worktree). Repo root in the main checkout is `D:\Local\Git\transcriber`.

## Architecture overview

One standalone Rust library crate, `crates/vault/`, with its own `Cargo.toml`. No workspace manifest — the root workspace arrives with F3 (batch decision). No `Makefile` — F4 owns repo-wide QA entry points.

The crate is a pure-core / thin-shell design: every rule of the naming convention is a pure function over `&str`, and exactly one module touches the filesystem's write path.

```
                          crates/vault/src/
  F3 (Tauri #[command])   ┌──────────────────────────────────────────────┐
        │                 │ lib.rs        pub mod decls + curated pub use │
        ▼                 │ error.rs      VaultError + Rejection (NFR-5)  │
   Vault::open(root) ─────┼─► layout.rs   init, project dir, collisions   │
   Vault::ingest(file) ───┼─► ingest.rs   orchestration, rollback, result │
        │                 │      ├─► parse.rs   classify_filename (pure)  │
        │                 │      │      ├─ code.rs   project code FR-4/15 │
        │                 │      │      ├─ date.rs   YYMMDD  FR-5 + clock │
        │                 │      │      ├─ title.rs  title rules FR-6     │
        │                 │      │      └─ media.rs  ext allowlist FR-7   │
        │                 │      ├─► paths.rs    containment FR-14, 260   │
        │                 │      │               char cap NFR-4, names    │
        │                 │      └─► transfer.rs copy/rename-verify-delete│
        ▼                 │ appdata.rs    %LOCALAPPDATA% concept (FR-16)  │
   Ingested { … } ────────┴──────────────────────────────────────────────┘
        │
        └─► meeting_dir path handed to F2 (Python) as an argv argument
```

**Data flow of one ingest** (this ordering is a requirement, not a preference — FR-14 demands the containment check run *before* any directory creation):

1. `stat` the dropped file (exists, is a regular file).
2. `parse::classify_filename(file_name)` — pure. Extension first: not in the media allowlist → `Err(VaultError::UnsupportedMediaType)`, nothing on disk (FR-7). Otherwise split the stem on the **first two** `" - "` occurrences and validate code / date / title. Any failure yields `Classified::Unsorted { reason }` — not an error (FR-10).
3. Build the destination components: `[PROJECT, "<date> - <Title>"]` or `["unsorted", "<YYMMDD today> - <stem>"]`.
4. `paths::contained_child(root, components)` + `paths::check_len(...)` — lexical rejection of `..`, absolute, drive-relative, `\\?\`, UNC and separator-bearing components, then `starts_with(canonical root)`; then the 260-char cap including the `\source.<ext>` leaf. **No filesystem mutation has happened yet.**
5. `layout::ensure_project_dir` (case-insensitive reuse, FR-9) then `layout::resolve_meeting_dir` (FR-11: identical size+mtime → duplicate re-drop no-op; otherwise ` (2)`, ` (3)`, …).
6. `fs::create_dir_all` the meeting dir, remembering which components this call created.
7. `transfer::transfer_into_place` — same volume: `rename` then verify size; cross volume: `copy`, verify size, then delete the original (FR-12, NFR-2).
8. On any failure after step 6: delete a partial `source.*` and remove exactly the directories this call created, leaving the original intact.
9. Return `Ingested { classification, meeting_dir, source_path, collision }` (FR-13).

**Public API surface** F3 links against (fixed here so F3's spec can rely on it):

```rust
pub struct Vault;                 // Vault::open(root) -> Result<Vault, VaultError>   (FR-1)
impl Vault {
    pub fn root(&self) -> &Path;
    pub fn ingest(&self, source: impl AsRef<Path>) -> Result<Ingested, VaultError>;
    pub fn ingest_on(&self, source: impl AsRef<Path>, today: NaiveDate) -> Result<Ingested, VaultError>;
}
pub struct Ingested { pub classification: Classification, pub meeting_dir: PathBuf,
                      pub source_path: PathBuf, pub collision: CollisionOutcome }
pub enum Classification { Sorted { project: String, date: String, title: String },
                          Unsorted { reason: Rejection } }
pub enum CollisionOutcome { Fresh, DuplicateRedrop, SuffixedFolder(u32) }
pub enum VaultError { /* aborts the ingest */ }   pub enum Rejection { /* routes to unsorted */ }
pub fn parse::classify_filename(file_name: &str) -> Result<Classified, VaultError>;   // pure (FR-2)
pub fn appdata::app_data_dir(app_name: &str) -> Result<PathBuf, VaultError>;          // FR-16
```

`ingest_on` is the clock seam: `ingest` calls it with `date::today_local()`. Without it, FR-10's "two unsorted files distinguishable by date added" is untestable.

**Dependencies** (fixed once, in T1 — no later task may add one): `chrono` (`default-features = false, features = ["clock", "std"]`) for calendar-correct date validation and local "today"; dev-dependency `tempfile` for real-directory tests. No `regex` — the two patterns are a dozen lines of `char` checks.

**Module ownership rule that makes the waves work**: T1 creates *every* source file as a compiling stub and declares them all `pub mod` in `lib.rs`. Wave-2/3 tasks then own exactly one file each and never touch `lib.rs` or `Cargo.toml`. T10 is the only later task that edits `lib.rs`, adding the curated `pub use` surface.

## Risks

- **R1 (blocking, T1): no MSVC toolchain on this machine.** Probed: `C:\Program Files (x86)\Windows Kits\10\Lib\10.0.26100.0\` exists (Windows SDK present), but no `VC\Tools\MSVC` under either `Program Files\Microsoft Visual Studio\2022\*` or `Program Files (x86)\Microsoft Visual Studio\2022\*`. `x86_64-pc-windows-msvc` cannot link without it, so T1 may have to install VS 2022 Build Tools (VCTools workload, multi-GB, likely a UAC prompt). If that install cannot be completed non-interactively, **park T1 and escalate** — do not silently fall back to `x86_64-pc-windows-gnu`, because F3 (Tauri 2) requires MSVC anyway and a split toolchain would break the batch.
- **R2 (T1): first build needs network.** `chrono` and `tempfile` are fetched from crates.io. Vendor or `cargo fetch` once in T1 so wave-2 agents build offline-fast.
- **R3 (spec defect, T2): the FR-5 acceptance bullet is factually wrong.** It asks that `260229` be accepted "(2026 leap-year check applied correctly)". 2026 is **not** a leap year — `26` maps to 2026, February has 28 days. A calendar-correct implementation must **reject** `260229`. T2 implements the real calendar and covers the leap-year path with `240229` (accept) / `250229` (reject) / `260228` (accept) / `260229` (reject). Flagged at the plan gate.
- **R4 (spec contradiction, T4): FR-4's prose vs. its acceptance bullet.** The prose says the raw code "must already match `^[A-Za-z][A-Za-z0-9]{1,9}$` and is then normalized to uppercase", which would accept `els`; the acceptance bullet, Q1's own option-A text ("a lowercase-typo code silently goes unsorted") and the Decisions log (`^[A-Z][A-Z0-9]{1,9}$`) all say lowercase is a rejection. Two of three signals plus the operator's recorded decision win: **uppercase-only pattern, `els` → unsorted**. Uppercase normalization is then defensive and a no-op.
- **R5 (spec tension, T6): NFR-2 mandates a rename; FR-12 mandates copy-verify-delete.** Resolved by volume: same volume → `fs::rename` (atomic, preserves mtime) followed by the same size verification, rolled back by renaming home if verification fails; different volume → literal copy, verify, delete. Both of FR-12's observable criteria hold on either path, and NFR-2's <500 ms for a large file holds on the common one.
- **R6 (interpretation, T5): FR-6 says "reject, never repair", FR-10 says every media file lands somewhere.** These collide for a badly named file whose stem contains `:` or `?`. Resolution: rejection-not-repair governs *sorted* titles; the **unsorted** fallback name is sanitized (illegal and control chars → `_`, trailing dots/spaces trimmed, empty → `recording`) because it must never fail. Documented in `paths.rs`.
- **R7 (FR-11 assumption, T6): dedupe by size+mtime depends on mtime surviving the transfer.** `rename` preserves it and `std::fs::copy` on Windows goes through `CopyFileEx`, which copies the last-write time. T6 asserts this in a test so the assumption fails loudly at build time rather than silently degrading a re-drop into a ` (2)` folder.
- **R8 (wave mechanics): parallel agents share one `target/` and one crate.** Cargo's build lock serializes them safely, but a sibling's half-written module breaks everyone's compile. Mitigations: T1's stubs compile; each task keeps its own file compiling after every edit and uses `cargo test --test <own file>` during the wave; a warning or failure originating outside your **Files** set is not yours to fix — T11 is the authoritative gate.
- **R9 (NFR-4): the 260-char budget shrinks with the vault root's depth.** The check must be on the full absolute destination including `\source.<ext>`, not the relative part, or a deep root turns a passing test into a runtime OS error.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1 |
| 2 | T2, T3, T4, T5, T6, T7 |
| 3 | T8, T9 |
| 4 | T10 |
| 5 | T11 |

## Tasks

### [x] T1: Rust toolchain, crate skeleton, error vocabulary  [deps: —]

- **Files**: `crates/vault/Cargo.toml`, `crates/vault/.gitignore`, `crates/vault/README.md`, `crates/vault/src/lib.rs`, `crates/vault/src/error.rs`, `crates/vault/src/date.rs`, `crates/vault/src/title.rs`, `crates/vault/src/code.rs`, `crates/vault/src/media.rs`, `crates/vault/src/paths.rs`, `crates/vault/src/transfer.rs`, `crates/vault/src/appdata.rs`, `crates/vault/src/parse.rs`, `crates/vault/src/layout.rs`, `crates/vault/src/ingest.rs`, `crates/vault/tests/error_vocabulary.rs`
- **Test first**: `crates/vault/tests/error_vocabulary.rs` — cases: (NFR-5) a `Rejection::ALL` const slice covers every variant and no two `Display` strings are equal; (NFR-5) each message is specific — the `DateNotACalendarDate` message mentions the calendar, not "invalid filename"; a `VaultError::ALL_KINDS` slice likewise has pairwise-distinct `Display` strings; both enums are `Debug + Clone + PartialEq` and `VaultError: std::error::Error`; `Rejection` is `Copy`-able or cheap to clone so F3 can move it across the IPC boundary.
- **Implement**: (a) Install the toolchain non-interactively: prefer `winget install --id Rustlang.Rustup -e --accept-source-agreements --accept-package-agreements`, else download `https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe` and run it with `-y --default-toolchain stable --default-host x86_64-pc-windows-msvc --profile default` (that profile brings `rustfmt` and `clippy`). PATH will not be refreshed in the current shell — verify through the full path `& "$env:USERPROFILE\.cargo\bin\cargo.exe" --version`, and likewise for `rustc`, `cargo fmt --version`, `cargo clippy --version`. (b) Check MSVC: run `& "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`; if empty, install Build Tools (`winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"`). If this cannot complete unattended, mark this task `[!]` and escalate per R1. (c) Create the crate by hand (not `cargo new`, to keep the file set exact): `Cargo.toml` with `name = "vault"`, `edition = "2021"`, the two dependencies from the architecture section and nothing else; `.gitignore` with `/target`; `lib.rs` carrying `#![forbid(unsafe_code)]`, `#![deny(missing_docs)]`, crate-level docs that state the naming convention and the on-disk layout, and one `pub mod` line per module file; every other `src/*.rs` a compiling stub with a module doc comment and no items (later tasks fill them). `error.rs` is fully implemented here: `VaultError` (`RootIsNotADirectory`, `SourceMissing`, `SourceNotAFile`, `UnsupportedMediaType { ext }`, `PathEscapesVault`, `PathTooLong { len, limit }`, `SuffixLimitExceeded`, `AppDataUnavailable`, `Io { path, source }`) and `Rejection` (`MissingSeparator`, `EmptyProjectCode`, `InvalidProjectCode`, `ReservedProjectCode`, `DateNotSixDigits`, `DateNotACalendarDate`, `EmptyTitle`, `IllegalTitleCharacter(char)`, `ReservedDeviceName`, `TitleEscapesVault`) — each with a distinct human-readable `Display`. (d) `README.md`: the three QA commands, the fact that F4 owns the Makefile, and a stub API section T11 fills in. (e) `cargo fetch` so wave-2 agents build offline.
- **Skills**: — (the spec's "Applicable toolkits" list is empty; no domain toolkit resolves for this crate)
- **Done when**: `cargo --version`, `cargo fmt --version`, `cargo clippy --version` all succeed in a fresh shell; from `crates/vault/`, `cargo build`, `cargo test`, `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` all pass on the stub crate; `git status` shows no `target/` noise. No `Makefile` anywhere (F4 owns it).

### [x] T2: Date component — YYMMDD validation and the local-date clock  [deps: T1]

- **Files**: `crates/vault/src/date.rs`, `crates/vault/tests/date.rs`
- **Test first**: `crates/vault/tests/date.rs` — cases: (FR-5) `260812` and `260724` accepted and returned **verbatim**; `260230` → `DateNotACalendarDate`; `991345` → `DateNotACalendarDate`; `260228` accepted; `260229` **rejected** — 2026 is not a leap year, see R3; `240229` accepted and `250229` rejected, which is the real leap-year coverage; `2026-08-12`, `26081`, `2608123`, `""` → `DateNotSixDigits`; (NFR-3) the Arabic-Indic digits `٢٦٠٨١٢` → `DateNotSixDigits` (ASCII-digits-only, never `char::is_numeric`); `000101` accepted (year 2000), `991231` accepted (year 2099); `260012` and `261300` → `DateNotACalendarDate`; `260800` → `DateNotACalendarDate`; `today_local()` returns a date whose `format_yymmdd` is exactly six ASCII digits and round-trips through the validator.
- **Implement**: `pub fn validate(raw: &str) -> Result<ValidDate, Rejection>` — exactly six `is_ascii_digit` chars, `YY` mapped to `2000 + YY`, then a real-calendar check via `chrono::NaiveDate::from_ymd_opt`. `ValidDate` keeps the verbatim six-char string (FR-5 forbids reformatting) plus the `NaiveDate`. Also `pub fn today_local() -> NaiveDate` (`chrono::Local::now().date_naive()`) and `pub fn format_yymmdd(d: NaiveDate) -> String` for FR-10's date-added prefix. No `std::fs` in this module, ever.
- **Skills**: —
- **Done when**: `cargo test --test date` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` pass; the module imports nothing from `std::fs`/`std::path`.

### [x] T3: Title rules — Windows-illegal characters, trimming, reserved device names  [deps: T1]

- **Files**: `crates/vault/src/title.rs`, `crates/vault/tests/title.rs`
- **Test first**: `crates/vault/tests/title.rs` — cases: (FR-6) `Security issue`, `Client demo`, `Security - issue - part 2` accepted verbatim; each of `< > : " / \ | ? *` in a title → `IllegalTitleCharacter(c)` with the offending char reported; `Q3: review` → `IllegalTitleCharacter(':')`; a `\u{0}`, `\u{1f}` or `\u{7f}` control char → `IllegalTitleCharacter`; `NUL`, `nul`, `CON`, `com1`, `COM9`, `LPT1`, `LPT9`, `AUX`, `PRN` → `ReservedDeviceName`; the stem rule also catches `NUL.backup`; `COM0`, `LPT0`, `CONSOLE`, `NULL` are **accepted** (not reserved); `""`, `"   "`, `"..."`, `" . "` → `EmptyTitle` after trimming; `Review ` → `Review` (trailing space stripped, FR-6 acceptance); `Review...` → `Review`; leading whitespace trimmed; (NFR-3) emoji, an RTL mark `\u{200f}`, and a 30 000-char title are all accepted here without panic (length is `paths.rs`'s concern, T5); a title that is trimmed to something *different in meaning* is never produced — only whitespace and trailing dots are ever removed.
- **Implement**: `pub fn validate(raw: &str) -> Result<ValidTitle, Rejection>`: trim leading/trailing whitespace, strip trailing `.` and spaces, reject if empty, reject on the illegal-char set and control chars (check before and after trimming so an illegal char is never hidden), then reject if the stem before the first `.` case-insensitively equals a reserved device name. `ValidTitle` derefs to `&str`. Pure — no filesystem, no path types.
- **Skills**: —
- **Done when**: `cargo test --test title` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`.

### [x] T4: Project code pattern and media extension allowlist  [deps: T1]

- **Files**: `crates/vault/src/code.rs`, `crates/vault/src/media.rs`, `crates/vault/tests/code.rs`, `crates/vault/tests/media.rs`
- **Test first**: `crates/vault/tests/code.rs` — cases: (FR-4) `ELS`, `GIS`, `AB`, `A1B2C3`, and a 10-char `ABCDEFGHIJ` accepted; `A` (1 char) and an 11-char code → `InvalidProjectCode`; `els` and `Els` → `InvalidProjectCode` (uppercase-only, see R4 — this is the resolution of the FR-4 prose vs. acceptance-bullet conflict); `EL S` → `InvalidProjectCode`; `""` → `EmptyProjectCode`; `1ELS` → `InvalidProjectCode`; (FR-15) `UNSORTED` → `ReservedProjectCode`, and `unsorted`/`Unsorted` reject too (whatever the variant, they must not become a project); (FR-14 defense in depth) `..`, `\\?\C:`, `C:`, `ELS\evil`, `ELS/evil` all reject and none of them can reach the filesystem layer. `crates/vault/tests/media.rs` — cases: (FR-7) all ten of `mp4 mkv mov webm avi m4a mp3 wav flac ogg` accepted and normalized to lowercase; `MP4` → `mp4`; `exe`, `txt`, `json`, `md`, `""` → `Err(VaultError::UnsupportedMediaType { ext })` carrying the offending extension; `movie.mp4.exe` resolves on the **last** extension and is unsupported; a name with no dot at all is unsupported; a non-ASCII extension is unsupported; extension extraction never panics on a name that is only dots.
- **Implement**: `code.rs`: `pub fn validate(raw: &str) -> Result<ProjectCode, Rejection>` — hand-rolled `^[A-Z][A-Z0-9]{1,9}$` over `char`s (no regex crate), then a case-insensitive `unsorted` reserved-word check that runs regardless of the pattern outcome so `UNSORTED` maps to `ReservedProjectCode` rather than a generic rejection. `media.rs`: `pub fn from_file_name(name: &str) -> Result<MediaExt, VaultError>` splitting on the last `.`, ASCII-lowercasing, matching a `const ALLOWED: [&str; 10]`; `MediaExt` exposes `as_str()` and `source_file_name()` → `source.<ext>`. Also `pub fn stem(name: &str) -> &str` for the unsorted fallback. Both modules pure.
- **Skills**: —
- **Done when**: `cargo test --test code --test media` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`.

### [x] T5: Path containment, length cap, and vault name shaping  [deps: T1]

- **Files**: `crates/vault/src/paths.rs`, `crates/vault/tests/paths.rs`
- **Test first**: `crates/vault/tests/paths.rs` — cases: (FR-14) `contained_child(root, ["..", "x"])`, `["ELS", "..", "..", "evil"]`, `["C:\\Windows"]`, `["\\\\?\\C:"]`, `["\\\\server\\share"]`, `["ELS/evil"]`, `["ELS\\evil"]`, `["."]`, `[""]` each return `VaultError::PathEscapesVault`; a legal `["ELS", "260812 - Security issue"]` returns a path that `starts_with` the canonicalized root; (FR-14 second bullet) after **all** of the rejecting calls above, the root directory is still empty — the containment check creates nothing; (NFR-4) `check_len` on a destination whose full absolute form including `\source.mp4` exceeds 260 returns `PathTooLong { len, limit: 260 }`, and a 259-char one passes; (FR-8) `meeting_folder_name("260812", "Security issue")` == `260812 - Security issue`; (FR-10) `unsorted_folder_name(date(2026,8,21), "random meeting")` == `260821 - random meeting`; the unsorted shaper repairs rather than rejects (R6): `"Q3: review"` → `260821 - Q3_ review`, `"bad\\name"` → underscore, trailing dots/spaces trimmed, an empty or all-illegal stem → `260821 - recording`, a stem longer than 120 chars truncated on a char boundary (never mid-UTF-8, assert with an emoji stem); (FR-11) `suffixed("260812 - Security issue", 2)` == `260812 - Security issue (2)`; (FR-15) the reserved-name constants `source`, `transcript.json`, `summary.md`, `unsorted` are exported and no function in this module writes any of them.
- **Implement**: pure/lexical module over `&Path`/`&str`. `contained_child(root: &Path, components: &[&str]) -> Result<PathBuf, VaultError>` rejects a component that is empty, `.`, `..`, contains `/` `\` or `:`, or starts with `\\`; then joins onto a canonicalized root and asserts `starts_with`. The only filesystem call permitted is canonicalizing the (already existing) root. `check_len` measures the full absolute destination including the `source.<ext>` leaf against 260 (R9). Name shapers as above; document in module docs why unsorted repairs while sorted rejects.
- **Skills**: —
- **Done when**: `cargo test --test paths` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`; a test asserts the vault root is still empty after every rejection path.

### [x] T6: File transfer — rename/copy, verify, delete, roll back  [deps: T1]

- **Files**: `crates/vault/src/transfer.rs`, `crates/vault/tests/transfer.rs`
- **Test first**: `crates/vault/tests/transfer.rs` (real dirs via `tempfile`) — cases: (FR-12) a successful transfer leaves the destination byte-length equal to the original and the **original absent**; (R7/FR-11) the destination's mtime equals the original's mtime after transfer — assert it, because the size+mtime dedupe depends on it; (FR-12) a forced failure — pre-create the destination path as a *directory* named `source.mp4` — returns an error, leaves the original intact and creates no partial file; a zero-byte source transfers cleanly; a missing source → `VaultError::SourceMissing`; a directory passed as the source → `SourceNotAFile`; (NFR-2) an 8 MiB file moved within the same temp volume completes in under 500 ms (same-volume rename path) — assert elapsed time; a simulated cross-volume transfer (call the copy path directly) verifies size before deleting, and when the verification is made to fail the destination is removed and the original survives.
- **Implement**: `pub(crate) fn transfer_into_place(src: &Path, dest: &Path) -> Result<(), VaultError>` — `fs::rename` first (same volume, atomic, mtime-preserving), verify `metadata(dest).len()`, rename back on mismatch; on `rename` failure (cross-volume or otherwise) fall back to `fs::copy` → verify size → `fs::remove_file(src)`, removing `dest` if verification fails so the original is never lost (FR-12, R5). Split the copy path into a separately callable `pub(crate) fn copy_verify_delete` so the test can exercise it without a second volume. Also `pub(crate) fn same_recording(a: &Path, b: &Path) -> io::Result<bool>` comparing size and mtime, which T9 uses for FR-11's duplicate re-drop. Every `io::Error` is wrapped with the path that produced it.
- **Skills**: —
- **Done when**: `cargo test --test transfer` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`; no test leaves anything outside its `tempfile` directory.

### [x] T7: Application-data directory as a concept distinct from the vault  [deps: T1]

- **Files**: `crates/vault/src/appdata.rs`, `crates/vault/tests/appdata.rs`
- **Test first**: `crates/vault/tests/appdata.rs` — cases: (FR-16) `app_data_dir("Transcriber")` returns `%LOCALAPPDATA%\Transcriber` as an absolute path; the function **creates nothing** — assert the returned path does not spring into existence (F4 owns installation); with `LOCALAPPDATA` unset the call returns `VaultError::AppDataUnavailable` rather than panicking; an app name containing `..`, `\`, `/` or `:` returns an error; `DEFAULT_APP_NAME` is a valid app name by the same rule.
- **Implement**: read `LOCALAPPDATA` via `std::env::var_os`, validate the app name as a single safe component with a local check (this module must not call `paths.rs` — that task runs concurrently in the same wave), join, return. Module docs state the FR-16 contract explicitly: the vault holds only meeting artifacts — no models, no logs, no databases, no config — and the concrete install location is F4's decision.
- **Skills**: —
- **Done when**: `cargo test --test appdata` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`.

### [x] T8: Filename parser — the pure classification entry point  [deps: T2, T3, T4]

- **Files**: `crates/vault/src/parse.rs`, `crates/vault/tests/parse_filename.rs`, `crates/vault/tests/parse_fuzz.rs`
- **Test first**: `crates/vault/tests/parse_filename.rs` — cases: (FR-2/FR-3) `ELS - 260812 - Security issue.mp4` → project `ELS`, date `260812`, title `Security issue`, ext `mp4`; `GIS - 260724 - Client demo.mp4` likewise; `ELS - 260812 - Security - issue - part 2.mp4` → title `Security - issue - part 2` (split on the first two separators only); `recording_final(1).mp4` and `ELS-260812-Security.mp4` → `Unsorted { reason: MissingSeparator }`; a name with exactly one `" - "` → `MissingSeparator`; the parser is exercised against a path that does not exist on disk and the test creates no fixture directory at all (FR-2 acceptance); (FR-7) `... .exe` and `... .txt` → `Err(VaultError::UnsupportedMediaType)`, **not** `Unsorted` — the extension gate runs before classification; (FR-4/R4) `els - 260812 - x.mp4` → `Unsorted { InvalidProjectCode }`; (FR-15) `unsorted - 260812 - x.mp4` and `UNSORTED - 260812 - x.mp4` → `Unsorted { ReservedProjectCode }`; (FR-5) `ELS - 260230 - x.mp4` → `Unsorted { DateNotACalendarDate }`; (FR-6) `ELS - 260812 - Q3: review.mp4` → `Unsorted { IllegalTitleCharacter(':') }`, `ELS - 260812 - NUL.mp4` → `ReservedDeviceName`, `ELS - 260812 -  .mp4` → `EmptyTitle`; (FR-14) `.. - 260812 - x.mp4` and `ELS - 260812 - ..\..\evil.mp4` are rejections, and the unsorted result they produce still carries the original stem for the fallback name; the first failing rule wins, and the reported reason is the specific one (NFR-5); (NFR-1) a 4096-char filename classifies in under 1 ms (measure over 100 iterations to beat timer granularity). `crates/vault/tests/parse_fuzz.rs` — (NFR-3) a seeded xorshift generator (no dependency) produces 10 000+ filenames drawn from an alphabet of ASCII, the separator string, `" - "` fragments, control chars, `\u{0}`, emoji, RTL marks, lone surrogate-ish sequences expressed as valid UTF-8, plus lengths up to 32 768; every call returns `Ok`/`Err` and none panics; the same seed reproduces the same run.
- **Implement**: `pub fn classify_filename(file_name: &str) -> Result<Classified, VaultError>`: extension gate via `media::from_file_name` first (FR-7 aborts), then `media::stem`, then `stem.splitn(3, " - ")` to honour "first two separators only", then `code::validate` → `date::validate` → `title::validate`, mapping the first `Rejection` into `Classified::Unsorted { reason, stem, ext }`. `Classified::Sorted(ParsedName { project, date, title, ext, stem })`. Zero filesystem access in this module.
- **Skills**: —
- **Done when**: both test files pass, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`; the fuzz test is deterministic (fixed seed) and runs in the default `cargo test` sweep in under a few seconds.

### [x] T9: Vault layout — init, project-folder reuse, collision policy  [deps: T5, T6]

- **Files**: `crates/vault/src/layout.rs`, `crates/vault/tests/layout.rs`
- **Test first**: `crates/vault/tests/layout.rs` (real dirs via `tempfile`) — cases: (FR-1) `init` on an empty directory creates `<root>\unsorted\`; calling it twice more changes nothing, returns success, and does not touch any existing child; `init` on a path that exists as a **file** returns `VaultError::RootIsNotADirectory`, not a panic; `init` on a path whose parent does not exist creates the whole chain or returns a typed `Io` error, never panics; (FR-9) with `<root>\els\` pre-existing, resolving project `ELS` returns exactly `<root>\els` and creates no sibling `ELS`; with nothing pre-existing it creates `<root>\ELS`; a pre-existing *file* named `<root>\ELS` yields a typed error; the resolver is case-insensitive but never renames what it finds; (FR-11) `resolve_meeting_dir` on a free name → `Placement::Fresh`; when `<parent>\<name>\source.mp4` exists with the **same size and mtime** as the incoming file → `Placement::DuplicateRedrop` and the existing file is byte-for-byte untouched afterwards (assert content and mtime); when it exists with a different size → `Placement::Suffixed { dir: "... (2)", n: 2 }`; when ` (2)` is also taken by a different file → `(3)`; a duplicate match against a suffixed folder is reported as a duplicate re-drop of that folder; beyond 999 suffixes → `VaultError::SuffixLimitExceeded`; no path in this module ever truncates or overwrites an existing `source.*`.
- **Implement**: `pub fn init(root: &Path) -> Result<(), VaultError>` (root + `unsorted`, idempotent, `create_dir_all`, explicit `is_dir` checks first). `pub(crate) fn ensure_project_dir(root, code) -> Result<PathBuf, VaultError>` — read `root`'s entries and reuse the first whose file name `eq_ignore_ascii_case` the code, else create the uppercase name; go through `paths::contained_child` for both. `pub(crate) fn resolve_meeting_dir(parent: &Path, base_name: &str, incoming: &Path) -> Result<Placement, VaultError>` implementing the FR-11 policy on top of `transfer::same_recording`, probing `paths::suffixed` names. Resolution never creates the meeting directory — T10 does that, after the containment check, so rollback has a single owner.
- **Skills**: —
- **Done when**: `cargo test --test layout` passes, then full `cargo test`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`.

### [x] T10: Ingest orchestration, rollback, and the public API surface  [deps: T7, T8, T9]

- **Files**: `crates/vault/src/ingest.rs`, `crates/vault/src/lib.rs`, `crates/vault/tests/ingest.rs`
- **Test first**: `crates/vault/tests/ingest.rs` (real dirs via `tempfile`) — cases: (FR-8) ingesting `ELS - 260812 - Security issue.mp4` yields exactly `<root>\ELS\260812 - Security issue\source.mp4` and **no other file** anywhere under the root — walk the tree and assert the full file list; (FR-7) `.MP4` ingests and produces `source.mp4`; `.exe` and `.txt` return `UnsupportedMediaType` and create nothing on disk, not even the project folder; (FR-9) with `<root>\els\` pre-existing the ingest writes into it; (FR-10) `random meeting.mp4` ingested with `ingest_on(.., 2026-08-21)` lands at `<root>\unsorted\260821 - random meeting\source.mp4` and in no project folder; two unsorted files ingested with different injected dates sort by name in date-added order; the unsorted result's `meeting_dir` exists and is writable, so F2 could drop `transcript.json` into it — the test actually writes one there; (FR-11) ingesting the same file twice reports `CollisionOutcome::DuplicateRedrop`, leaves exactly one `source.mp4`, unmodified; ingesting a *different* file with the same name reports `SuffixedFolder(2)` and both recordings survive intact; (FR-12) after a success the original is **absent**; with the destination sabotaged (a directory pre-created at the `source.mp4` path) the call errors, the original is intact, no `source.*` exists in the vault, and the meeting folder the attempt created is gone — while a project folder that already existed beforehand is *not* removed; (FR-13) `meeting_dir` and `source_path` are absolute, the directory exists at return time, a sorted result carries project/date/title and a `Fresh` collision outcome, an unsorted result carries a `Rejection`; (FR-14) `.. - 260812 - x.mp4`, `ELS - 260812 - ..\..\evil.mp4` and a project code of `\\?\C:` all fail or route to unsorted without ever creating anything outside the root — assert the parent of the root is unchanged; a test asserts the containment check runs *before* directory creation by pointing an over-long destination at a fresh root and observing that no project directory appeared; (NFR-4) a title that pushes the absolute destination past 260 chars returns `PathTooLong` and creates nothing; (FR-15) `summary.md` does not exist anywhere after any of the above; a missing source file → `SourceMissing`; a directory dropped instead of a file → `SourceNotAFile`.
- **Implement**: `ingest.rs` defines `Vault`, `Ingested`, `Classification`, `CollisionOutcome` and drives the 9-step sequence from the architecture overview, with a small `Rollback` guard recording which directories this call created so failure unwinds exactly those and no more. `Vault::open` calls `layout::init` and stores the canonicalized root. `ingest` delegates to `ingest_on(source, date::today_local())`. Then rewrite `lib.rs` to add the curated `pub use` surface (`Vault`, `Ingested`, `Classification`, `CollisionOutcome`, `VaultError`, `Rejection`, `classify_filename`, `Classified`, `ParsedName`, `app_data_dir`, the reserved-name constants) and crate-level docs showing the exact call F3's `#[command]` will make — keeping the `pub mod` declarations so nothing else breaks. Treat every argument as hostile (the `cli` profile's library rule, and the `desktop` profile's "a file dialog result is not validation").
- **Skills**: —
- **Done when**: `cargo test` passes in full, `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` are clean, and `cargo doc --no-deps` builds without warnings (the crate denies `missing_docs`).

### [x] T11: Acceptance sweep, consumer smoke run, QA gate  [deps: T10]

- **Files**: `crates/vault/tests/acceptance.rs`, `crates/vault/examples/f3_consumer.rs`, `crates/vault/README.md`
- **Test first**: `crates/vault/tests/acceptance.rs` — one test function per acceptance-criteria bullet in the spec, named after its FR (`fr08_exact_destination_and_nothing_else`, `fr12_failed_transfer_rolls_back`, …), each written against the **public** API only, the way F3 will call it, over a real `tempfile` vault. This file is the traceability artifact: every bullet under "Acceptance criteria" in `specs/meeting-vault-layout/spec.md` is either a test here or carries a comment naming the earlier test file that covers it. Include the two spec bullets that no single earlier task owns end to end: a full-session scenario ingesting one sorted, one unsorted and one duplicate recording in sequence and asserting the resulting tree exactly; and the FR-5 note recording that `260229` is rejected because 2026 is not a leap year (R3), so the deviation from the spec's wording is visible in the test suite rather than buried.
- **Implement**: `examples/f3_consumer.rs` — a scratch consumer mirroring F3's drop handler: takes `<vault root>` and `<dropped file>` from argv, calls `Vault::open` then `ingest`, prints the classification, meeting-folder path, source path and collision outcome to stdout and any error to stderr with a nonzero exit code (the `cli` profile's Verification rules on stdout/stderr and exit codes). Run it for real before finishing, per the profile: one sorted recording, one badly named one, one re-drop, one `.txt`. Finish `README.md`: the F3 usage snippet, the vault layout diagram, the reserved names, and the three QA commands.
- **Skills**: —
- **Done when**: from `crates/vault/`, all three QA commands pass — `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`; `cargo run --example f3_consumer -- <temp vault> "<temp>\ELS - 260812 - Security issue.mp4"` prints an absolute meeting-folder path that exists, exits 0, and the same command on a `.txt` exits nonzero with the message on stderr and nothing written to the vault; every acceptance bullet in the spec maps to a named test.

## QA expectations

- **Makefile targets present: none.** There is no `Makefile` in this repository and `make` is not on PATH. Per the batch decision, the root `Makefile` and repo-wide QA entry points are **owned by F4** — this feature must not create one.
- This feature's QA commands, all run with `crates/vault/` as the working directory:
  - format — `cargo fmt --check`
  - lint — `cargo clippy --all-targets -- -D warnings`
  - type/build — `cargo build` (and `cargo doc --no-deps`, since the crate denies `missing_docs`)
  - test — `cargo test`
- If PATH has not been refreshed since T1, invoke cargo through `& "$env:USERPROFILE\.cargo\bin\cargo.exe"`.
- Known-flaky risks to watch: the NFR-2 timing assertion in T6 (8 MiB same-volume rename under 500 ms) and the NFR-1 timing assertion in T8 — both are wall-clock assertions with generous margins; if a machine under load makes them flake, keep the assertion but raise the margin rather than deleting the check. Parallel wave agents share one `target/` directory, so cargo may block on the build lock — that is correct behavior, not a failure.
