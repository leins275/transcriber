# Resume point — 260910-meeting-type-in-filename

Operator stopped 2026-09-10, before the blueprint gate. Nothing implemented; `main` is clean at the 0.24.0 release bump.

## Request (verbatim intent)
Meeting **type** as a structural element of the filename: `<PRJ>-<DATE>-<NAME>[-<TYPE>]`, optional, last.
`-` is reserved as the separator and may not appear inside a section. Spaces around separators are trimmed,
spaces inside a section are allowed. Any file may still be uploaded: if it does not fit the scheme it goes to `unsorted`.

## Operator decisions already taken (do NOT re-ask)
1. The type persists **in the meeting folder name**: `<YYMMDD> - <Name> - <TYPE>`, or `<YYMMDD> - <Name>` when absent.
2. Type is **free text**, same rules as the name (no hyphen, no illegal chars, not empty).
3. The type **is editable after ingest** on the recording page next to rename; editing renames the folder.

## Findings from the code (already verified)
- `crates/vault/src/parse.rs::classify_filename` splits with `splitn(3, '-')`, trims spaces around separators,
  validates code → date → title, first failure maps to `Classified::Unsorted`. A hyphen inside the title is
  currently ALLOWED — that is what changes.
- New rule: 3 sections = no type, 4 = type, **5+ = unsorted** (needs a new `Rejection` variant).
- `paths::meeting_folder_name(date, title)` builds `<YYMMDD> - <Title>`; project is the parent dir.
  Signature changes ripple into the app.
- `title::validate` rejects `< > : " / \ | ? *`, control chars, empty, Windows device names. `paths::check_len` = 260.
- `manage.rs::rename_meeting` / `resolve_meeting` are the rename seam; `commands/meetings.rs` is the app side.
- Python service never parses vault names (confirm in the blueprint).

## Consequences to accept, on the operator's real vault (51 meetings)
- `TBOT/260828 - Q&A Сессия с Дмитрием - поиск рабочего триггера` and similar will read as *typed* once the
  folder name is parsed back. Accepted; no migration planned for 51 meetings, but the final report should list
  the affected folders.
- Default recorder names (`Запись встречи 31.07.2026 11-04-56 - запись`) have hyphens in the time, so 5+ sections
  → they now land in `unsorted`. Correct per the rule; three such meetings already exist in TBOT.
- Rename/edit must reject a hyphen in either field or the folder-name round trip breaks.

## Explicitly out of scope
Using the type to drive roster groups or diarization bounds — a separate later feature.

## How to resume
`/sdd:ship 260910-meeting-type-in-filename`. A blueprint agent was running when the operator stopped; if
`blueprint.md` is absent or incomplete, relaunch it with the prompt reconstructed from this file. Then: blueprint
gate → test wave → implement → quality gate. Commit subject must be `feat:` (NOT `feat!:` — a `!` would bump to 1.0.0;
the repo is pre-1.0 with features_always_bump_minor).

## Update
The blueprint agent was stopped with the previous session; `blueprint.md` was never written. Relaunch it from the request and decisions above.
