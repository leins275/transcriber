---
slug: 260909-trn-260909-spec (consolidated batch: search-index-in-vault, speaker-tagged-embeddings, job-progress-accuracy, selection-menu-viewport-clamp, per-turn-speaker-reassign, project-speaker-roster)
base_ref: 0cdd13df2dc6ac7a4d1254bbd35e632feac954f3
round: 2
---

# Evaluation report: TRN 260909 batch (six merged features)

Scope judged: `git diff 0cdd13d..HEAD` excluding `specs/` (77 files, +7913/-276), against all six approved blueprints. Where F4/F5/F6 share `SelectionSpeakerMenu.tsx` / `SpeakerTag.tsx` / `TranscriptViewer.tsx` the merged result was judged against all three, with F5's assign-vs-rename semantics and the `Edit speaker for this turn` label taken as ruling over F6's FR-4 b2 wording (`Rename Maxim` / `onRename` on a roster pick), per the orchestrator's ruling.

Evidence gathered: full diff and every touched source file read; `uv run pytest` over the 14 new/changed service test files (209 cases, exit 0, model/GPU/network-free); `vitest run` over the seven desktop files named in the stale-test list (127 cases green **on the working tree** — see E1). Cargo was not run here (a concurrent QA run holds the target dir); `tests/job_phase.rs` and `tests/roster.rs` were read in full instead. `frontend-toolkit:internal-ui` is not installed; UI was judged against the repo's own conventions (CSS-module tokens, `btn btn-ghost` / `pill`, role/label-named controls, inline `role="alert"`).

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 0 | 5 | 4 |

The diff implements all six blueprints. Every must-FR maps to code and to a behaviour-level test authored before implementation; the four security lenses turn up nothing exploitable (all new SQL is bound with `?` placeholders, the two new Tauri commands go through the shared `project_dir` validation and never create a project, the roster read is capped, `/v1/search`'s `speaker` is length-bound and normalised). What remains is a set of small edges: one documentation byte-corruption introduced by the batch (E2), a locale-dependent divergence between the TS and Rust roster normalisation that FR-2 explicitly forbids (E3), a few deliberate implementer deviations that should be recorded as accepted rather than silently kept (E5, E6), and the fact that HEAD itself still carries the eight stale tests whose fixes sit uncommitted in the working tree (E1).

### Adjudication of `<test-mismatches>`

| Item | Ruling |
|---|---|
| F2 T4: overlong `speaker` → blueprint says 422, test asserts 400 | **Test right, blueprint wrong.** `app.py`'s `RequestValidationError` handler maps every validation failure on every route to `400 invalid_request`; the blueprint's 422 would have broken the service's own error contract. |
| F3 T2: `speaker_counting` / `discrete_diarization` step-finished calls → task text says `None`, FR-4 b4 says `1.0` | **FR-4 right, T2 case list wrong.** `_step_fraction` returns `1.0` for artifact-without-counts uniformly (`diarizer.py`), which matches the requirement and pyannote 3.4's call shape. |
| F3 T7: `activeJob.test.ts` "stays untouched" | **Blueprint wrong.** `ActiveJobView` gained `phase`; the file's two whole-view `toEqual`s cannot survive that. The `phase: null` additions are the right fix (present in the working tree, not at HEAD — E1). |
| F5 fixtures: separator turns needed for "2 turns of Maxim" | **Test authors right.** `groupIntoTurns` merges adjacent same-speaker segments regardless of pause; the blueprint's fixture sketch was wrong about that. |
| F6 T7: `onClose` vs `onCancel` | **Naming only.** The blueprint's Architecture section already said `onClose`; T7's Implement line said `onCancel`. Code uses `onClose`, called after a landed save and on Cancel, matching FR-6. |
| F6 T8: `save(roster)` one-arg, rejects on refusal | **Test right.** The hook is bound to the open recording's project; `App.tsx` adapts to `RecordingPage`'s `(project, draft)` signature in one line. |
| Factory: F3 `JobSnapshot.phase` made optional in TS | Minor spec drift, see E5 — recommend accepting. |
| Factory: F6 `chats.rs` unknown project now `invalid_argument` | **Code right.** F6 FR-3 b2 requires "rejected"; `tests/roster.rs` pins `invalid_argument`; the comment on `project_dir` now states exactly this. No chats test pinned the old `not_a_file`. Accept. |
| Factory: F1 helper skips stray `-wal`/`-shm` without a main file | Minor spec drift, see E6. |
| Factory: F2 `best_chunk_for` has no fallback under a filter | **Code right.** FR-4 b1 says the returned chunk "is one of the matching chunks"; an unscoped fallback would violate that. `_ranked` drops the doc (`continue`) rather than showing another speaker's text. Accept. |
| Immediately-green cases (F1 ×3, F2 ×13, F3 ×4, F4 ×1, F5 ×4, F6 ×~10) | All are negative/unchanged-behaviour invariants (stdout stays clean, no-filter path unchanged, queued rows show no phase, open mode = today). None indicates the plan was wrong about what existed; they are regression guards and are kept. |
| Gaps reported by the test wave | F2 FR-2 `make_snippet` unchanged — code untouched and `tests/test_hybrid.py` already pins the first-line drop; not a gap. F3 FR-2 monotone denominator on split-retry — not expressible without a seam; accepted. F4 FR-2 c4 (layout effect before first paint) — verified by review: `useLayoutEffect(measure, [measure])` with `measure` keyed on `anchor.x/y`. F6 boundary values — see E9. |

## Findings

### E1 [minor] [correctness] [status: fixed] — F3 / F5 / F6

- **Where**: HEAD `apps/desktop/src/components/SpeakerTag.test.tsx:151-156`, `apps/desktop/src/lib/activeJob.test.ts:38,68`, `apps/desktop/src/components/RecordingPage.test.tsx:228`, `apps/desktop/src/components/SpeakerTag.roster.test.tsx` (three `Rename <name>` queries + the "renames throughout" case)
- **Spec ref**: F3 T7 / F5 T2, T3 / F6 T4 "Done when" (suites green); CLAUDE.md release rule (merge to `main` is the ship decision)
- **Expected**: the merged `main` HEAD passes `npm --prefix apps/desktop run test`.
- **Actual**: at HEAD eight desktop cases are red (the known stale set). The corrections exist **only as uncommitted working-tree edits** in those four files (`git status`: ` M` on each); with them applied the seven affected files pass 127/127. Whatever pushes `main` next must not lose them.
- **Suggested fix**: commit the four test-file edits (they are the correct adjudications above; nothing in the code needs to change for them).

### E2 [minor] [spec-drift] [status: fixed] — F6

- **Where**: `CLAUDE.md:14` (introduced by commit `678002e`, "keep the CLAUDE.md layout table unformatted")
- **Spec ref**: F6 FR-8 (docs record the artifact; nothing else in CLAUDE.md should change)
- **Expected**: the layout table row reads `` `uv` only — never system `python` `` (UTF-8 em dash `E2 80 94`, as at base).
- **Actual**: the row now reads `` `uv` only â€” never system `python` `` (bytes `C3 A2 E2 82 AC E2 80 9D` — an em dash round-tripped through cp1252). The rest of the file is fine; only this row was re-encoded.
- **Suggested fix**: restore the em dash on that line (`git show 0cdd13d:CLAUDE.md` line 14).

### E3 [minor] [correctness] [status: fixed] — F6

- **Where**: `apps/desktop/src/lib/roster.ts:35` (`toLocaleLowerCase()`), `apps/desktop/src/lib/roster.ts:55-56`; Rust side `apps/desktop/src-tauri/src/commands/roster.rs:105` (`to_lowercase()`)
- **Spec ref**: F6 FR-2 b3 — "The TypeScript `lib/roster.ts` helpers produce the same result for the same input, so the editor never shows a list the backend would change on save."
- **Expected**: identical case-folding on both sides of the IPC boundary.
- **Actual**: `toLocaleLowerCase()` with no locale argument folds by the **host's** default locale; on a Turkish-locale Windows (`tr-TR`, a real possibility — Turkish is one of the three decode languages) `"Ilya"` folds to `"ılya"` in TS but `"ilya"` in Rust, so `["Ilya", "ilya"]` is two names in the editor and one after save — exactly the drift FR-2 b3 forbids.
- **Suggested fix**: use `toLowerCase()` (locale-independent Unicode default case mapping, which is what Rust's `str::to_lowercase` implements) in `normalizeRosterNames` and `removeRosterName`; `ProjectRosterPanel.tsx:104` uses `toLocaleLowerCase()` for a React `key` and can change with it.

### E4 [minor] [correctness] [status: fixed] — F6

- **Where**: `apps/desktop/src/components/ProjectRosterPanel.tsx:57` (`useState<ProjectRosterView>(roster)`); `apps/desktop/src/components/RecordingPage.tsx:495` (`roster={projectRoster ?? EMPTY_ROSTER}`)
- **Spec ref**: F6 FR-6 b4 ("reopening shows the persisted roster"), FR-7 b1
- **Expected**: the draft the editor saves is derived from the persisted roster.
- **Actual**: the draft is seeded from the prop once, at mount. `useProjectRoster` resets to `EMPTY_ROSTER` on every project change and then reads asynchronously; if the panel is opened before that read settles (or after a read that degraded to `EMPTY_ROSTER`), Save writes `{open, []}` over the persisted file. The window is one local IPC round-trip, so it is hard to hit by hand, but the panel silently diverging from a prop it was handed is the kind of thing that later features trip over.
- **Suggested fix**: render the panel with `key={\`${roster.mode}:${roster.names.join("")}\`}` in `RecordingPage`, or sync the draft from the prop while the draft is still untouched.

### E5 [minor] [spec-drift] [status: accepted] — F3

- **Where**: `apps/desktop/src/types.ts:80` (`phase?: string | null`)
- **Spec ref**: F3 FR-7 b2 / T6 ("`JobSnapshot.phase: string | null` in `types.ts`")
- **Expected**: required field, mirroring the Rust struct that always serialises the key (`null` when unset).
- **Actual**: optional, because seven pre-existing test files build `JobSnapshot` literals without it. Runtime behaviour is identical (every reader uses `job.phase ?? null` / truthiness); the type is merely weaker than the wire.
- **Suggested fix**: accept as-is (recommended — the wire guarantees the key and the readers are defensive), or make it required and add `phase: null` to the seven builders in the same fixer pass as E1.

### E6 [minor] [spec-drift] [status: fixed] — F1

- **Where**: `services/transcription/src/transcription/search/index_db.py:186-187` (`if not legacy.exists(): return False`)
- **Spec ref**: F1 FR-1 b1 ("deleted, together with its `-wal` and `-shm` sidecars"); Architecture: "Otherwise unlink the three files with `missing_ok=True`; return `True` iff the main file existed and was removed"
- **Expected**: the three-file unit is always swept once the guard passes.
- **Actual**: when only sidecars survive (a crash mid-checkpoint on the old build), the early return leaves `index.sqlite3-wal` / `-shm` in the app dir forever — the literal "orphan in the app folder" the operator asked not to have. The factory notes record this as deliberate; nothing in the blueprint asks for it.
- **Suggested fix**: replace the `exists()` early-return with `existed = legacy.exists()` before the loop and `return existed` after it (the log record still fires only when the main file was there). Test 1/3 of `test_legacy_index_cleanup.py` keep their meaning.

### E7 [minor] [correctness] [status: accepted] — F2

- **Where**: `services/transcription/src/transcription/search/speakers.py:97-103` (`extract_query_speakers`), `:74-77` (`_full_name_pattern`)
- **Spec ref**: F2 FR-5 b1 (pure logic; "returns the subset of `known` the text mentions")
- **Expected**: a key that names nobody never matches.
- **Actual**: a whitespace-only key is truthy, survives the `if not key` guard, and `_full_name_pattern("   ")` compiles to `(?<!\w)(?!\w)`, which matches at every position — every question would be scoped to that key. Unreachable through the index today (`upsert_doc` strips and drops blanks before storing), so this is a robustness hole in a public pure function, not a live bug; `_first_name_pattern` would also `IndexError` on `key.split()[0]` for the same input if reached first.
- **Suggested fix**: `key = name.strip().casefold()` and `if not key or GENERIC_LABEL.match(key): continue`.

### E8 [minor] [improvement] [status: accepted] — F5 (shared with F6)

- **Where**: `apps/desktop/src/components/SpeakerTag.tsx:147-160` (the `SpeakerNameField` stays fully editable while `mode === "choosing"`), `:88-98` (`assignThisTurn` / `renameEverywhere` read `draft` at click time)
- **Spec ref**: F5 T2 Implement — "the input stays visible (read-only or disabled is fine; keep the draft on screen)"
- **Expected**: the two scope buttons act on the name the operator confirmed with Enter.
- **Actual**: the box is still live, so the operator can click back into it, change the text, and then press "All 3 turns of Speaker 2" — the label promises one thing, the write uses whatever is in the box now, without re-running `commit`'s guards (same-name → no-op, blank → `onAssign("")`/`renameSpeaker` no-op, so nothing corrupting, but the chooser's contract is bypassed). The blueprint explicitly allowed read-only/disabled here.
- **Suggested fix**: pass `readOnly` (input) / `disabled` (select) to `SpeakerNameField` while choosing, or re-enter `editing` on any change so a fresh Enter is required.

### E9 [minor] [improvement] [status: accepted] — F6 / F3 (coverage gaps)

- **Where**: `apps/desktop/src-tauri/tests/roster.rs`, `apps/desktop/src/lib/roster.test.ts`, `apps/desktop/src-tauri/tests/job_phase.rs`
- **Spec ref**: F6 FR-2 b2 (limits: "longer than 200 characters" / "more than 500 names"); F3 FR-7 b1 (`HttpService::status` decodes a body with no `phase` key — and, by the Architecture's `#[serde(default)]` on both fields, no `progress` key either)
- **Expected**: boundary and default-decoding cases pinned.
- **Actual**: `roster.rs` tests 201 chars / 501 names but not exactly 200 / 500 (both accepted by the code — `> MAX`), so an off-by-one regression would pass; `job_phase.rs` covers `"progress": null` and a missing `phase` key but not a missing `progress` key (`StatusResponse.progress` has `#[serde(default)]`, untested). The TS side has no limit tests by design (limits are Rust-only).
- **Suggested fix**: one parametrised boundary case per limit in `roster.rs`; one `{"status":"running"}` body case in `job_phase.rs`.

## Coverage matrix

### F1 — 260909-search-index-in-vault

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 b1 three files removed | `search/index_db.py::remove_legacy_app_dir_index` | `test_legacy_index_cleanup.py::test_legacy_app_dir_index_is_deleted_with_its_wal_and_shm_sidecars`, `::test_service_startup_removes_the_orphaned_app_dir_index` | ✓ (E6 for the sidecar-only edge) |
| FR-1 b2 vault index untouched | same (resolve-equality guard, only `data/index.sqlite3*` touched) | `::test_the_in_vault_index_survives_the_cleanup_byte_for_byte` | ✓ |
| FR-1 b3 runs in `lifespan` before the job manager | `app.py:153-157` (first statement of `lifespan`) | `::test_service_startup_removes_the_orphaned_app_dir_index` (TestClient enter) | ✓ |
| FR-1 b4 one `legacy_index_removed` record, stdout clean | `app.py:154` (`_logger.info(..., extra={"event": ...})`) | `::test_service_startup_logs_the_removal_exactly_once`, `::test_service_startup_keeps_stdout_free_of_the_removal` | ✓ |
| FR-2 b1 configured app-dir index kept | `index_db.py:184` | `::test_an_app_dir_index_that_is_still_the_configured_one_is_kept` (parametrised: vault-less default, explicit path, `data/../data` spelling) | ✓ |
| FR-2 b2 silent when nothing exists | `index_db.py:186` | `::test_nothing_to_clean_up_is_reported_as_no_removal`, `::test_service_startup_stays_silent_when_there_is_no_orphan` | ✓ |
| FR-2 b3 OSError → warning, service starts | `index_db.py:188-197` | `::test_an_unremovable_legacy_index_is_reported_as_a_warning_instead_of_raising`, `::test_service_starts_even_when_the_orphan_cannot_be_removed` | ✓ |
| FR-3 README row | `services/transcription/README.md:92` | — (doc; verified by review) | ✓ |
| NFR-1 no startup cost | two `resolve()`, one `exists()`, ≤3 `unlink` | review | ✓ |

### F2 — 260909-speaker-tagged-embeddings

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 b1 chunk findable by its speakers only | `index_db.py` `_SCHEMA` (`chunk_speakers` + index), `upsert_doc:410-424`, `_TAGGED_CHUNKS` | `test_index_db_speakers.py::test_a_chunk_is_found_by_a_speaker_who_has_a_line_in_it`, `::..._not_found_by_a_speaker_with_no_line_in_it`; `test_indexer_speakers.py::test_a_speaker_filter_finds_only_the_meeting_where_that_speaker_speaks` | ✓ |
| FR-1 b2 override outranks label | `indexer.py::_transcript_lines:118-121` (same resolution as the rendered line) | `test_indexer_speakers.py::test_an_operator_name_replaces_the_diarization_label_it_overrides` | ✓ |
| FR-1 b3 no tag for unlabelled lines / summary / note | `indexer.py::_speakers_in`, `flat_breadcrumb` ignores speakers, `ChunkRecord.speakers=()` default | `::test_only_speakers_with_transcript_lines_become_known`, `::test_summaries_and_notes_are_unreachable_through_a_speaker_filter` | ✓ |
| FR-1 b4 casefold keys | `upsert_doc` (`casefold()`), `_speaker_keys` | `test_index_db_speakers.py::test_the_speaker_filter_matches_a_cyrillic_name_regardless_of_case` | ✓ |
| FR-1 b5 no orphan tags on replace / sweep | `_delete_doc_locked:437-441` (explicit delete + FK cascade) | `::test_replacing_a_doc_forgets_the_speakers_of_its_old_chunks`, `::test_sweeping_a_doc_gone_from_disk_forgets_its_speakers` | ✓ |
| FR-2 b1 breadcrumb `[p / m / w / A, B]`, omitted when none | `indexer.py::transcript_breadcrumb:327-336` | `test_indexer_speakers.py::test_a_transcript_breadcrumb_names_its_speakers_in_order_of_first_appearance`, `::test_a_transcript_without_speaker_labels_gets_a_breadcrumb_without_them`, `::test_the_text_handed_to_the_embedder_names_the_speakers` | ✓ |
| FR-2 b2 summary/note breadcrumb unchanged | `flat_breadcrumb` | `::test_a_notes_breadcrumb_is_unchanged` | ✓ |
| FR-2 b3 `make_snippet` unchanged | `search/hybrid.py::make_snippet` (untouched) | existing `tests/test_hybrid.py` (first-line drop) | ✓ (code untouched) |
| FR-3 b1 schema v2, rw open recreates | `INDEX_SCHEMA_VERSION = 2`, `_migrate_or_recreate` | `test_index_db_speakers.py::test_an_index_from_an_older_schema_version_is_recreated_empty` | ✓ |
| FR-3 b2 read-only reports stale; MCP answers "not built" | `IndexDb.__init__:243-248` (`schema_stale`), `mcp_server.py::_Vault.index:103-109` | `::test_an_older_schema_version_opened_read_only_is_reported_stale`, `::test_a_current_schema_version_opened_read_only_is_not_stale`; `test_mcp_server_speakers.py::test_hybrid_search_over_an_older_schema_index_asks_for_the_app_instead_of_answering` | ✓ |
| FR-4 b1 every channel filtered; hit chunk is a tagged one | `index_db.py` `fts_query`, `best_chunk_for`, `title_trigram_query`, `exact_title_docs`, `vec_query` (post-filter within over-fetch); `service.py::_ranked` (`continue` when no tagged chunk) | `test_index_db_speakers.py::test_best_chunk_for_returns_a_chunk_the_filtered_speaker_took_part_in`, `::test_exact_title_docs_filtered_by_speaker_...`, `::test_title_trigram_query_filtered_by_speaker_...`, `::test_vec_query_filtered_by_speaker_...` (skips without sqlite-vec); `test_api_search_speakers.py::test_a_filtered_hits_snippet_comes_from_that_speakers_lines` | ✓ |
| FR-4 b2 unknown speaker → empty, not error | `_TAGGED_CHUNKS` IN-subquery yields nothing | `test_index_db_speakers.py::test_a_filter_naming_an_unknown_speaker_finds_nothing_in_any_text_channel`; `test_api_search_speakers.py::test_a_speaker_nobody_in_the_vault_answers_empty_not_an_error`; MCP `::..._finds_nothing_instead_of_failing` | ✓ |
| FR-4 b3 no filter → identical | `_speaker_keys(None) → None`, SQL unchanged | `test_index_db_speakers.py::test_an_unfiltered_read_still_returns_every_doc`; `test_api_search_speakers.py::test_a_query_without_a_speaker_still_finds_every_meeting`; existing `test_index_db.py`, `test_api_search.py` | ✓ |
| FR-4 b4 `POST /v1/search` `speaker` (≤200, blank = none, wire shape unchanged) | `schema.py::SearchRequest.speaker`, `api/search_routes.py:74-75` | `test_api_search_speakers.py::test_a_speaker_filter_keeps_only_that_speakers_chunks`, `::test_a_blank_speaker_means_no_filter`, `::test_a_speaker_filtered_hit_keeps_the_pinned_wire_shape`, `::test_an_overlong_speaker_is_rejected_as_an_invalid_request` (400, adjudicated) | ✓ |
| FR-4 b5 MCP `hybrid_search(speaker=)` | `mcp_server.py:143-170` | `test_mcp_server_speakers.py::test_hybrid_search_scoped_to_a_speaker_...`, `::..._without_a_speaker_returns_every_matching_meeting`, `::test_the_hybrid_search_tool_documents_its_speaker_argument` | ✓ |
| FR-5 b1 `extract_query_speakers` rule | `search/speakers.py` | `test_search_speakers.py` (12 cases: first name, two names, 2-letter ending, patronymic excluded, `Ольге`, `марте` excluded, full name, Latin case, namesakes, nobody, unknown) | ✓ (E7 for a blank key) |
| FR-5 b2 generic labels never matched | `GENERIC_LABEL` | `::test_generic_diarization_labels_are_never_matched` | ✓ |
| FR-5 b3 OR semantics / empty set | `extract_query_speakers` returns a set; `_speaker_keys` IN (...) | `::test_namesakes_are_both_named_by_a_bare_first_name`, `::test_a_question_naming_nobody_scopes_to_nobody` | ✓ |
| FR-5 b4 chat auto-scope, project-scoped known set, composes with dates | `api/search_routes.py::chat:172-190` (inside the `run_serial` callable), `index_db.py::known_speakers` | `test_api_chat_speakers.py` (6 cases incl. `::test_a_name_absent_from_the_requests_project_is_not_a_filter`, `::test_the_speaker_and_date_filters_compose`); `test_index_db_speakers.py::test_known_speakers_is_scoped_to_one_project` | ✓ |
| FR-6 README | `services/transcription/README.md` "Hybrid search and the MCP server" (tags, breadcrumb, `speaker`, chat auto-scope, one-time rebuild) | — (doc; verified by review) | ✓ |
| NFR-1 model-free suite | `FakeEmbedder`, tmp SQLite everywhere | 209-case targeted run, exit 0 | ✓ |
| NFR-2 indexed lookup | `chunk_speakers_by_key`, IN-subqueries | review | ✓ |

### F3 — 260909-job-progress-accuracy

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 b1-b3 `progress: float\|null` clamped, `phase`, queued 0.0/null, succeeded 1.0/null incl. ledger | `schema.py::JobStatus`, `jobs.py::JobState`, `_set_phase`, `_job_state_from_ledger_row` (untouched), `app.py::get_job` | `test_jobs_phase.py::test_job_status_accepts_a_null_progress_with_a_phase_label`, `::..._clamps_...` (1.7/-0.2), `::test_a_queued_job_reports_zero_progress_and_no_phase`, `::test_a_succeeded_job_...`, `::test_a_failed_job_reports_no_phase`, `::test_a_job_rebuilt_from_the_ledger_...` | ✓ |
| FR-1 b4 README semantics table | `services/transcription/README.md` "Progress and phases" | — (doc) | ✓ |
| FR-2 summarize phases, no token/cap percentage | `jobs.py::_summarize_sync` (`call_label`, `on_token`), `_complete_text(on_token=)` (no `on_progress` passed) | `test_llm_jobs.py::test_a_summarize_job_reads_the_transcript_before_the_first_token`, `::..._counts_the_tokens_it_has_written`, `::test_a_map_reduced_summary_names_the_part_it_is_summarizing`, `::test_a_finished_summarize_job_reports_a_full_bar_and_no_phase` + existing summary.md/sidecar cases | ✓ |
| FR-3 export two phases, constants gone | `jobs.py::_export_sync:1198-1208` | `::test_an_export_job_reports_writing_the_markdown_while_it_is_built`, `::..._rendering_the_pdf_while_it_renders`, `::test_a_finished_export_job_...`; existing font-warning case | ✓ |
| FR-4 pyannote hook (signature, step map, fractions, both calls, `total == 0`) | `diarizer.py::_STEP_PHASES`, `_step_fraction`, `_progress_hook`, `diarize()` | `test_diarizer.py::test_the_load_and_decode_phases_are_reported_before_each_step_runs`, `::test_a_pipeline_step_is_reported_under_its_display_phase` (parametrised incl. unknown step), `::test_the_reported_fraction_follows_the_pipeline_step_counts` (incl. `total=0`, `completed>total`), `::test_a_pipeline_without_embedding_support_still_reports_its_steps` (retry), `::..._without_a_progress_listener_still_diarizes` | ✓ |
| FR-5 transcribe `preparing`, unscaled, relay, `naming speakers`, degrade | `jobs.py::_run_job:697-725,774`, `_diarize_segments:661-669` | `test_jobs_phase.py::test_transcription_reports_preparing_until_...`, `::test_the_first_provider_progress_clears_the_phase_...`; `test_jobs_diarization.py::test_transcription_reports_its_own_fraction_unscaled_with_diarization_queued`, `::test_a_running_diarization_pass_shows_the_engines_current_step`, `::test_a_diarized_transcription_ends_at_full_progress_with_no_phase`, `::test_a_diarization_that_cannot_decode_still_ends_at_full_progress`, `::test_a_diarize_job_names_a_returning_voice_from_a_sibling_meeting` | ✓ |
| FR-6 standalone diarize phases, constants gone | `jobs.py::_diarize_existing_sync:967,990-996` | `test_jobs_diarization.py::test_a_running_diarize_job_shows_the_engines_current_step`, `::test_a_finished_diarize_job_ends_at_full_progress_with_no_phase`, `::test_a_diarize_job_cancelled_mid_pass_ends_without_a_phase` | ✓ |
| FR-7 Rust pass-through, `#[serde(default)]`, phase-aware dedupe, fake | `service/mod.rs::JobStatus`, `service/http.rs::StatusResponse`, `service/fake.rs`, `jobs.rs::apply_status`, `snapshots_equal_ignoring_created_at`, `new_pending_snapshot` (the derived-job constructors in `commands*.rs` build through `JobRegistry` helpers, so no literal was missed) | `tests/job_phase.rs` (9 cases: `from_wire`, null progress decode, missing `phase` key, fake walk, phase-only poll emitted, terminal 1.0/None, serde `"phase"` key) | ✓ (E9: missing `progress` key untested) |
| FR-8 JobRow meta line, real bar vs indeterminate track, header chip, non-running rows | `JobRow.tsx::metaLine`, `percentFor`, render branch; `JobRow.module.css` (`data-indeterminate`, `.progressSliver`); `lib/activeJob.ts` (`phase` only when running); `AppHeader.tsx::jobSuffix` | `JobRow.test.tsx` (9 new cases: phase+percent, phase without `%`, progressbar without `aria-valuenow`, `aria-valuenow=42` + 42% fill, queued/done/failed with stray phase, 1.7 clamp); `AppHeader.test.tsx` (3 new: phase when no percent, percent wins, queued neither); `activeJob.test.ts` (working tree, E1) | ✓ (E5) |
| FR-9 CLI phase line, no crash on null | `cli.py::_run_transcribe:282-298` | `test_jobs_phase.py::test_the_cli_reports_the_preparing_phase_before_any_transcription_progress`, `::..._prints_no_progress_line_for_it`; existing `test_cli.py` | ✓ |
| NFR-1 Event-synchronised fakes | `tests/fakes.py::FakeDiarizer` (`blocked`/`release`), test-local gated provider/LLM | review of the new cases: no `sleep`-based timing | ✓ |
| NFR-2 per-token cost | `_set_phase` = one f-string + one clamp on the worker thread | review | ✓ |

### F4 — 260909-selection-menu-viewport-clamp

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (all six bullets) | `lib/viewportPosition.ts::clampToViewport` | `lib/viewportPosition.test.ts` (9 cases, hardcoded expectations: 400/308, 792, 8, 732, 12, 12, 8, 500/308, defaults) | ✓ |
| FR-2 b1-b3 rendered `left/top` | `state/useClampedPosition.ts`, `SelectionSpeakerMenu.tsx:52,96` (`style={placement}`), `.menu` transform removed | `SelectionSpeakerMenu.position.test.tsx` (`792px/308px`, `732px`, `400px`) | ✓ |
| FR-2 b4 before first paint | `useLayoutEffect(measure, [measure])` | review only (not expressible in jsdom) | ✓ |
| FR-3 anchor change / resize / unsubscribe | `measure` keyed on `anchor.x/y`; `useEffect` resize listener with cleanup | `::re-places itself when the selection moves to a new anchor`, `::..._after a resize` (`392px`), `::stops measuring once unmounted` | ✓ |
| FR-4 everything else unchanged; existing suites untouched | `SelectionSpeakerMenu.tsx` diff limited to the hook + the F6 field swap | `SelectionSpeakerMenu.test.tsx` (unmodified, 10/10), `TranscriptViewer.test.tsx` selection cases (unmodified) | ✓ |
| NFR-1 no new dependency | `package.json`/lock untouched | review | ✓ |
| NFR-2 one layout read per anchor / resize | `measure` is the only `getBoundingClientRect` caller | `::stops measuring once unmounted` (spy count) | ✓ |

### F5 — 260909-per-turn-speaker-reassign

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 chooser with real N, nothing written until picked; narrow / wide outcomes; trim; same name no-op | `SpeakerTag.tsx::commit`, `assignThisTurn`, `renameEverywhere`, `choosing` render branch; `TranscriptViewer.tsx` `turnsHeld` | `SpeakerTag.test.tsx::asks which turns a changed name applies to...`, `::gives this turn alone...`, `::renames the speaker everywhere...`, `::trims the typed name...`, `::writes nothing when the name is retyped unchanged`; `TranscriptViewer.test.tsx::renames a speaker everywhere when the operator picks the wider scope`, `::gives one turn away...`, `::offers the wider scope in turns, not in segments` | ✓ (E8) |
| FR-2 per-turn reassignment to a new / existing name | `assignThisTurn → onAssign(trimmed)` → `assignSpeaker` | `TranscriptViewer.test.tsx::hands a whole multi-segment turn to a name the transcript has never seen`; `SpeakerTag.test.tsx::hands this turn to a name already in use in the transcript` | ✓ |
| FR-3 Escape / focus-leave discard; in-tag focus moves do not | wrapper `onKeyDown` (Escape), `handleBlur` with `relatedTarget` containment | `SpeakerTag.test.tsx::abandons the edit on Escape in the input`, `::...while the chooser is open`, `::discards an unanswered chooser when focus leaves the tag`, `::discards an unconfirmed change...`; `TranscriptViewer.test.tsx::writes nothing when the scope choice is abandoned with Escape`, `::...clicks away from an open scope choice` | ✓ |
| FR-4 focus lands on "Only this turn", Tab reaches the wide one, hint names the scope | `narrowChoiceRef` effect; hint `Enter, then choose the scope` | `SpeakerTag.test.tsx::opens the chooser on the narrower choice...`, `::puts the wide choice one Tab away...` (assertion order fixed in working tree, E1), `::promises a scope choice while editing...` | ✓ |
| FR-5 N ≤ 1 commits directly, hint `this turn only` | `commit` (`turnsHeld <= 1`) | `SpeakerTag.test.tsx::commits straight away when nobody else holds the name`, `::says the edit is this turn only...`; `TranscriptViewer.test.tsx::renames a speaker held by a single turn without asking about scope` | ✓ |
| FR-6 unattributed Enter/blur, clear → `onAssign(null)`, known buttons unchanged, `SelectionSpeakerMenu` untouched by F5 | idle branch byte-identical apart from `setMode`; `handleBlur` commits for `speaker === null` in open mode | `SpeakerTag.test.tsx::names an unattributed turn on Enter`, `::...when focus leaves the tag`, `::asks only for a name...`, `::unattributes just this turn when the box is emptied`, `::still attributes this turn to a known name in one click`; `TranscriptViewer.test.tsx` kept cases | ✓ |
| FR-7 `speakerTurnCounts` pure, once per transcript | `lib/turns.ts::speakerTurnCounts`; `TranscriptViewer.tsx` `useMemo` | `lib/turns.test.ts` `describe("speakerTurnCounts")` (5 cases) | ✓ |
| NFR-1/2 no timers, no deps | review | — | ✓ |

### F6 — 260909-project-speaker-roster

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 b1 absent → open/[]; b2 atomic save + round-trip; b3 malformed/oversized → open/[]; b4 file skipped by walkers; b5 `ROSTER_FILE_NAME` | `commands/roster.rs` (`read_roster_file` 64 KiB cap, `write_roster_file` temp+rename), `crates/vault/src/paths.rs::ROSTER_FILE_NAME`, `lib.rs` re-export + layout doc | `tests/roster.rs::a_project_that_has_never_had_a_roster_reads_as_the_open_roster`, `::a_saved_roster_round_trips_and_lands_in_the_project_folder`, `::an_unparseable_roster_file_...`, `::a_roster_file_beyond_the_read_cap_...`, `::a_saved_roster_never_surfaces_as_a_meeting_in_the_vault_listing` | ✓ |
| FR-2 b1 normalisation; b2 limits; b3 TS mirrors Rust | `roster.rs::normalize_names`, `validate_names`; `lib/roster.ts::normalizeRosterNames` | `tests/roster.rs::saving_trims_blank_and_case_insensitively_duplicate_names`, `::a_name_longer_than_two_hundred_characters_is_refused...`, `::more_than_five_hundred_names_are_refused...`; `lib/roster.test.ts` (same fixture) | ✓ code / **E3** (locale) / E9 (boundaries) |
| FR-3 project validation shared with chats, never creates, `not_configured` | `commands/chats.rs::project_dir` (`pub(super)`), `roster.rs` calls it | `tests/roster.rs::reading_a_roster_for_anything_that_is_not_a_project_is_refused` (10 bogus names incl. reserved, path syntax, unknown), `::saving_a_roster_never_creates_a_project`, `::with_no_meetings_root_configured_...`, `::saved_chats_still_list_after_the_project_lookup_is_shared_with_the_roster` | ✓ |
| FR-4 b1 tag select on unattributed turn → `onAssign`; b2 existing speaker (ruled: F5 semantics, label `Edit speaker for this turn`, chooser when N ≥ 2); b3 off-roster value shown; b4 Escape cancels, known buttons intact; b5 menu select `Attribute selection to a speaker`; b6 empty roster disabled option | `SpeakerNameField.tsx` (select branch), `SpeakerTag.tsx` (`roster` prop, blur rule for pickers), `SelectionSpeakerMenu.tsx` (`roster` prop, `commitDraft` blank guard) | `SpeakerNameField.test.tsx` (6 roster cases), `SpeakerTag.roster.test.tsx` (working-tree version: 11 cases incl. `All 3 turns of Maxim`), `SelectionSpeakerMenu.roster.test.tsx` (6 cases), `TranscriptViewer.roster.test.tsx::attributes a whole turn...`, `::attributes exactly the selected sentences...` | ✓ (E1 for HEAD) |
| FR-5 open mode / unsorted = today | `lib/roster.ts::pickerSource`, `RecordingPage.tsx:541` (`roster` only with a project) | `lib/roster.test.ts` `describe("pickerSource")`, `TranscriptViewer.roster.test.tsx` open-mode cases, unmodified `SelectionSpeakerMenu.test.tsx` / `TranscriptViewer.test.tsx`, `RecordingPage.roster.test.tsx::keeps the free-text name box on an unfiled recording` | ✓ |
| FR-6 breadcrumb button (project only), panel contents, save/close/alert, cancel | `RecordingPage.tsx:292-301,494-500`, `ProjectRosterPanel.tsx` | `RecordingPage.roster.test.tsx` (6 cases), `ProjectRosterPanel.test.tsx` (14 cases: group name, radios, Remove, Add via Enter/button, dedupe, seed + disabled, save payload, close after save, alert on refusal, cancel) | ✓ (E4) |
| FR-7 App loads once per project, degrades, skips unsorted, adopts returned view | `state/useProjectRoster.ts`, `App.tsx:647-650,756-760` | `state/useProjectRoster.test.ts` (7 cases incl. stale-response guard, refused save), `App.roster.test.tsx` (7 cases) | ✓ |
| FR-8 CLAUDE.md, docs/setup.md, vault layout doc | `CLAUDE.md` speaker paragraph + "in open mode", `docs/setup.md` "Project speaker roster", `crates/vault/src/lib.rs:52-53` | — (docs; verified by review) | ✓ (**E2**: em dash corrupted on line 14) |
| NFR-1 one file read, no second walk | `read_project_roster_handler` (single `metadata` + `read_to_string`); sibling scan reused via `projectSpeakers` | review | ✓ |
| NFR-2 no service / index / `speakers.json` change | no Python or `transcript.json` change in F6 | review | ✓ |

## Positive notes

- **Security posture is clean across all three payloads.** Every new SQL fragment binds through `?` placeholders (the `{}` in `_TAGGED_CHUNKS`/`_TAGGED_DOCS` only ever receives `_holders()` output); the roster commands reuse `chats.rs`'s validation byte-for-byte instead of copying it, keep the existence check ahead of canonicalisation so a missing project is `invalid_argument` and never created, cap the read at 64 KiB, and write temp+rename; `SearchRequest.speaker` is length-bound and normalised; the MCP tool degrades to the existing "not built yet" message on a stale schema instead of querying a schema it does not understand.
- **The F3 signal is honest end to end.** `_set_phase` is the single writer; the `tokens / max_tokens` figure no longer reaches `JobStatus` because `_summarize_sync` simply stops passing `on_progress`; the pyannote hook accepts `file=` and swallows unknown kwargs so a future pyannote cannot fail a diarization pass through its progress reporting; `snapshots_equal_ignoring_created_at` compares `phase`, which is what keeps a summarize job's labels flowing to the UI. The fakes (`FakeDiarizer` `blocked`/`release`, the gated provider/LLM) synchronise on `threading.Event` with a loud timeout — no sleeps anywhere.
- **F6's `SpeakerNameField` absorbed the input/select split so that F4 and F5 could land in the same two files without knowing about rosters**; `pickerSource` is the one place a mode becomes "select vs datalist". `SpeakerTag`'s blur rule deliberately refuses to commit a picker's blur (the Escape-unmounts-the-select hazard the test wave flagged) — keep that.
- **F4's placement maths is a pure function with hardcoded expectations**, the hook measures exactly once per anchor/resize, and removing the CSS transform is documented in the rule itself so nobody re-adds a double shift.
- **F1's helper guards the vault-less fallback by `resolve()` equality**, so a hand-written `index_db_path` pointing at the app-dir file is never deleted, and the `OSError` branch is provoked with a real directory-in-place-of-file rather than a patched `unlink`.
- **F2 keeps the no-filter path byte-identical** (`_speaker_keys(None)` short-circuits before any SQL changes), scopes `known_speakers` to the chat's project so a name known only elsewhere stays an ordinary word, and reads the known set inside the same `run_serial` callable as retrieval — the index handle is still touched only on the serial executor.

## Round 2 — verification of the round-1 fixes

Scope: the `<fix-scope>` files only — commits `4e91bc1` (E1: four stale desktop test files) and `6e7e58e` (E2/E3/E4/E6: `CLAUDE.md`, `lib/roster.ts` + test, `ProjectRosterPanel.tsx` + test, `search/index_db.py` + `tests/test_legacy_index_cleanup.py`). `git show --stat` confirms neither commit touches anything outside that list; `git status` shows no working-tree edits left under `apps/`, `services/` or `crates/` (only `specs/` state files). Evidence this round: `uv run pytest tests/test_legacy_index_cleanup.py` (12 cases, exit 0); `vitest run` over the six touched desktop files (106/106 green at HEAD, no working-tree edits needed); `eslint` over the four changed TS files (exit 0 — the setState-during-render pattern in the panel trips no rule). The round-1 narrative above is left as written; only the status tags and the verdict table were updated.

| Finding | Claim | Verdict | Evidence |
|---|---|---|---|
| E1 | fixed | **fixed** | `4e91bc1` commits exactly the four adjudicated edits: `SpeakerTag.test.tsx` asserts focus *before* the Enter that fires the wide choice; `activeJob.test.ts` adds `phase: null` to both whole-view `toEqual`s; `RecordingPage.test.tsx` and `SpeakerTag.roster.test.tsx` query `Edit speaker for this turn` and, for the wide-scope roster pick, pass `turnsHeld: 3` and click `All 3 turns of Maxim` (the F5-over-F6 ruling from round 1). All six files green at HEAD. |
| E2 | fixed | **fixed** | `diff` of `CLAUDE.md` lines 10–18 against `0cdd13d` is empty; `od -c` on line 14 shows the em dash back as `E2 80 94`. The remaining `CLAUDE.md` delta is the two intended F6 paragraphs (plus prettier's `*created*` → `_created_`, already present and not flagged in round 1). |
| E3 | fixed | **fixed** | `roster.ts:41,58,61` use `toLowerCase()`; `ProjectRosterPanel.tsx:140` (the React key) follows. No `toLocaleLowerCase` call remains under `apps/desktop/src` outside the new test's spy. The regression case plays a Turkish host by mocking `String.prototype.toLocaleLowerCase` (`I`→`ı`) and asserts `["Ilya","ilya"]` collapses to `["Ilya"]` and that `removeRosterName` matches across the casing; the spy is restored in `afterEach`. With the old code the first assertion yields two names, so the test discriminates. |
| E4 | fixed | **fixed** | `ProjectRosterPanel.tsx:61-78`: the draft is seeded from the prop, `seededFrom` remembers the prop's identity (`mode` + names joined on NUL), and on a prop change the draft is re-seeded only while `identityOf(draft) === seededFrom`. This is React's documented "adjust state on prop change during render" form — the guard makes the second render converge, and once the operator has touched the draft a late read no longer overwrites it. Both new cases (`adopts the roster that settles after the panel was opened` — Save then sends the persisted `{roster, [Anna, Maxim]}`; `keeps the names the operator already typed when the roster lands late`) pin exactly the two branches. `RecordingPage.tsx:496` still passes `projectRoster ?? EMPTY_ROSTER`, so the hook's in-flight `EMPTY_ROSTER` is what the panel now safely outgrows. |
| E5 | accepted | **accepted** | `types.ts:80` still `phase?: string \| null`. Round 1 recommended accepting: the wire always carries the key and every reader is defensive. |
| E6 | fixed | **fixed** | `index_db.py:194-205`: `existed = legacy.exists()` precedes the three-suffix unlink loop; `return existed` after it; the `OSError` branch still returns `False` with the warning; `app.py:153` therefore still logs `legacy_index_removed` only when the main file was there. New case `test_sidecars_orphaned_without_their_main_file_are_swept_too` seeds only `-wal`/`-shm`, asserts `removed is False` and both gone. Docstring updated to describe the three-file unit. Tests 1/3 keep their meaning as predicted. |
| E7 | accepted | **accepted** | Unreachable through the index (`upsert_doc` strips blanks); robustness only. |
| E8 | accepted | **accepted** | Blueprint permits the live input; guards make the bypass non-corrupting. |
| E9 | accepted | **accepted** | Boundary/default-decoding coverage gaps; no code defect. |

**New findings introduced by the fixes: none.** Checked specifically: the panel's render-time `setState` pair cannot loop (the `persisted !== seededFrom` guard is satisfied after one re-render) and passes lint; `identityOf`'s NUL separator cannot collide with a name the Rust side would accept; `roster.test.ts`'s prototype spy is scoped to its `describe` and restored; the E6 helper's behaviour on a directory-in-place-of-file (the round-1 OSError case) is unchanged (`exists()` is `True`, unlink raises, `False` returned, warning logged).

Round-2 verdict: 0 blockers, 0 majors, 0 open minors — 5 fixed, 4 accepted.
