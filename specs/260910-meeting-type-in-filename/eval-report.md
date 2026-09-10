---
slug: 260910-meeting-type-in-filename
base_ref: dd69f260d62c0b856a148ce3b1699ee0e95603e2
round: 1
---

# Evaluation report: Meeting type as an optional fourth section of the filename

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 4 | 0 | 0 |

The diff implements the blueprint. `crates/vault` remains the single authority: `classify_filename` splits on every `-`, trims ASCII spaces only, gates on section count before the validators (extension → count → code → date → title → type), and routes 5+ sections to `unsorted` with `TooManySeparators` — never an error. The four `Rejection` variants, `validate_kind`, the three-argument `meeting_folder_name`, its inverse `parse_meeting_folder_name`, the `MeetingUpdate.kind` seam with the `ReservedSeparator` guard placed before `ensure_project_dir`, and the app/UI pass-through are all present and each is pinned by tests written against the public API. The three parsers (`vault::parse_meeting_folder_name`, `lib/meetingName.ts`, `lib/fileName.ts`) agree on grammar, trimming and section counts; I found no path on which a type is lost or corrupted through the folder-name round trip. Verified locally on Windows: `cargo test -p vault --test acceptance --test parse_filename --test paths --test error_vocabulary --test parse_fuzz --test ingest --test title` (all green, 140 cases), `cargo test -p transcriber-desktop --test meeting_type` (8/8), and vitest on the seven affected files (116/116). Nothing under `services/` is touched. The remaining findings are two documentation/coverage gaps and two behavioural consequences worth an explicit decision — none blocks shipping.

## Test-mismatch adjudication (from test-wave.md)

| Item | Ruling | Evidence |
|---|---|---|
| Orchestrator widened T5 by `jobs.rs` for FR-7 c2 | Code right, blueprint's Files list was incomplete. FR-7 c2 is an approved criterion and the app had never surfaced an unsorted reason. `jobs.rs::ingest_message` reports the `Rejection` `Display` verbatim, joined with a collision note when both apply; the rename guard (`has_active_job_for`) is untouched. | `apps/desktop/src-tauri/src/jobs.rs:943-953`; `tests/meeting_type.rs::an_unsorted_drop_reports_why_its_name_did_not_conform` green |
| T3's transient `None` bridge in `manage.rs` | Gone. | `crates/vault/src/manage.rs:237-238` passes `valid_kind.as_deref()` |
| Second obsolete acceptance case (`ELS - 2026-08-12 - x.mp4`) | Test rewrite right, blueprint's prediction of one red case was incomplete. A four-digit-year date carries two hyphens → five sections → `TooManySeparators` is exactly FR-1 c7's ordering; a hyphen-free `DateNotSixDigits` case (`ELS - 26081 - x.mp4`) is retained. | `crates/vault/tests/acceptance.rs:205-211` |
| `api.ts::updateVaultEntry` `kind: update.kind ?? null` untested | Gap confirmed — `api.test.ts` has zero `updateVaultEntry` occurrences. See **E2**. | `grep updateVaultEntry apps/desktop/src/api.test.ts` is empty |
| ASCII-space-only trimming on the TS mirrors and Rust, largely untested | Implementation consistent on all three sides (`trim_matches(' ')` / `/^ +\| +$/g`); pinned only on `classify_filename` (two tab cases). See **E4**. | `parse.rs:83`, `paths.rs:314`, `fileName.ts:22`, `meetingName.ts:32` |
| NUL-byte `<select>` sentinels in `MeetingEditor.tsx` | Pre-existing, out of scope, and intact: 2 NUL bytes before and after the change. No action here. | `tr -cd '\000' \| wc -c` = 2 on base and on the working copy |
| T5 harness fix (`FakeService::with_llm_model_absent()`) | Test right. The drop chain legitimately queues `summarize` when an LLM is present, and the rename guard must refuse then; using the LLM-absent fake tests naming, not the chain. Guard unchanged in the diff. | `tests/meeting_type.rs:40-46`; the `jobs.rs` diff touches only `ingest_message` |

## Findings

### E1 [minor] [correctness] [status: fixed]

- **Where**: `CLAUDE.md:69`; `apps/desktop/src-tauri/src/commands/meetings.rs:340-342`
- **Spec ref**: FR-7 c4 (CLAUDE.md states the convention); FR-3 (folder name `<YYMMDD> - <Name>[ - <Type>]`)
- **Expected**: The folder name has at most three sections; the type is its **third** section (`types.ts:283` says so correctly).
- **Actual**: CLAUDE.md reads "The type persists as the meeting folder's own **fourth** section (`<YYMMDD> - <Name>[ - <Type>]`)" and the handler doc reads "a type persists as the folder name's **fourth** section (`<date> - <title> - <type>`)". Both ordinals are wrong — the fourth section is a *file*-name concept. Encoding, layout table and the rest of the paragraph are correct (UTF-8, no mojibake, the layout table is byte-identical to base).
- **Suggested fix**: "fourth" → "third" in both places.

### E2 [minor] [improvement] [status: fixed]

- **Where**: `apps/desktop/src/api.ts:97`; `apps/desktop/src/api.test.ts` (no `updateVaultEntry` case)
- **Spec ref**: FR-8 ("`api.updateVaultEntry` sends `kind: update.kind ?? null`"); T7 Done-when
- **Expected**: The one line that turns an absent TS `kind` into the explicit `null` the Rust `Option<String>` expects is pinned by a test — it is the seam that makes "clear the Type field" strip the type rather than read as "unchanged".
- **Actual**: No test anywhere exercises it; T7's Done-when named a file with zero coverage of this function, so its green run was vacuous. The Rust harness and the editor tests stop on either side of the `?? null`.
- **Suggested fix**: Two cases in `api.test.ts` in the file's existing style: `updateVaultEntry(id, { project, date, title, kind: "Retro" })` → the invoke payload contains `kind: "Retro"`; the same call without `kind` → payload contains `kind: null`.

### E3 [minor] [improvement] [status: fixed]

- **Where**: `crates/vault/src/paths.rs:339` (`unsorted_folder_name`) together with `apps/desktop/src/lib/meetingName.ts:46`
- **Spec ref**: FR-3 c4 (unsorted stem kept verbatim, hyphens included) combined with FR-8 c2 (three folder sections read as typed); FR-9 lists only *legacy* folders
- **Expected**: The blueprint documents which existing folders change meaning (FR-9) and accepts that ambiguity. It does not mention that the same ambiguity now applies to every *new* unsorted ingest whose stem contains exactly one hyphen.
- **Actual**: A `MissingSeparator` drop such as `ELS - 260812.mp4` lands as `unsorted/260910 - ELS - 260812` and the UI now reads it as title `ELS`, type `260812`; `just one - separator.mp4` shows as title `just one` with a `separator` pill. Before this feature such folders displayed the whole stem as the title. The behaviour is fully consistent with the spec (the round trip is preserved, and the rename form seeds title/type so the operator can fix it), but it is an ongoing consequence rather than a one-time migration effect, and nothing records it.
- **Suggested fix**: Either accept explicitly (one line in the blueprint's Assumptions or `lib.rs` docs: "an unsorted folder whose stem holds one hyphen reads as typed in the UI"), or — if unwanted — have `unsorted_folder_name` neutralise hyphens in the stem. The latter changes FR-3 c4 and needs an operator decision; do not do it silently.

### E4 [minor] [improvement] [status: fixed]

- **Where**: `crates/vault/tests/paths.rs` (folder-name parser cases), `apps/desktop/src/lib/meetingName.test.ts`, `apps/desktop/src/lib/fileName.test.ts`
- **Spec ref**: FR-1 header rule (ASCII spaces only), FR-3 ("trim ASCII spaces"), test-wave note "untested on both sides"
- **Expected**: The deliberate "trim `' '` only, let a tab reach the section" rule is a cross-language invariant of the three mirror parsers; it is pinned on `classify_filename` (`a_tab_beside_the_type_separator…`, `a_tab_beside_the_date_separator…`) but on none of the other three.
- **Actual**: `parse_meeting_folder_name("260812 -\tTitle")`, `parseMeetingName("260812 -\tTitle")` and `parseFileName("ELS -\t260812 - T.mp4")` have no test; a later "helpful" switch to `.trim()` / `str::trim` on any one side would silently diverge from Rust. (`parseMeetingName("260812 - - K")` is likewise unpinned in TS; Rust has it.)
- **Suggested fix**: One tab case per file (folder parser → `None`/`null`, since the date section then starts with `\t`; file parser likewise), plus the `260812 - - K` case in `meetingName.test.ts`.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 grammar, order, trimming, `ParsedName.kind` | `crates/vault/src/parse.rs:74-136` | `tests/parse_filename.rs` (34 cases incl. order, tab, empty type, verbatim stem); `tests/acceptance.rs::fr02_fr03_pure_parser_splits_on_every_separator…`, `fr05_calendar_rejections…` | ✓ |
| FR-2 four variants, Display, `all()==13`, `validate_kind` | `error.rs:251-272,288-295,326-337,369-373`; `title.rs:75-99` | `tests/error_vocabulary.rs` (13 count, pairwise distinct, 5 Display cases, 8 `validate_kind` cases); `VaultError::all_kinds()==12` retained | ✓ |
| FR-3 typed folder name + inverse, re-exports, crate docs | `paths.rs:277-334`; `lib.rs:11-52,178`; `README.md:22-57` | `tests/paths.rs` (build, parse, compact, 6-row round trip, 7 non-parsing names, unsorted hyphens kept); `acceptance.rs::fr03_…reads_back_through_the_crate_root_api` | ✓ (tab-trim unpinned → E4) |
| FR-4 ingest lands typed folder; 5+ → unsorted; collisions | `ingest.rs:298-309` | `acceptance.rs::fr04_*` (4 cases, exact tree via `list_files`) | ✓ |
| FR-5 path cap incl. type, at ingest and rename | pre-existing `check_len` / `check_move_length` (no new code, as planned) | `tests/paths.rs::check_len_accepts_a_typed_destination_of_exactly_260…`, `…pushes_a_fitting_destination_past_the_cap…`; `acceptance.rs::fr05_a_type_that_overruns…creates_nothing`, `fr05_a_rename_whose_typed_target…moves_nothing` | ✓ |
| FR-6 rename edits/strips type, `ReservedSeparator` before any move | `manage.rs:76-87,197-208,222-225,237-238` | `acceptance.rs::fr06_*` (10 cases); inline `manage.rs` tests (trim, refusal creates no project dir) | ✓ |
| FR-7 IPC pass-through, unsorted reason in job message, CLAUDE.md | `commands.rs:1127-1129`; `commands/meetings.rs:357,378`; `jobs.rs:607,943-953`; `CLAUDE.md:69` | `apps/desktop/src-tauri/tests/meeting_type.rs` (8 cases); c4 by inspection | ✓ code; doc ordinal wrong → E1 |
| FR-8 TS mirrors, `MeetingUpdate.kind`, `api.ts`, editor, page pill, row tag | `fileName.ts:37-46`; `meetingName.ts:46-67`; `types.ts:283-287`; `api.ts:97`; `MeetingEditor.tsx` (Type field, guard, preview, payload); `RecordingPage.tsx:307`; `VaultRow.tsx:74-77` | `fileName.test.ts` (10), `meetingName.test.ts` (16), `MeetingType.test.tsx` (16); untouched `MeetingEditor.test.tsx` / `RecordingPage.test.tsx` / `VaultRow.test.tsx` still green | ✓ except `api.ts` → E2 |
| FR-9 no migration; consequences listed; probe re-run | — (documentation) | Probe re-run 2026-09-10 over `D:\SynologyDrive\PARA\03-Resources\Call Recordings`: 54 folders, exactly the six predicted (list below) | ✓ |
| FR-10 service untouched | — | `git diff --stat` touches nothing under `services/` | ✓ |
| NFR-1 purity, no panic, fuzz green | `parse.rs`, `paths.rs` (no fs, no clock) | `tests/parse_fuzz.rs` green unchanged (2 cases / 10,000 names) | ✓ |
| NFR-2 Windows in scope | — | all suites executed on Windows 11 for this report | ✓ |
| Out of scope respected | no `Classification::Sorted` / `VaultMeetingView` change; no picker/vocabulary; no service/MCP/index change; `VaultRow.module.css` adds only the `align-items: baseline` line the blueprint allowed | inspection | ✓ |

### FR-9 probe — actual list (2026-09-10, 54 meeting folders)

Now read as **typed**:

- `ELS/260903 - chat for sme-s and ground problems with badgerdoc` (title `chat for sme`, type `s and ground problems with badgerdoc`)
- `HUB/260828 - Правки по финансу с Лизой - CRM Idea` (type `CRM Idea`)
- `TBOT/260828 - Q&A Сессия с Дмитрием - поиск рабочего триггера` (type `поиск рабочего триггера`)

Now **non-conforming** (whole name shown, editor seeds an empty date):

- `TBOT/260529 - Запись встречи 29.05.2026 11-57-25 - запись`
- `TBOT/260731 - Запись встречи 31.07.2026 11-04-56 - запись`
- `TBOT/260731 - Запись встречи 31.07.2026 11-37-12 - запись`

Identical to the blueprint's prediction; no seventh folder appeared.

### Parser agreement (orchestrator question 2)

| Rule | `vault::classify_filename` | `vault::parse_meeting_folder_name` | `lib/fileName.ts` | `lib/meetingName.ts` |
|---|---|---|---|---|
| Split on every ASCII `-` | ✓ | ✓ | ✓ | ✓ |
| Trim ASCII `' '` only | ✓ (`trim_matches(' ')`) | ✓ | ✓ (`/^ +\| +$/g`) | ✓ |
| Accepted section counts | 3 / 4 | 2 / 3 | 3 / 4 | 2 / 3 |
| Empty section | validator rejection (`EmptyTitle` / `EmptyType`) | `None` | `null` | `null` |
| Date | six digits + calendar | six ASCII digits | `/^\d{6}$/` | `/^\d{6}$/` |
| Character rules | full (`title::validate`) | none (display-only, documented) | none (documented) | none (documented) |

The only asymmetry is the intended one: the TS mirrors and the folder-name reader do not re-validate characters, so a name Rust would route to `unsorted` for `IllegalTypeCharacter` still "parses" for display — the pre-existing design for the title, extended consistently to the type. Round trip `meeting_folder_name` → `parse_meeting_folder_name`: the rename guard and the parser's own split guarantee no `-` in title/type; `title::validate` trims leading whitespace and trailing `.`/` `, so the built name has no trimmable edges to lose. Cyrillic, `&`, `.`, inner spaces survive (pinned by the six-row round-trip table).

### Security / performance

Nothing new to report. The only new IPC input, `kind`, is trimmed and validated in `vault` (illegal chars including `/` `\` `:`, control chars, reserved device names; `..` trims to empty → `EmptyType`) and always lands as a suffix of a date-prefixed component under `parent.join(..)`, so no traversal is possible. No shell, no deserialization, no secrets. The `split('-')` allocation is negligible.

### Housekeeping note (not a finding)

`crates/vault/README.md`, `crates/vault/src/ingest.rs` and `crates/vault/src/title.rs` are wholly CRLF in the working tree (`git ls-files --eol`: `i/lf w/crlf`, attribute `text=auto eol=lf`). Git normalises them to LF at commit, so the repository content is unaffected, and rustfmt's `Auto` newline style tolerates it. Mentioned only so nobody mistakes a future whole-file diff for a real change.

## Positive notes

- The `ReservedSeparator` guard runs before `ensure_project_dir`, and an inline test pins that a refused rename creates no project folder — better than the blueprint asked for. Keep it there.
- `jobs.rs::ingest_message` reuses the `Rejection` `Display` verbatim and joins it with the collision note rather than replacing one with the other — the right shape for the pre-existing "unsorted reason never surfaced" gap.
- `Classification::Sorted` and `VaultMeetingView` were left unchanged exactly as decided; the type lives in one place (the folder name) with one writer and one reader per language.
- Tests are behaviour-first throughout: exact on-disk trees via `list_files`, hardcoded expected strings in the round-trip table (not recomputed), `FakeService` as the only double and only for the out-of-process service, `getByLabelText` / `getByRole` queries in the React tests, and the editor payload asserted with `toStrictEqual` so the "no `kind` key when blank" contract is real.
- `parse_meeting_folder_name` is documented as display-side (six-digit check, no calendar validation) with `date::validate` named as the authority — the split of responsibilities is explicit, not accidental.
- The MeetingEditor hint is a single shared `<p>` bound through `aria-describedby` to whichever field is invalid; the NUL sentinels survived the edit.
