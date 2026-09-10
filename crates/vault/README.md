# vault

A vault domain library that turns a dropped meeting recording into a
well-formed place on disk. See `specs/meeting-vault-layout/spec.md` for the
full requirements this crate implements.

## QA commands

Run from `crates/vault/` (this directory):

```powershell
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

`cargo doc --no-deps` also builds cleanly (the crate denies `missing_docs`).

There is no repo-wide `Makefile` — this crate does not own one. Per the
batch decision, repo-wide QA entry points belong to F4.

## Naming convention

A dropped recording is filed by its own filename:

```text
<Project code> - <date> - <Title>[ - <Type>].<ext>
```

`-` is reserved as the separator: the stem is split on **every** hyphen,
the spaces around a separator are optional (`ELS-260812-Title.mp4` parses
like `ELS - 260812 - Title.mp4`) and each section is trimmed of ASCII
spaces, while spaces inside a section are kept. Three sections are an
untyped meeting, four carry the optional meeting type (same character
rules as the title), fewer than three are a missing separator, and five or
more mean a section contained the reserved `-` — those go to `unsorted/`,
never rejected. Only an unsupported extension is rejected outright.

## Vault layout

```text
<vault root>/
  <PROJECT>/
    <date> - <Title>[ - <Type>]/
      source.<ext>
  unsorted/
    <YYMMDD of ingest> - <original stem>/
      source.<ext>
```

Every ingested recording — sorted or unsorted — gets its own folder, so
later artifacts (a transcript, eventually a summary) can be written next to
the source. The meeting type is persisted in the folder name and nowhere
else: `vault::paths::meeting_folder_name(date, title, kind)` writes it and
`vault::parse_meeting_folder_name(name)` reads it back (two sections →
untyped, three → typed, anything else → `None`). An unsorted folder name
keeps the original stem verbatim, hyphens included.

## Reserved names

- `unsorted` — reserved at the vault root; can never be used as a project
  code, case-insensitively (FR-15).
- `source.*` — the recording inside a meeting folder.
- `transcript.json` — written by F2 into a meeting folder; this crate never
  writes it.
- `summary.md` — a placeholder only; nothing in this crate creates or writes
  it (out of scope).

## Scope extension: read-only listing

The original spec (`specs/meeting-vault-layout/spec.md`, "Out of scope")
explicitly excluded "browsing, listing, searching, renaming or re-filing
existing vault content" from the MVP. The operator has since extended that
scope for the vault-browser feature: `vault::list_meetings(root)` scans the
vault read-only (two levels — `<root>/<PROJECT>/<meeting>` and
`<root>/unsorted/<meeting>`) and returns every meeting folder found, newest
date first. It creates, writes, renames and deletes nothing, and tolerates
junk entries by skipping them rather than failing the whole call. See
`src/list.rs` for the full contract.

## API (F3's usage)

F3's Tauri `#[command]` links this crate directly and calls it synchronously
on drop:

```rust
use std::path::Path;
use vault::Vault;

let vault = Vault::open(Path::new(r"D:\MeetingVault"))?;
let ingested = vault.ingest(Path::new(
    r"C:\Users\me\Downloads\ELS - 260812 - Security issue.mp4",
))?;
// F3 passes `ingested.meeting_dir` straight to F2 as an argument.
println!("{}", ingested.meeting_dir.display());
```

The curated public surface (re-exported from the crate root): `Vault`,
`Ingested`, `Classification`, `CollisionOutcome`, `VaultError`, `Rejection`,
`classify_filename`, `Classified`, `ParsedName`, `parse_meeting_folder_name`,
`MeetingFolderName`, `app_data_dir`, and the reserved-name constants (`SOURCE_STEM`, `TRANSCRIPT_FILE_NAME`,
`SUMMARY_FILE_NAME`, `UNSORTED_DIR_NAME`). Individual modules
(`vault::paths`, `vault::date`, …) remain reachable directly for anything
not curated at the crate root.

## Scratch consumer

`examples/f3_consumer.rs` mirrors F3's drop handler for manual smoke-testing
against a real vault directory:

```powershell
cargo run --example f3_consumer -- <vault root> <dropped file>
```

Prints the classification, meeting-folder path, source path and collision
outcome to stdout on success; on failure, prints the error to stderr and
exits non-zero without writing anything to the vault.
