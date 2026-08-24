---
slug: action-item-archive-grouping
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Archive status and source grouping for action items

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 1 | 0 | 0 |

The diff implements the spec faithfully and completely. Every FR and NFR maps to production code and at least one passing test; the acceptance criteria are covered literally (raw `archived: false` line, JSON `null` for `unsorted/`, `source_date` with the 20xx century pinned against the `%y` pivot, key-set drift pin, YAML round-trip through real PyYAML). Verified independently: full Python suite 417 passed / 2 skipped; `ruff check`, `ruff format --check`, `mypy src` clean; `verify_locks` and `uv lock --check` consistent (pyyaml is dev-group only); `cargo test -p vault` (181 tests across targets) and `cargo clippy -p vault --all-targets -- -D warnings` green — the Rust change is doc-only as planned, its intra-doc links resolve, and the 7 rustdoc warnings pre-date this diff (all in `lib.rs`/`list.rs`). A repo grep confirms no production code path opens an existing artifact `.md` for writing (FR-4b): the only artifact writers are creation-time `write_text_atomic` / `write_bytes`. Nothing out-of-scope was built — no UI surface, no `archived`-conditioned behavior, no migration. The single finding is delivery state, not code: the new FR-5 test file is untracked in git and its plan task is still marked in-progress.

## Findings

### E1 [minor] [improvement] [status: open]

- **Where**: `services/transcription/tests/test_exporting.py` (untracked, `?? ` in `git status`); `specs/action-item-archive-grouping/plan.md` T2 marked `[~]`
- **Spec ref**: FR-5 (both acceptance criteria live in this file)
- **Expected**: The FR-5 pinning tests are part of the committed change set; the plan reflects T2 as done.
- **Actual**: The file exists and both its tests pass (verified in this evaluation), but it was never `git add`ed and the plan checkbox is stale at `[~]`. An ordinary `git commit` of tracked changes would silently drop the entire FR-5 pin, leaving that requirement untested on the merged branch.
- **Suggested fix**: `git add services/transcription/tests/test_exporting.py` and flip T2 to `[x]` in the plan.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (`archived: false` on every item, both kinds) | `services/transcription/src/transcription/jobs.py:942-944` | `tests/test_llm_jobs.py::test_action_items_are_written_with_screenshots_and_front_matter` (raw line + `is False`), `::test_facts_use_the_kind_key_and_audio_only_recordings_get_no_screenshots` | ✓ |
| FR-2 (`source_project` null under `unsorted/`; `source_meeting`; `source_recording` incl. null) | `jobs.py:895-901` (meeting-anchored derivation, `artifacts.UNSORTED_DIR_NAME`) | `tests/test_llm_jobs.py::test_unsorted_meetings_get_a_null_source_project_and_no_recording` (raw `source_project: null` + `is None` + recording `None`); existing test keeps `ELS`/`source.mp4` | ✓ |
| FR-3 (`source_date` from leading `YYMMDD`, null when unparseable, job still succeeds) | `artifacts.py::source_date_from_meeting_name` (explicit digit parse, `date(2000+yy,...)`, ASCII guard); `jobs.py:902,947` | `tests/test_llm_units.py::test_source_date_reads_the_meetings_leading_yymmdd_as_20xx` (incl. `990101`→`2099-01-01` pivot pin), `::test_source_date_is_none_when_the_prefix_is_not_a_calendar_date` (incl. non-ASCII digits); `tests/test_llm_jobs.py::test_a_meeting_without_a_date_prefix_still_succeeds_with_a_null_source_date` | ✓ |
| FR-4 (hand-edit tolerance; no rewrite path) | no behavior change (contract pinned); absence verified by repo grep — only creation-time atomic writes exist | `tests/test_llm_units.py::test_list_items_tolerates_obsidian_style_rewritten_front_matter`, `::test_list_items_never_writes_to_the_files_it_reads`; contract clause in both module docs | ✓ |
| FR-5 (app ignores `archived` in export and listing) | `exporting.py` unchanged by construction (`_item_section` reads title/type/body only) | `tests/test_exporting.py::test_export_output_is_identical_whether_an_item_is_archived` (byte-identical), `::test_list_items_includes_an_archived_item_exactly_like_an_unarchived_one` — **file untracked, see E1** | ✓ (code) / E1 (delivery) |
| FR-6 (documented cross-language contract + drift pin) | `artifacts.py` module docstring field table; `crates/vault/src/artifacts.rs` doc mirror ceding source-of-truth to Python | `tests/test_llm_jobs.py` `ACTION_ITEM_META_KEYS` / `FACT_META_KEYS` exact-set asserts on job-written files | ✓ |
| NFR-1 (strict YAML subset) | `render_front_matter` unchanged | `tests/test_llm_units.py::test_written_front_matter_parses_identically_under_a_real_yaml_parser` (`yaml.safe_load` == `parse_front_matter` == input meta, incl. quotes and non-ASCII) | ✓ |
| NFR-2 (`parse_front_matter` never raises) | `parse_front_matter` unchanged (best-effort) | `tests/test_llm_units.py::test_parse_front_matter_never_raises_on_edited_or_garbled_text` (11-case battery + degradation asserts) | ✓ |
| NFR-3 (atomic writes only) | `write_item` → `write_text_atomic` unchanged; no new write path in diff | pinned transitively by all job-level file-on-disk tests | ✓ |

## Positive notes

- The `source_project` derivation was re-anchored on `meeting_dir.parent` (the job's input) exactly as planned, and `_export_sync`'s duplicate literal `"unsorted"` was converged onto the shared `artifacts.UNSORTED_DIR_NAME` constant — one derivation rule in the codebase instead of two, layout-invariant across the F6 merge.
- `source_date_from_meeting_name` guards `str.isdigit()`'s non-ASCII acceptance with an explicit `isascii()` check and pins it with an Arabic-Indic-digits test — a real bug class caught before it existed. The 20xx century rule is pinned against the `strptime("%y")` pivot with `990101 → 2099-01-01`.
- The FR-6 mirror in `crates/vault/src/artifacts.rs` states unambiguously that the Python docstring + pytest is the source of truth and that F6/F7 may relocate or delete the module — exactly the spec's contingency wording. Doc links to `crate::paths::UNSORTED_DIR_NAME` resolve.
- `pyyaml` was added dev-group only with a comment explaining why, preserving production's zero-YAML-dependency stance; the lock was regenerated and passes both `verify_locks` and `uv lock --check`.
- `source_date` and `project_name` are computed once per job, outside the per-item loop.
