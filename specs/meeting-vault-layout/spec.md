---
slug: meeting-vault-layout
created: 2026-08-21
status: approved
---

# Spec: Meeting vault layout and naming convention

## Summary

A vault domain library that turns a dropped meeting recording into a well-formed place on disk. It parses the filename against the convention `<Project code> - <date> - <Title>.<ext>`, and either files the recording at `<vault root>/<PROJECT>/<date> - <Title>/source.<ext>` or, when the name does not conform, into `<vault root>/unsorted/` ordered by date added. Every ingested recording gets its own folder, because transcripts (F2) and later artifacts are written next to the source. The library is the single owner of vault paths: F3 (Tauri) calls it on drop, and F2 (Python) only ever receives the resulting meeting-folder path.

## Problem & context

The operator works across several projects and records meetings for each. Today the recordings are dumped flat into a `Meetings` folder on Windows (`D:\Local\Git\transcriber\IDEA.md` lines 1-4). There is no per-meeting home for the artifacts the tool will produce, no grouping by project, and no defined behavior for files that were named carelessly. Before anything can be transcribed, the tool needs a deterministic answer to "where does this file go, and where do its artifacts live".

The repository is greenfield: `D:\Local\Git\transcriber\IDEA.md`, a `.gitignore`, and `specs/` are the only tracked content. `D:\Local\Git\transcriber\vexa\` is a gitignored read-only reference clone belonging to F2 and is out of scope here. No build files, no source tree, no `Makefile` exist yet, so this feature also lays the first source directory of the project.

Two folders are required by the source document (`IDEA.md` line 8): the user-visible vault, and an application folder holding scripts, models and internals the user never needs to see. This spec fixes the boundary between them; F4 owns where the application folder is actually installed.

## Users

- **Operator (sole end user)** — drags a meeting recording onto the desktop app and expects it to land in the right project folder, or in `unsorted` when they got the name wrong, without ever having to open Explorer.
- **F3 Tauri/Rust desktop app (consumer)** — calls this library from the drop handler; receives a classification and an absolute meeting-folder path.
- **F2 Python transcription service (consumer)** — receives a meeting-folder path as an argument and writes `transcript.json` into it. It never parses filenames and never chooses paths.

## Profiles

Detection probes were run against the actual repository. It is greenfield, so **no profile's detection signals are present today**.

- `cli` — matched by construction, on the profile's own terms: this feature is a library with no entry point and no UI dependency, and the profile's negative signal holds literally (no `react` / `vue` / `tauri` / `electron` / Qt dependency exists anywhere in the repo — there is no dependency manifest at all). The `cli` profile explicitly covers "a published package with no entry point and no UI dependency → library".
- `desktop` — **not** matched for this feature, flagged as a forward match for the repository. `src-tauri/tauri.conf.json` does not exist yet; F3 will create it. Recorded here because this library's only write-path caller will be a Tauri `#[command]`, so the `desktop` profile's path-traversal rule ("a file dialog result is not validation") binds this library's input boundary even though no `desktop` code is in this feature.
- `web` — not matched. No `package.json`, no HTTP surface. F3 will introduce it.

## Detected stack

Nothing is detectable by probe. The rows below record what this feature establishes, with the decision that fixes each.

| Layer | Technology | Evidence |
|---|---|---|
| Vault library | Rust (library crate, no async, no I/O in the parser) | Greenfield; language chosen in Decisions log 2026-08-21, see rationale below |
| Consumers | Tauri 2 + Rust (F3), Python (F2) | `specs/_intake/idea/intake.md` F2/F3 task text |
| Testing | `cargo test` built-in harness | No test framework present; Rust has no profile toolkit row |
| Target OS | Windows only for MVP | `specs/_intake/idea/intake.md` Decisions log 2026-08-21 |
| Toolchain | Rust toolchain **not installed** on this machine (`cargo` not on PATH, no `%USERPROFILE%\.cargo`, no `.rustup`); `uv 0.8.17` and `node v22.17.1` are present | Probed 2026-08-21 |

Makefile QA targets present: **none**. There is no `Makefile` in the repository, and `make` itself is not on PATH. `make -n format`, `make -n lint`, `make -n type` and `make -n test` all fail with `make: command not found`. This feature should introduce the QA entry points the rest of the batch will inherit.

**Why Rust and not Python.** The two consumers need different things. F3 needs the whole write path — parse, validate, create the meeting folder, transfer the file — synchronously inside the drop handler. F2 needs only one thing: a folder path to write `transcript.json` into, which F3 hands it as an argument. That asymmetry makes the choice one-sided: in Rust, F3 links the crate directly and F2 needs nothing at all; in Python, F3 would have to spawn a Python interpreter on every drop just to decide a folder name, putting the Python runtime and its startup cost on the app's most latency-visible path and forcing a serialization contract for what is a function call. Rust also gives the parser a single implementation, so the two processes cannot drift on what counts as a valid name. Not treated as an open question — the trade-off does not survive scrutiny in the other direction.

## Functional requirements

- **FR-1** (must): Given a configured vault root path, the library ensures the root directory and `<root>/unsorted/` exist, creating them if absent, and is idempotent across repeated calls. The vault root path itself is supplied by the caller (F4 lets the user choose it at install time); this library does not decide or persist it.
- **FR-2** (must): A **pure** parsing function takes a filename string and returns either a parsed meeting (project code, date, title, extension) or a rejection carrying a machine-readable reason. It performs no filesystem access, so it is fully unit-testable.
- **FR-3** (must): The filename grammar is `<Project code>` + `" - "` + `<date>` + `" - "` + `<Title>` + `"." + <ext>`, splitting on the **first two** occurrences of the separator `" - "` (space, hyphen, space) so that titles may themselves contain `" - "`. A filename with fewer than two separators is a rejection.
- **FR-4** (must): The project code is validated and normalized to uppercase for the folder name; a code that fails validation makes the file unsorted. Validation rule (Q1 resolved → **A**): pattern only, `^[A-Z][A-Z0-9]{1,9}$` applied case-insensitively to the raw code before uppercasing is NOT performed — the raw code must already match `^[A-Za-z][A-Za-z0-9]{1,9}$` and is then normalized to uppercase.
- **FR-5** (must): The date component must be exactly six digits in `YYMMDD` form and must denote a real calendar date (`260230` and `991345` are rejections). The date is preserved verbatim in the meeting folder name — no reformatting.
- **FR-6** (must): The title must be non-empty after trimming and is sanitized for Windows: characters `< > : " / \ | ? *` and control characters are rejected, trailing dots and spaces are stripped, and a title whose stem matches a reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) is a rejection. Sanitization never silently rewrites a title into a different meaning — a title that cannot be used verbatim is a rejection, not a repair.
- **FR-7** (must): Only recording media extensions are accepted for ingest: `mp4`, `mkv`, `mov`, `webm`, `avi`, `m4a`, `mp3`, `wav`, `flac`, `ogg` (case-insensitive). A file with any other extension is **not ingested at all** and returns an error to the caller — `unsorted` is for media files with bad names, not for arbitrary files.
- **FR-8** (must): A valid recording is placed at `<root>/<PROJECT>/<date> - <Title>/source.<ext>`, preserving the original extension in lowercase. The meeting folder name is the original filename with the project-code prefix and the extension trimmed, exactly as `IDEA.md` line 27 describes.
- **FR-9** (must): If a project folder matching the code already exists under the root, it is reused regardless of letter case (Windows filesystems are case-insensitive); otherwise it is created with the normalized uppercase name.
- **FR-10** (must): A recording that fails any of FR-3 through FR-6 is routed under `<root>/unsorted/`, ordered by date added. Layout (Q4 resolved → **A**): folder per file with a date-added prefix — `unsorted/<YYMMDD of ingest> - <original stem>/source.<ext>` — uniform with sorted meetings, so an unsorted recording still has a meeting folder F2 can write `transcript.json` into.
- **FR-11** (must): Destination collisions are detected before any write and resolved by a single defined policy (Q2 resolved → **D**): if the incoming file is byte-identical in size and mtime to the existing `source.*`, the ingest is a **no-op** reported as a duplicate re-drop; otherwise a new meeting folder with a numeric suffix (`<date> - <Title> (2)`) is created. The library never silently overwrites an existing `source.*`.
- **FR-12** (must): Transfer semantics of the dropped file (Q3 resolved → **C**): **copy, verify size at destination, then delete the original**. Atomic in effect: a failure part-way leaves neither a partially-written `source.*` in the vault nor a destroyed original, and any directory the operation created is removed on rollback. The original is deleted only after successful verification.
- **FR-13** (must): The ingest call returns a result the caller can act on without re-deriving anything: the classification (`sorted` / `unsorted`), the absolute meeting-folder path, the absolute `source.<ext>` path, the parsed project/date/title when sorted, and the rejection reason when unsorted. F3 passes the meeting-folder path straight to F2.
- **FR-14** (must): Every computed destination path is verified to be contained within the vault root after normalization. Any input that would escape it (`..` segments, absolute paths, drive-relative paths, UNC or `\\?\` device prefixes smuggled through the project code or title) is rejected before any filesystem call. The caller's file-dialog or drag-drop origin is not treated as validation.
- **FR-15** (must): `unsorted` is a reserved name and can never be used as a project code (case-insensitively); such a filename is itself routed to unsorted. Inside a meeting folder, `source.*`, `transcript.json` and `summary.md` are reserved names owned by the vault contract. `summary.md` is a placeholder only — nothing in this feature creates or writes it.
- **FR-16** (should): The library exposes the application-data directory as a concept distinct from the vault (`IDEA.md` line 8: the app folder holds scripts and models the user need not see) and guarantees the vault contains only meeting artifacts — no models, no logs, no databases, no config. The concrete install location is F4's decision; the default assumed here is `%LOCALAPPDATA%\<AppName>\`.

## Non-functional requirements

- **NFR-1**: Filename parsing (FR-2) touches no filesystem and completes in under 1 ms for any input up to 4096 characters.
- **NFR-2**: Ingest of a recording already on the vault's volume completes in under 500 ms regardless of file size, by using a rename rather than a byte copy.
- **NFR-3**: No input — arbitrary bytes, non-UTF-8 sequences, 32k-character filenames, empty strings, emoji, RTL marks — causes a panic or an unhandled error; every path returns a typed rejection.
- **NFR-4**: Total destination path length is checked against the Windows 260-character limit before writing; an ingest that would exceed it fails with a distinct, actionable error rather than a raw OS error.
- **NFR-5**: Every rejection reason is a distinct enumerated variant, so F3 can render a specific message ("date is not a real calendar date") rather than "invalid filename".

## Acceptance criteria

- **FR-1**:
  - [ ] Calling init on an empty directory creates `<root>/unsorted/`; calling it twice more changes nothing and returns success.
  - [ ] Calling init on a path that exists as a file returns an error, not a panic.
- **FR-2/FR-3**:
  - [ ] `ELS - 260812 - Security issue.mp4` parses to project `ELS`, date `260812`, title `Security issue`, ext `mp4`.
  - [ ] `GIS - 260724 - Client demo.mp4` parses to project `GIS`, date `260724`, title `Client demo`, ext `mp4`.
  - [ ] `ELS - 260812 - Security - issue - part 2.mp4` parses with title `Security - issue - part 2` (split on the first two separators only).
  - [ ] `recording_final(1).mp4` and `ELS-260812-Security.mp4` (no spaced separators) are both rejections.
  - [ ] The parser is called in a test with no filesystem present in the fixture and passes.
- **FR-4**: covered by Q1's chosen rule; at minimum a lowercase code, a code containing a space, and an empty code are rejections.
- **FR-5**:
  - [ ] `260230` (Feb 30) and `991345` are rejections; `260229` is accepted (2026 leap-year check applied correctly for `260228`/`260229`).
  - [ ] `2026-08-12` and `26081` are rejections.
  - [ ] The accepted date appears verbatim in the folder name: `260812 - Security issue`.
- **FR-6**:
  - [ ] `ELS - 260812 - Q3: review.mp4` is a rejection (illegal `:`).
  - [ ] `ELS - 260812 - NUL.mp4` is a rejection.
  - [ ] `ELS - 260812 -  .mp4` (blank title) is a rejection.
  - [ ] `ELS - 260812 - Review .mp4` yields folder `260812 - Review` with the trailing space stripped.
- **FR-7**:
  - [ ] `ELS - 260812 - Security issue.MP4` ingests, producing `source.mp4`.
  - [ ] `ELS - 260812 - Security issue.exe` and `... .txt` return an unsupported-type error and create nothing on disk.
- **FR-8/FR-9**:
  - [ ] Ingesting `ELS - 260812 - Security issue.mp4` yields exactly `<root>/ELS/260812 - Security issue/source.mp4` and no other files.
  - [ ] With `<root>/els/` pre-existing, a second ingest for `ELS` writes into that existing folder and does not create a sibling.
- **FR-10**:
  - [ ] `random meeting.mp4` lands under `<root>/unsorted/` per Q4's layout and is not placed in any project folder.
  - [ ] Two unsorted files added in a known order are distinguishable by date added through the chosen layout.
  - [ ] An unsorted entry exposes a meeting-folder path in its result that F2 could write `transcript.json` into.
- **FR-11**:
  - [ ] Ingesting the same valid filename twice applies Q2's policy and never leaves the first `source.*` overwritten or truncated.
  - [ ] The second result reports the collision outcome explicitly.
- **FR-12**:
  - [ ] After a successful ingest, the original file is present or absent exactly as Q3 specifies.
  - [ ] Simulating a failure during transfer (destination made unwritable) leaves no `source.*` in the vault, leaves the original intact, and removes the meeting folder the attempt created.
- **FR-13**:
  - [ ] The returned meeting-folder path is absolute and the folder exists at return time.
  - [ ] An unsorted result carries a rejection reason; a sorted result carries project, date and title.
- **FR-14**:
  - [ ] `.. - 260812 - x.mp4`, `ELS - 260812 - ..\..\evil.mp4` and a project code of `\\?\C:` all reject with a containment error and create nothing outside the root.
  - [ ] A test asserts the containment check runs before any directory creation.
- **FR-15**:
  - [ ] `unsorted - 260812 - x.mp4` and `UNSORTED - 260812 - x.mp4` are routed to unsorted, not treated as a project.
  - [ ] No code path in this feature creates or writes `summary.md`; a test asserts it is absent after ingest.
- **NFR-3**:
  - [ ] A property/fuzz-style test over randomized filenames produces no panic across at least 10k cases.
- **NFR-4**:
  - [ ] A title that pushes the full destination past 260 characters returns the dedicated path-too-long error and creates nothing.

## Out of scope

- Summary generation and any writing of `summary.md` — reserved filename only (operator decision, 2026-08-21).
- Transcription itself and the content or schema of `transcript.json` — F2 owns it; this feature only guarantees the folder it goes into.
- Any UI, drag-and-drop handling, Tauri commands or React components — F3.
- The installer, choosing/persisting the vault root at install time, and downloading whisper — F4.
- Non-Windows platforms. The path rules here are Windows-specific by decision; macOS/Linux support is future work.
- Browsing, listing, searching, renaming or re-filing existing vault content; promoting an unsorted meeting into a project after the fact; a vault index or database.
- Watching the vault folder for externally added files; batch import of an existing `Meetings` folder.
- Media inspection of any kind — duration, codec, content-based type sniffing. Classification is by filename and extension only.
- Migration or versioning of the on-disk layout.

## Applicable toolkits

Union of the matched profile's Toolkits rows, filtered to signals actually observed in this repository. The `cli` profile offers three rows — pytest, Docker, published package/binary. **None of their signals are present**: there is no `pyproject.toml` or pytest dependency (this feature is Rust), no Dockerfile or compose file, and no publishable artifact (the crate is consumed in-tree by F3; the distributable binary is F4's deliverable).

- *(none)*

The `cli` profile has no Rust testing row, so verification rides on the built-in `cargo test` harness plus the profile's inline Verification rules: exercise the public API from a scratch consumer script the way F3 will call it, against a real temporary vault directory, before declaring a task done. Every public function is an attack surface — validate at the boundary and do not assume a well-behaved caller.

**Mandatory skills**: none. The `cli` profile declares none, and the `workflow-toolkit` discipline skills every implementer invokes are sufficient.

## Open questions

*All resolved by the operator at the spec gate (2026-08-21): Q1 → A, Q2 → D, Q3 → C, Q4 → A. Retained below for the record.*

**Q1 — How strictly is a project code validated?** This decides how many stray project folders a typo can create.
- **A. Pattern only** — `^[A-Z][A-Z0-9]{1,9}$`. Simple, zero config, works on day one; but `Zoom - meeting - notes.mp4`-style names with two separators can mint a bogus project folder, and a lowercase-typo code silently goes unsorted.
- **B. Pattern + must already exist** — valid only if `<root>/<CODE>/` exists; otherwise unsorted. Impossible to create a stray folder; but the very first meeting of every new project goes to unsorted until the operator hand-creates the folder.
- **C. Pattern + registry file** — a small `projects.json` in the app folder lists known codes. Explicit and inspectable; adds a config surface and an "add project" flow that MVP has no UI for.
- **D. Pattern + auto-create with caller confirmation** — the library reports "unknown project code" and F3 asks the operator. Best UX; pushes a dialog into F3's MVP scope.

**Q2 — What happens when the destination meeting folder already exists?** Unavoidable: same project, same day, same title, or a simple re-drop.
- **A. Suffix the folder** — `260812 - Security issue (2)`. Never loses data; can quietly fragment one meeting across two folders on an accidental re-drop.
- **B. Reuse the folder, suffix the source** — `source.mp4` plus `source-2.mp4` in one folder. Keeps a day's recording together; breaks F2's assumption of exactly one `source.*` per folder.
- **C. Reject as a duplicate** — return an error, change nothing. Safest and most predictable; the operator must rename the file themselves to ingest a genuine second meeting.
- **D. Reject only on identical size+mtime, otherwise suffix the folder** — treats a re-drop as a no-op and a genuine second meeting as new. Most correct behavior; the most logic to build and test.

**Q3 — Is the dropped recording moved into the vault or copied?**
- **A. Move** — rename on the same volume, copy-then-delete across volumes. No duplicated multi-GB video, matches "I loaded it into the vault"; the file vanishes from Downloads, which surprises people once.
- **B. Copy, leave the original** — non-destructive and trivially safe; the operator ends up cleaning Downloads by hand and doubles disk use per meeting.
- **C. Copy, then delete the original only after verifying size (and hash) at the destination** — the safety of B with the tidiness of A; slower and more code on every ingest.

**Q4 — What does an unsorted entry look like on disk?** F2 must have a folder to write `transcript.json` into, so this decides whether badly-named files are processable at all.
- **A. Folder per file, date-added prefixed** — `unsorted/260821 - random meeting/source.mp4`. Uniform with sorted meetings, sorts by date added by name alone, F2 works unchanged; the original filename is no longer the folder name verbatim.
- **B. Folder per file, original stem** — `unsorted/random meeting/source.mp4`, ordering left to folder mtime. Preserves the name exactly; Explorer must be sorted by date manually and collisions on identical stems need handling.
- **C. Flat** — `unsorted/random meeting.mp4`, no folder. Simplest and most obvious to a human browsing; unsorted recordings then cannot be transcribed at all, since F2 has nowhere to write.
- **D. Flat with a date-added prefix** — `unsorted/260821 - random meeting.mp4`. Sorts correctly and stays simple; same blocker as C for transcription.

## Decisions log

- 2026-08-21 — Does the MVP generate `summary.md`? → No. Reserved filename/placeholder in the layout only; nothing in this feature writes it. (Operator, split gate.)
- 2026-08-21 — What is dragged into the app? → The meeting **recording** (mp4/audio), not a transcript file. (Operator, split gate.)
- 2026-08-21 — Which platforms does the MVP target? → Windows only; path rules are Windows-specific. (Operator, split gate.)
- 2026-08-21 — Which language owns the vault logic, given consumers F2 (Python) and F3 (Rust)? → **Rust**, as an in-tree library crate. F3 needs the entire write path synchronously on drop and links the crate directly; F2 needs only the meeting-folder path, which F3 passes as an argument. The reverse (Python) would put an interpreter spawn on the drag-drop hot path and require an IPC contract for a function call. Single implementation, so the two processes cannot disagree on what a valid filename is. (Analyst, spec draft — decided rather than asked; not genuinely ambiguous.)
- 2026-08-21 — Q1 project-code validation → **A: pattern only** (`^[A-Z][A-Z0-9]{1,9}$`, case-normalized). (Operator, spec gate.)
- 2026-08-21 — Q2 destination collision → **D: dedupe on identical size+mtime (no-op re-drop), otherwise suffix the folder** (`<date> - <Title> (2)`). (Operator, spec gate.)
- 2026-08-21 — Q3 transfer semantics → **C: copy, verify size at destination, then delete the original**. (Operator, spec gate.)
- 2026-08-21 — Q4 unsorted layout → **A: folder per file with date-added prefix** (`unsorted/<ingest date> - <stem>/source.<ext>`). (Operator, spec gate.)
