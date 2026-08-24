---
slug: artifacts-in-sync-folder
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Store action items and facts under the recording's own folder

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 0 | 0 | 0 |

The diff implements the spec faithfully and completely. The anchor decision moved to exactly one place per language side (llm.rs `extract_vault_entry_handler` on Rust, `items_for_meeting` on Python), `require_project_dir` was deleted with no residual callers, provenance derivation matches FR-3 including the `null`-for-unsorted branch, and the export path reads meeting-level items with the unsorted special case removed. The Q1 decision (leave legacy in place, stop reading) is honored: no deletion or migration code exists anywhere in the diff, and tests assert both the non-read and the non-delete halves. FR-7's interim surface (`list_project_artifacts`, `report.py`) is documented as legacy but untouched, as required. All four QA layers were re-run during this evaluation and pass: cargo test --workspace (16 suites, 0 failures), cargo fmt --check, clippy -D warnings, vitest (271/271), eslint, tsc, pytest (all green, 2 pre-existing skips), ruff check + format --check, mypy, sync_version/verify_locks, and the scripts/tests suite (161 passed). No findings.

## Findings

None. Adversarial passes performed and cleared:

- **Correctness**: `meeting_dir.parent.name` derivation is safe for both `<root>/<project>/<meeting>` and `<root>/unsorted/<meeting>` shapes (`resolve_entry` yields only these; `code::validate` reserves the word `unsorted` so no project can shadow it). `fit_slug`'s deeper-anchor budget is covered by a realistic 174-char sync-root test including the collision suffix and the 20-char screenshot sibling, plus the refusal branch (`ErrorKind.INVALID_REQUEST`).
- **Security (desktop profile)**: the only IPC change *removes* a gate, not a validation — `output_dir` is now `meeting_dir.join(constant)`, where `meeting_dir` still comes from `resolve_entry`'s canonicalized entry-id lookup and `require_transcript` still guards the input. No path from UI-supplied strings into paths or shell. No secrets, no update/protocol surface touched.
- **Performance**: export reads one directory listing per kind per export; no new loops or I/O amplification.
- **Spec drift**: nothing beyond scope was built (no migration code, no new config, no F7/F8 territory touched; `RecordingPage.tsx` change is exactly the two `disabled`/`title` props, `unsorted` pill retained). Strict skills: none declared for development; nothing to enforce.

One evidence note adjudicated rather than filed as a finding: T7's real-LLM smoke did not exercise FR-4's screenshot-link rewriting (fixtures had no media). On its merits the unit-level evidence is sufficient: `test_export_rewrites_item_screenshot_links_relative_to_the_export_dir` (services/transcription/tests/test_llm_jobs.py) runs the *real* export job through `JobManager` (the export path involves no LLM at all), writes a real PNG via `write_item`, and asserts both the rewritten `(../../action items/<slug>/screenshot-0010.png)` string in `export.md` and that the resolved path is an existing file on disk. That is a full-fidelity exercise of the rewriting code path.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (meeting-level anchor, unchanged names) | apps/desktop/src-tauri/src/commands/llm.rs:233 (`meeting_dir.join(artifact_kind.dir_name())`); crates/vault/src/paths.rs docs; services/transcription/src/transcription/artifacts.py docs | llm.rs::`extraction_targets_the_meeting_level_artifact_directory` (asserts `<meeting>/facts` and negates `<PROJECT>/facts`); paths.rs::`artifact_directory_names_are_the_pinned_cross_language_strings`; test_llm_units.py::`test_artifact_dir_names_pin_the_cross_language_contract`; test_llm_jobs.py extraction tests retargeted to `meeting_dir / "<kind>"` incl. `assert not (meeting_dir.parent / "action items").exists()` | ✓ |
| FR-2 (unsorted extraction un-gated) | llm.rs (`require_project_dir` deleted, handler line ~228); apps/desktop/src/components/RecordingPage.tsx:217,225 | llm.rs::`extraction_on_an_unsorted_meeting_enqueues_into_the_meeting_folder`, ::`extraction_without_a_transcript_is_refused_with_an_actionable_message` ("transcribe it first"); RecordingPage.test.tsx::"extracts action items and facts from an unfiled recording", ::"no longer tells…", ::"disables an extraction button while its own job is in flight" | ✓ |
| FR-3 (provenance: parent name, null for unsorted) | services/transcription/src/transcription/jobs.py:895-898 | test_llm_jobs.py::`test_action_items_are_written_with_screenshots_and_front_matter` (`source_project == "ELS"`, `source_meeting`); ::`test_extraction_on_an_unsorted_meeting_records_a_null_source_project` | ✓ |
| FR-4 (export reads meeting folder; unsorted special case gone) | services/transcription/src/transcription/exporting.py:71-78,103-132; jobs.py:1011-1019 (`project_dir` computation deleted) | test_llm_jobs.py::`test_export_assembles_sections_in_order_and_renders_a_pdf`; ::`test_export_of_an_unsorted_meeting_includes_its_meeting_level_items`; ::`test_export_rewrites_item_screenshot_links_relative_to_the_export_dir` (real file resolution) | ✓ |
| FR-5 (legacy dirs never listed as meetings) | crates/vault/src/paths.rs:472 (`RESERVED_PROJECT_DIR_NAMES` unchanged); list.rs behavior untouched | paths.rs::`reserved_project_dir_names_cover_the_legacy_trees_but_not_exports`; list.rs::`reserved_artifact_directories_are_never_listed_as_meetings` (pre-existing, passing unchanged) | ✓ |
| FR-6 (Q1: leave in place, stop reading, never delete) | No migration/deletion code anywhere in the diff; exporting.py reads meeting dir only | test_llm_jobs.py export test: legacy item with matching `source_meeting` absent from export **and** `legacy_md.is_file()` after the job; extraction test asserts no writes to `meeting_dir.parent / "action items"` | ✓ |
| FR-7 (won't: no aggregation rework) | `list_project_artifacts`, `report.py`, browsing handlers untouched (doc-only edits) | n/a (negative requirement; verified by diff inspection — artifacts.rs body and report.py logic unchanged) | ✓ |
| NFR-1 (260-char budget at the deeper anchor) | artifacts.py `fit_slug` (unchanged, anchor-agnostic) | test_llm_units.py::`test_fit_slug_fits_a_realistically_deep_meeting_level_parent` (174-char sync root, collision suffix, longest screenshot sibling, hopeless-depth refusal) | ✓ |
| NFR-2 (atomic write ordering) | artifacts.py `write_item` (untouched: images first, then `write_text_atomic`) | Pre-existing write_item tests; verified untouched by diff | ✓ |
| NFR-3 (all four make targets green) | — | Re-run during evaluation: cargo fmt/clippy/test, eslint/tsc/vitest, ruff check+format/mypy/pytest, sync_version, verify_locks, scripts/tests — all pass | ✓ |

## Positive notes

- The plan's single-decision-point architecture held: the anchor is chosen in exactly one Rust expression and one Python function, which is why the diff stays this small. Fixers should not re-introduce any anchor logic elsewhere.
- The negative assertions are as strong as the positive ones: nothing-written-to-legacy after extraction, legacy-item-excluded-but-still-on-disk after export, and the Rust test explicitly negates the old `<PROJECT>/facts` target. FR-6's "nothing silently deleted" is pinned by tests, not just by absence of code.
- The screenshot-link export test resolves the rewritten relative path against the real filesystem rather than only string-matching — do not weaken it to a substring check.
- Doc updates are thorough and honest about the legacy state (paths.rs, list.rs comment, artifacts.rs "Legacy anchor" section, artifacts.py layout table), which materially de-risks the F7 demolition that follows.
- The `first_llm_submission` helper deduplicated the polling loop instead of copy-pasting it into the new unsorted test.
