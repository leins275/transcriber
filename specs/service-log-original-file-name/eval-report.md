---
slug: service-log-original-file-name
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Service log shows the original file name

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 0 | 0 | 0 |

The diff implements the spec fully and stays inside its boundaries. The write path records the dropped file's base name only in the `PendingWork::Ingest` arm (captured before the vault rename), the `Filed` arm submits `None`, and the `meeting` key is skipped entirely when absent — the FR-5 wire body is byte-identical to the pre-feature one, pinned by an exact `body_json` wiremock match. The read path parses `meeting_json` exactly once in `http.rs` with a deliberately total parser (non-JSON, `{}`, non-string, empty string, non-object, and `null` all yield `None` — six-variant test), so the panel stays presentational. The FR-3 fallback handles both separators, requires a real parent folder, and leaves non-`source.<ext>` base names (LLM/derived rows) untouched. The diff under `services/transcription/` is empty, `SCHEMA_VERSION` is still 2, and all suites pass as run by this evaluation: cargo test (219 + 8 e2e), Vitest (274), pytest (green, 2 skips), clippy `-D warnings`, eslint, tsc — all clean.

## Findings

None.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (record original name at submit) | `apps/desktop/src-tauri/src/jobs.rs:431` (capture before ingest), `service/http.rs:132-147` (`SubmitBody.meeting` / `SubmitMeeting`), `http.rs:411-417` | `http.rs::submit_posts_exact_body_keys_and_returns_job_id_from_202` (exact body incl. `meeting`); `jobs.rs::a_dropped_recording_is_submitted_with_its_original_file_name`; live wire check (`compare.js` — live `meeting_json` byte-identical to fixture) | ✓ |
| FR-2 (render recorded name; parse once in Rust) | `http.rs::original_file_name_from` + `From<LedgerJobResponse>`, `service/mod.rs::LedgerJob`, `commands/ledger.rs::LedgerJobView`, `LedgerPanel.tsx::displayNameOf` step 1 | `http.rs::list_ledger_jobs_reads_the_original_file_name_out_of_meeting_json`; `ledger.rs::the_view_carries_the_recorded_original_file_name` + snake_case serialization test; Vitest "shows the recorded original file name instead of source.<ext>" | ✓ |
| FR-3 (derived fallback for rows without a name) | `LedgerPanel.tsx:38-65` (`SOURCE_BASE_NAME`, `displayNameOf` steps 2-3) | Vitest: backslash fallback, slash fallback, non-`source.<ext>` base unchanged, no-throw batch (`null` / bare `source.mp4` / empty path) | ✓ |
| FR-4 (tooltip = full `source_path`; other fields unchanged) | `LedgerPanel.tsx:142` (title unchanged); `ledger.rs` additive field | Vitest "keeps the full source path in the row's tooltip" (both branches); `ledger.rs::every_pre_existing_field_survives_the_conversion_unchanged` | ✓ |
| FR-5 (retranscribe never invents a name) | `jobs.rs:474-478` (`Filed` arm → `None`); `http.rs` skip-serialize | `jobs.rs::a_retranscribe_of_a_filed_recording_submits_no_original_file_name`; `http.rs::submit_omits_the_meeting_key_entirely_when_no_original_file_name_is_known` (exact body, no `meeting` key) | ✓ |
| FR-6 (no schema migration; absent key = absent value) | zero diff under `services/transcription/` (verified: `git diff <base> --stat`); `SCHEMA_VERSION = 2` (`ledger.py:24`); `#[serde(default)] meeting_json` (`http.rs:278`) | pytest suite green with zero modifications (run by evaluator); `http.rs::list_ledger_jobs_a_row_without_a_meeting_json_key_still_decodes` | ✓ |
| NFR-1 (wire compat both ways) | skip-serialize on write; `#[serde(default)]` on read | FR-5 exact-body test (old-service-compatible body); FR-6 absent-key decode test | ✓ |
| NFR-2 (malformed `meeting_json` never breaks the panel) | `http.rs::original_file_name_from` (total function) | `http.rs::list_ledger_jobs_a_malformed_meeting_json_yields_no_name_rather_than_an_error` (6 variants incl. `null` column); Vitest no-throw fallback cases | ✓ |
| NFR-3 (Windows `\` paths, `/` still works) | `displayNameOf` splits on `/[\\/]/`; vault ext is normalized lowercase so the `source.` literal match is safe | Vitest `C:\...` fixture and `/home/...` fixture | ✓ |

Cross-cutting acceptance: live app check (T4) evidenced in the scratchpad — `compare.js` proves the live ledger row's `meeting_json` string is byte-identical to the wiremock fixture, so the test fixtures faithfully model the real wire (the Python `json.dumps` `": "` separator is reproduced in the fixture string).

## Positive notes

- The FR-5 wire test uses an exact `body_json` match with **no** `meeting` key, which pins byte-identical backward compatibility rather than merely "meeting is null" — the strongest possible form of the requirement.
- The malformed-`meeting_json` test covers two cases beyond the spec's list (empty-string name, JSON array) plus a `null` column, all within NFR-2's intent; the parser rejects empty strings so the UI truthiness check can never show a blank label.
- `ledger.rs::every_pre_existing_field_survives_the_conversion_unchanged` uses a fully-populated fixture so "copied" is distinguishable from "defaulted" — a genuine FR-4 regression guard, not a tautology.
- The plan's contract-note interpretation (malformed `meeting_json` proven in Rust, `original_file_name: null` fallback proven in Vitest) is implemented exactly as flagged and approved; the two halves together cover the FR-3/NFR-2 acceptance bullets.
- Existing Vitest fixtures were updated (`source.mp4` → `260822 - source.mp4`) rather than special-cased, so every pre-existing test now models a pre-feature row through `buildRow`'s `original_file_name: null` default — exactly what the plan prescribed.
- The vault's lowercase-normalized `source.{ext}` (documented at `crates/vault/src/ingest.rs:278`) makes the case-sensitive `SOURCE_BASE_NAME` regex sound; the extension capture group is case-agnostic anyway.
