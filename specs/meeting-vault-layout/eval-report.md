---
slug: meeting-vault-layout
base_ref: 6d0fce75f5cc49a0b46c6eb6c052d4029ab06f7d
round: 2
---

# Evaluation report: Meeting vault layout and naming convention

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 2 | 0 |
| minor | 2 | 2 | 3 |

**Round 2.** Both majors are genuinely fixed, verified independently rather than taken on the fix note's word. E1: replaying the crate's own generator against the crate's own public parser now puts **6,942 of 10,000** cases past the extension gate (round 1 measured 0/10,000), of which 3,064 reach project-code validation. E2: the smoke consumer, run the same way as in round 1, now prints plain `C:\...` paths for `meeting_dir` and `source_path`, and a regression test asserts the absence of the `\\?\` prefix for the root and for both sorted and unsorted results. E5 and E8 are fixed as described, with no stale references left behind. The full QA gate is clean at the current tree: 144 tests across 14 integration targets plus the doc-test, 0 failures; `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo doc --no-deps` all pass.

Two minors are open rather than accepted. **E4 is reopened**: its deferral rationale ("E2's fix removes the masking … so this gap is no longer hidden") is factually wrong. Rust's `std::fs` re-applies the extended-length prefix internally for over-long paths, so an emoji-heavy stem still produces a real directory at 309 UTF-16 units while `check_len` reports 178 — and this round the evaluator drove that all the way to the consumer: **Python could not write `transcript.json` into the returned `meeting_dir`** (`FileNotFoundError`), which is precisely the F3→F2 handoff FR-13 exists to guarantee. It stays *minor* only because the input class is narrow (a realistic 15-character vault root needs a title of 107+ characters of which 60+ are non-BMP). **E9 is new**, a residual of E1's fix: the sweep now reaches `code::validate` but still reaches `date::validate` and `title::validate` in **zero** of 10,000 cases, so the round-1 fix note's claim that it reaches "date and title validation" overstates what was achieved. E3, E6 and E7 remain accepted deferrals; their rationales are sound and the code is unchanged. No regressions were introduced by the fix pass.

## Findings

### E1 [major] [correctness] [status: fixed]

- **Where**: `crates/vault/tests/parse_fuzz.rs:55-106` (`EXTENSIONS`, `random_filename`), `:108-130`
- **Spec ref**: NFR-3 acceptance — "A property/fuzz-style test over randomized filenames produces no panic across at least 10k cases"; plan T8
- **Expected**: 10,000 randomized filenames actually exercise `classify_filename`'s separator split and the validators behind it.
- **Actual (round 1)**: `ALPHABET` could not spell any of the ten allowed extensions, so all 10,000 cases died in `media::from_file_name`. Measured 0/10,000 past the extension gate.
- **Fix applied**: `random_filename` appends a real allowed extension to 70% of generated names, and `ten_thousand_random_filenames_never_panic` now counts cases returning anything other than `Err(UnsupportedMediaType)` and asserts `reached_parser > 5_000`.
- **Round-2 verification**: confirmed independently. A scratch consumer crate (path-dependency on `vault`, replaying the same seed and the same generator against the crate's public `classify_filename`) measures:

  ```
  stopped at extension gate: 3058
  reached parser:            6942
    MissingSeparator:        3878
    reached code validation: 3064   (EmptyProjectCode 213, InvalidProjectCode 2851)
  ```

  The sweep is no longer vacuous, and the assertion in the test is a real guard against the regression recurring. **Fix confirmed.** See E9 for the residual.

### E2 [major] [correctness] [status: fixed]

- **Where**: `crates/vault/src/paths.rs:125-147` (`simplify_extended_prefix`, `is_plain_drive_path`), `crates/vault/src/ingest.rs:49,126,192`, `crates/vault/tests/ingest.rs:477-527`
- **Spec ref**: FR-13 — "the absolute meeting-folder path … F3 passes the meeting-folder path straight to F2"
- **Actual (round 1)**: `Vault::root()`, `Ingested::meeting_dir` and `Ingested::source_path` all carried the Windows extended-length `\\?\` prefix, unusable by a large fraction of downstream tooling and unpresentable in a UI. No test could catch it because every assertion compared against an equally-prefixed expected path.
- **Fix applied**: `paths::simplify_extended_prefix` (crate-private) strips `\\?\` when the remainder is a plain `<drive>:\...` path and leaves anything else — notably `\\?\UNC\...` — untouched. Applied at exactly three points: the canonicalized root in `Vault::open`, the resolved project directory in `Vault::ensure_project_dir`, and the checked destination before `paths::check_len` in `place_and_transfer`.
- **Round-2 verification**: confirmed on both axes.
  1. *Smoke consumer, re-run the same way as round 1* (`cargo run --example f3_consumer` against a scratch vault, one sorted / one unsorted / one `.txt`):

     ```
     meeting_dir: C:\Users\...\vaulteval2\root\ELS\260812 - Security issue
     source_path: C:\Users\...\vaulteval2\root\ELS\260812 - Security issue\source.mp4
     ```

     Plain form on the sorted, unsorted and duplicate paths; `.txt` still exits 1 with the message on stderr and nothing written into the vault beyond `unsorted/` (which `Vault::open` creates per FR-1).
  2. *Regression test*: `tests/ingest.rs::fr13_returned_paths_are_plain_and_never_carry_the_extended_length_prefix` asserts `!…starts_with(r"\\?\")` for `Vault::root()` and for the `meeting_dir`/`source_path` of both a sorted and an unsorted ingest. It fails if the prefix returns.

  The `check_len` adjustment is also correct and not a regression: creation now goes through the plain paths, so measuring the plain form is the right budget. The `DuplicateRedrop` and `SuffixedFolder` results derive from the same already-simplified `parent_dir`, so they are covered by construction even though the test does not assert on them. **Fix confirmed.**

### E3 [minor] [security] [status: accepted]

- **Where**: `crates/vault/src/paths.rs:80-102` (`contained_child`), `crates/vault/src/layout.rs:74-97` (`ensure_project_dir`)
- **Spec ref**: FR-14 — "Every computed destination path is verified to be contained within the vault root **after normalization**"
- **Expected**: The destination is normalized and re-checked, not just the root.
- **Actual**: Only `root` is canonicalized; the joined destination is checked purely lexically (`starts_with` on the string) and never re-normalized. `ensure_project_dir` reuses whatever entry under the root matches the code case-insensitively and passes `file_type().is_dir()` — a directory **junction** or symlink satisfies that. If `<root>\ELS` is a junction to `C:\Elsewhere`, `contained_child` returns a path that lexically starts with the root and `fs::create_dir_all` then writes the meeting folder and `source.mp4` outside the vault. The exploit requires prior write access to the vault root (or an operator who set up the junction deliberately), so the practical risk is low, but the spec's stated guarantee is not the one implemented.
- **Suggested fix**: After `ensure_project_dir` returns, `canonicalize()` the resolved parent and re-assert `starts_with(&self.root)`, returning `PathEscapesVault` otherwise.
- **Disposition: accepted, not fixed** (round 1, carried forward). Fixing it correctly means a second FR-14 enforcement pass over every directory resolution (`ensure_project_dir`, and `resolve_meeting_dir`'s suffix probing), beyond the round's scope. Round-2 re-check: code unchanged, rationale still sound, no new exposure introduced by the fix pass.

### E4 [minor] [correctness] [status: open — reopened]

- **Where**: `crates/vault/src/paths.rs:165-179` (`check_len`), `:37` (`MAX_UNSORTED_STEM_CHARS`), `:211-226`
- **Spec ref**: NFR-4 — "Total destination path length is checked against the Windows 260-character limit before writing; an ingest that would exceed it fails with a distinct, actionable error rather than a raw OS error"; FR-13
- **Expected**: A measurement in the same units Windows uses — UTF-16 code units.
- **Actual**: `check_len` counts `chars()`. A non-BMP character (emoji) is one `char` but two UTF-16 units, and the unsorted stem is likewise capped at 120 **chars**. Round 1 filed this as latent, masked by E2. **The round-1 disposition — "E2's fix removes the masking … so this gap is no longer hidden" — is factually incorrect.** Rust's `std::fs` internally re-applies the extended-length prefix for paths over `MAX_PATH`, so the over-long directory is still created successfully; the harm simply moved from "the directory is created at a length Explorer cannot open" to "an over-long path is handed back to the caller as `Ingested::meeting_dir`, silently".

  Reproduced end-to-end this round through the public API only (short vault root `C:\Users\…\Temp\e4c`, dropped file of 120 emoji + `.mp4`):

  ```
  ingest: OK
    meeting_dir chars=178  utf16=298     <- check_len measured 178, passed
    C:\Users\<user>\AppData\Local\Temp\e4c\r\unsorted\260821 - 😀…😀
  ```

  Then, standing in for F2:

  ```
  F2 write of transcript.json into the returned meeting_dir:
    FAILED: FileNotFoundError [Errno 2]   (real Win32 length 309 UTF-16 units)
  ```

  So the library reports success and hands F3 a meeting folder that F2 cannot write into — the exact handoff FR-13 defines — and the folder cannot be removed by ordinary tooling either (`rm -rf` fails; only a `\\?\`-prefixed delete works). NFR-4's promise of "a distinct, actionable error" is not kept for this input class.

  A second, smaller instance of the same gap: `check_len` runs on the *fresh* base name, but `layout::resolve_meeting_dir` may append ` (2)`…` (999)` afterwards, adding 4-6 unmeasured characters to the path that is actually created.

- **Severity**: kept **minor**, not raised. With a realistic vault root (`D:\MeetingVault`, 15 chars) the budget is 213 characters and overflow needs a title of 107+ characters of which 60+ are non-BMP. Real, demonstrated, but not a shape an operator is likely to produce.
- **Suggested fix**: Measure with `full_destination.as_os_str().encode_wide().count()` (or `to_string_lossy().encode_utf16().count()`), cap `MAX_UNSORTED_STEM_CHARS` in the same units, and re-run `check_len` (or budget for the maximum suffix) after `resolve_meeting_dir` picks a suffixed name. Re-verify the existing 259/261-character boundary tests in the new units.

### E5 [minor] [spec-drift] [status: fixed]

- **Where**: `crates/vault/src/error.rs:172-178` (the `Rejection` doc comment), `crates/vault/tests/error_vocabulary.rs:26-35`
- **Spec ref**: FR-14 acceptance; NFR-5
- **Actual (round 1)**: `Rejection::TitleEscapesVault` was declared, documented, included in `Rejection::all()` and re-exported, but never constructed anywhere — dead surface F3 could never observe.
- **Fix applied**: The variant was deleted (rather than made reachable), with a doc comment on `Rejection` explaining why no such variant exists: `title::validate` already rejects `/` and `\` as `IllegalTitleCharacter` and collapses a `.`/`..` title to `EmptyTitle` after the trailing-dot trim, so containment logic is unreachable from a title.
- **Round-2 verification**: the rationale checks out against `src/title.rs:51-73` — `first_illegal_char` runs on the raw string before trimming, and `trim_end_matches(['.', ' '])` turns `..` into `""`. `Rejection::all()` is now 9 entries, `error_vocabulary.rs` asserts 9, `cargo doc --no-deps` is clean, and a repo-wide grep finds no stale reference to the removed variant outside the explanatory comment. **Fix confirmed.**
- **Residual, not reopened**: FR-14's acceptance wording ("all reject with a **containment error**") is still not delivered literally — `ELS - 260812 - ..\..\evil.mp4` reports `IllegalTitleCharacter('\\')` and routes to unsorted. That is the *more* specific reason NFR-5 asks for, the outcome is materially safe (asserted in `tests/acceptance.rs:494`), and round 1 explicitly offered "delete the variant" as an acceptable fix. Recorded here so the deviation from the spec's wording stays visible.

### E6 [minor] [security] [status: accepted]

- **Where**: `crates/vault/src/transfer.rs:31,64,77,103`; `crates/vault/src/layout.rs:50,74,106`
- **Spec ref**: plan "Public API surface" (T6/T9 specify `pub(crate)`); `cli` profile — "For a library: every public function is an attack surface"
- **Actual**: `transfer_into_place`, `copy_verify_delete`, `copy_verify_delete_expecting`, `same_recording`, `ensure_project_dir` and `resolve_meeting_dir` are all `pub` and reachable through the `pub mod transfer` / `pub mod layout` declarations in `lib.rs`, where the plan specified crate-private. Two are unsafe for an arbitrary caller in ways `ingest` currently prevents by construction: `transfer_into_place` calls `fs::rename`, which on Windows **silently replaces an existing destination file** — the one thing FR-11 says the library never does — and `copy_verify_delete_expecting` accepts a caller-supplied expected size and deletes the original when it matches.
- **Suggested fix**: Make the six items `pub(crate)` and move their tests into in-module `#[cfg(test)]` blocks; at minimum mark `copy_verify_delete_expecting` `#[doc(hidden)]` and document `transfer_into_place`'s overwrite behaviour on its own doc comment.
- **Disposition: accepted, not fixed** (round 1, carried forward). Narrowing these requires relocating `tests/transfer.rs` and `tests/layout.rs` coverage into in-module blocks, disproportionate to the round's scope. Round-2 note: the fix pass demonstrated the alternative pattern in `paths.rs` — `simplify_extended_prefix` is correctly `pub(crate)` and tested indirectly through `ingest.rs` — so the precedent for narrowing now exists in-tree.

### E7 [minor] [correctness] [status: accepted]

- **Where**: `crates/vault/src/layout.rs:150-169` (`probe`)
- **Spec ref**: FR-11
- **Actual**: `probe` returns `Occupied` for **any** existing directory that does not contain a matching `source.<ext>` — including one that is empty, or one that holds only `transcript.json` because the operator deleted the source by hand. The re-drop then lands in `<name> (2)` and the earlier transcript is orphaned in a folder with no recording. Within FR-11's letter, but the one case where the policy fragments a meeting rather than protecting one.
- **Suggested fix**: Treat a candidate directory containing no `source.*` at all as free (reuse it), or at least document the behaviour so F3 can explain the `(2)` folder to the operator.
- **Disposition: accepted, not fixed** (round 1, carried forward). A product decision needing its own acceptance criterion, not a drive-by tweak. Round-2 re-check: code unchanged.

### E8 [minor] [correctness] [status: fixed]

- **Where**: `crates/vault/src/error.rs:258-303` (`#[cfg(test)] mod exhaustiveness`), `crates/vault/tests/error_vocabulary.rs:28-34,58-61`
- **Spec ref**: NFR-5; plan T1 — "a `Rejection::ALL` const slice **covers every variant**"
- **Actual (round 1)**: `Rejection::all()` / `VaultError::all_kinds()` were hand-written `Vec`s with hardcoded length assertions; adding a variant without updating `all()` left both tests green while silently dropping the new variant from the pairwise-distinct-`Display` guarantee.
- **Fix applied**: `#[cfg(test)] mod exhaustiveness` with two never-called functions that `match` every variant of `Rejection` / `VaultError` with no wildcard arm, plus comments in `error_vocabulary.rs` pointing at it as the real enforcement mechanism.
- **Round-2 verification**: the guard is compiled — it sits under `#[cfg(test)]` in the lib target, which `cargo test` builds (the `unittests src\lib.rs` target runs 0 tests but compiles) and which `cargo clippy --all-targets` also builds. Both are in the QA gate, so adding a variant without an arm fails the build at the enum change site. This is exactly the fix round 1 suggested. **Fix confirmed.** (It enforces the guard match, not `all()` itself, so it is a forcing function rather than a proof — an acceptable and standard trade.)

### E9 [minor] [correctness] [status: open]

- **Where**: `crates/vault/tests/parse_fuzz.rs:62-74` (`random_char`), `:108-130`
- **Spec ref**: NFR-3 — "No input … causes a panic or an unhandled error; every path returns a typed rejection"
- **Expected**: The 10k sweep exercises the validators behind `classify_filename`, including the date and title rules.
- **Actual**: A residual of E1's fix. Replaying the generator and bucketing the outcomes gives, over 10,000 cases: 3,878 `MissingSeparator` (stops before any validator), 3,064 reaching `code::validate` — and **0** reaching `date::validate` or `title::validate`, because every one of the 3,064 dies at the project code. The cause is `random_char`: only 2 of its 10 branches draw from `ALPHABET`, the other 8 emit control characters, emoji or RTL marks, so a 2-to-10-character first segment matching `^[A-Z][A-Z0-9]{1,9}$` is astronomically unlikely. Not one case produced `DateNotSixDigits`, `DateNotACalendarDate`, `EmptyTitle`, `IllegalTitleCharacter` or `ReservedDeviceName`. E1's fix note claims the sweep reaches "the separator split, project-code, date and title validation" — the last two are not reached.
- **Impact is limited, hence minor**: `date::validate` and `title::validate` each carry dedicated NFR-3 unit coverage (`tests/date.rs`'s Arabic-Indic digit case, `tests/title.rs::nfr3_unusual_unicode_is_accepted_without_panic` with emoji, an RTL mark and a 30,000-char title), so the un-swept code is not untested — it is only untested *by the fuzz sweep the acceptance criterion names*.
- **Suggested fix**: Prefix a valid-looking `"<CODE> - "` (and sometimes a six-digit-looking group) to a fraction of generated names, then extend the assertion from a single `reached_parser` count to per-stage counters — e.g. assert that at least a few hundred cases reach the title validator. Correct the file's module doc, which still claims the appended extension is sufficient to exercise the whole parser.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (init, idempotent, file-root error) | `src/layout.rs:50-63`, `src/ingest.rs:40-51` | `tests/layout.rs::init_on_empty_directory_creates_unsorted`, `::init_is_idempotent_and_leaves_existing_children_untouched`, `::init_on_file_path_returns_typed_error_not_panic`, `tests/acceptance.rs::fr01_…` | ✓ |
| FR-2 (pure parser) | `src/parse.rs:62-107` | `tests/parse_filename.rs::parser_needs_no_fixture_on_disk`, `tests/acceptance.rs::fr02_fr03_…` | ✓ |
| FR-3 (first-two-separator split) | `src/parse.rs:69-83` | `tests/parse_filename.rs::title_may_itself_contain_the_separator`, `::missing_separators_are_unsorted`, `::exactly_one_separator_is_unsorted` | ✓ |
| FR-4 (code pattern, R4 uppercase-only) | `src/code.rs:40-77` | `tests/code.rs` (9 cases), `tests/parse_filename.rs::lowercase_project_code_is_unsorted`, `tests/acceptance.rs::fr04_…` | ✓ |
| FR-5 (calendar date, verbatim) | `src/date.rs:38-56` | `tests/date.rs` (8 cases), `tests/acceptance.rs::fr05_calendar_rejections_and_verbatim_date_in_the_folder_name`, `::fr05_260229_is_rejected_2026_is_not_a_leap_year` | ✓ (R3 deviation, plan-approved, explicitly recorded) |
| FR-6 (title rules) | `src/title.rs:51-98` | `tests/title.rs` (13 cases), `tests/acceptance.rs::fr06_…` | ✓ |
| FR-7 (media allowlist, error not unsorted) | `src/media.rs:37-51`, `src/parse.rs:63` | `tests/media.rs` (11 cases), `tests/ingest.rs::fr07_…`, `tests/acceptance.rs::fr07_…` | ✓ |
| FR-8 (exact sorted destination) | `src/paths.rs:187-189`, `src/ingest.rs:99-174` | `tests/ingest.rs::fr08_sorted_ingest_creates_exact_destination_and_nothing_else`, `tests/acceptance.rs::fr08_fr09_…` | ✓ |
| FR-9 (case-insensitive project reuse) | `src/layout.rs:74-97`, `src/ingest.rs:180-213` | `tests/layout.rs::ensure_project_dir_reuses_existing_case_insensitive_folder`, `::…never_renames_what_it_finds`, `tests/ingest.rs::fr09_…` | ✓ |
| FR-10 (unsorted layout, date-added prefix) | `src/paths.rs:194-226`, `src/ingest.rs:302-308` | `tests/ingest.rs::fr10_unsorted_lands_under_unsorted_with_injected_date_and_is_writable`, `::fr10_two_unsorted_files_are_distinguishable_by_injected_date`, `tests/paths.rs` (5 shaper cases) | ✓ |
| FR-11 (dedupe-or-suffix, never overwrite) | `src/layout.rs:106-181`, `src/transfer.rs:103-107` | `tests/layout.rs` (6 cases incl. the 999-suffix limit), `tests/ingest.rs::fr11_duplicate_redrop_is_a_noop`, `::fr11_different_file_same_name_gets_suffixed_folder`, `tests/acceptance.rs::fr11_…` | ✓ (see E6, E7) |
| FR-12 (copy/rename-verify-delete, rollback) | `src/transfer.rs:31-97`, `src/ingest.rs:339-363` | `tests/transfer.rs` (9 cases), `tests/ingest.rs::fr12_failed_transfer_removes_only_the_meeting_directory_it_created`, `tests/acceptance.rs::fr12_…` | ✓ |
| FR-13 (result shape, usable by F2/F3) | `src/ingest.rs:216-232`, `src/paths.rs:125-147` | `tests/ingest.rs::fr13_result_fields_are_absolute_and_populated_correctly`, `::fr13_returned_paths_are_plain_and_never_carry_the_extended_length_prefix`, `tests/acceptance.rs::fr13_…`, plus an out-of-band smoke run of `examples/f3_consumer.rs` | ✓ (E2 fixed; E4's over-long case still returns a path F2 cannot open) |
| FR-14 (containment before any write) | `src/paths.rs:80-102`, `src/ingest.rs:114-128` | `tests/paths.rs::escaping_component_combinations_are_all_rejected`, `::no_rejecting_call_creates_anything_on_disk`, `tests/ingest.rs::fr14_…`, `::nfr4_path_too_long_creates_nothing_before_any_directory` | gap — E3 (lexical only, no post-normalization re-check) |
| FR-15 (reserved names) | `src/code.rs:41-43`, `src/paths.rs:46-58` | `tests/code.rs::rejects_reserved_word_case_insensitively`, `tests/paths.rs::reserved_names_are_exported_and_distinct`, `tests/ingest.rs::fr15_…`, `tests/acceptance.rs::fr15_…` | ✓ |
| FR-16 (app-data dir distinct from vault) | `src/appdata.rs:29-53` | `tests/appdata.rs` (5 cases) | ✓ |
| NFR-1 (<1 ms parse, 4096 chars) | `src/parse.rs` (allocation-light, no I/O) | `tests/parse_filename.rs::parsing_a_long_filename_is_fast` | ✓ |
| NFR-2 (<500 ms same-volume via rename) | `src/transfer.rs:34-37` | `tests/transfer.rs::same_volume_transfer_of_8mib_completes_under_500ms` | ✓ |
| NFR-3 (no panic on any input) | `src/parse.rs`, `src/title.rs`, `src/paths.rs:211-226` | `tests/parse_fuzz.rs` (6,942/10,000 now reach the parser), `tests/title.rs::nfr3_…`, `tests/date.rs`, `tests/acceptance.rs::nfr3_…` | ✓ (E1 fixed; E9 residual — date/title validators still unreached by the sweep) |
| NFR-4 (260-char cap) | `src/paths.rs:165-179` | `tests/paths.rs::check_len_rejects_a_destination_over_260_characters`, `::check_len_accepts_a_259_character_destination`, `tests/ingest.rs::nfr4_…`, `tests/acceptance.rs::nfr4_…` | gap — E4 (`char` vs UTF-16 units; reopened, now demonstrated end-to-end) |
| NFR-5 (distinct enumerated reasons) | `src/error.rs:99-303` | `tests/error_vocabulary.rs` (10 cases) + the compile-time `exhaustiveness` guard | ✓ (E5, E8 fixed) |

**QA gate at this tree** (run by the evaluator from `crates/vault/`): `cargo test` — 144 tests over 14 integration targets plus 1 doc-test, 0 failures; `cargo fmt --check` — clean; `cargo clippy --all-targets -- -D warnings` — clean; `cargo doc --no-deps` — clean.

Out-of-scope check (re-run): nothing in the diff writes `summary.md`, parses transcripts, adds UI/Tauri code, creates an installer, or introduces a `Makefile`. No gold-plating. Dependencies remain exactly `chrono` plus dev-dependency `tempfile`, as T1 fixed. The only tracked-file change outside `crates/` is the task-checkbox flip in `plan.md`.

## Positive notes

- **The fix pass did not widen its blast radius.** E2 touched three call sites and added one crate-private helper; E8 added a `#[cfg(test)]` module; E5 deleted a variant and one assertion constant. Nothing else in `src/` moved, and all four deferred findings were left visibly deferred with reasons rather than quietly half-patched.
- **`simplify_extended_prefix` refuses to guess.** It strips the prefix only when the remainder is a plain `<drive>:\` path and returns an extended UNC path untouched, with a doc comment saying why. The tempting version — an unconditional `strip_prefix` — would have silently mangled `\\?\UNC\server\share\…` into a broken path.
- **E5's fix chose the honest option.** Making `TitleEscapesVault` "reachable" would have required inventing a code path; deleting it and documenting at the enum why no such variant exists leaves the next maintainer with the reasoning instead of the puzzle.
- **The plan's two flagged spec defects are still handled honestly.** `tests/acceptance.rs::fr05_260229_is_rejected_2026_is_not_a_leap_year` carries the explanation of why the implementation contradicts the spec's own acceptance bullet, and `src/code.rs:35-39` documents R4's resolution at the point of decision.
- **The ordering FR-14 demands is genuinely enforced and genuinely tested.** `ingest.rs:125-128` runs the joint two-component containment check and the length cap before any `create_dir`, and `nfr4_path_too_long_creates_nothing_before_any_directory` proves it by observing that no project directory appears.
- **The rollback distinguishes "created by this call" from "already existed".** `Vault::ensure_project_dir` probes for the project folder *before* creating it and only registers it for unwind when it was absent, and `Rollback::unwind` reverses insertion order so the nested meeting folder goes first.
- **The FR-12 failure test is a real failure, not a mock.** `DenyFileCreationGuard` uses `icacls` to deny file-creation (but not subdirectory-creation) rights with inheritance, and restores permissions on drop so `tempfile` cleanup still works.
- **R7's hidden assumption is asserted, not assumed.** `tests/transfer.rs::successful_transfer_preserves_mtime` makes the size+mtime dedupe fail loudly at build time if mtime preservation ever regresses.
- **`resolve_meeting_dir` creates nothing**, keeping directory creation and its rollback in a single owner — locked down by `tests/layout.rs::resolve_meeting_dir_never_writes_anything`.
