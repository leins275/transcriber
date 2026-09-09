# Factory notes (Phase C.2/C.3) — carried into the consolidated quality gate

Known still-red tests after implementation, and test defects implementers were not allowed to fix:

## F5 per-turn-speaker-reassign
- `src/components/SpeakerTag.test.tsx` → "puts the wide choice one Tab away, and fires it from the keyboard": focus assertion placed AFTER the Enter act that unmounts the button. Fix: move `expect(wideChoice).toHaveFocus()` between `user.tab()` and `user.keyboard("{Enter}")`. Implementation is correct (19/20 green).
- `src/components/RecordingPage.test.tsx:228` queries `/rename maxim/i`; the blueprint retires that label for `Edit speaker for this turn`. Fix: change the query to `/edit speaker for this turn/i` (single-turn fixture → still commits via onAssign).

## F1 search-index-in-vault
- Done, 11/11; full service suite 680 passed. Unrelated brittle test observed: `tests/test_model_api.py::test_post_download_returns_202_immediately_without_blocking` asserts elapsed < 0.5 s and flaked once under full-suite load (passed on rerun). Not caused by this batch.
- Deliberate deviation: helper returns False before unlinking when the main legacy file is absent, so stray -wal/-shm without a main file are left in place.
- (F5 addendum) T3 implementer reports the SpeakerTag Tab+Enter case may also be a tab-order issue (input still in the ring ahead of the wide button), not only assertion placement — the fixer should verify both before deciding which side to change.

## F2 speaker-tagged-embeddings
- T1: `best_chunk_for` under a filter has NO first-chunk fallback (returns None) — T4 relies on every channel being filtered by the same key set so a filtered doc always has a matching chunk. `schema_stale` is True for a read-only open of an empty (user_version 0) file.

## F6 project-speaker-roster
- T1: `chats_dir` → `project_dir` split moved the `is_dir()` existence check AHEAD of `ensure_inside`; a non-existent project now errors `invalid_argument` (was `not_a_file`, contradicting its own doc). Behaviour change in chats.rs outside T1's strict scope — evaluator should judge.

## F3 job-progress-accuracy
- T6 deviation: `JobSnapshot.phase?: string | null` is OPTIONAL (blueprint said required) because seven untouchable test files build `JobSnapshot` literals without `phase`. Evaluator: accept, or a fixer adds `phase: null` to those seven builders and makes it required.
- T7 (pending): `activeJob.test.ts` has two whole-view `toEqual` assertions that break once `ActiveJobView` gains `phase` — fixer scope (implementer may not edit tests).
- Observed: full Python suite ~89 s on this machine vs the "<30 s" figure in CLAUDE.md; predates this batch.

## RESUME POINT (operator stopped 2026-09-09, factory phase C.2/C.3)
- impl_done + committed on branch: F1 (1c24771), F2 (see git log sdd/260909-speaker-tagged-embeddings), F4 (25c39de), F5 (2201c41). Not yet merged (C.4 pending).
- In flight when stopped (implementers were left running; they finish on their own in the worktrees, but nobody flips their headings to [x]):
  - F2 T5/T6: DONE and committed before the stop (729 passed).
  - F3 T4 (jobs.py summarize/export phases) — verify `pytest tests/test_llm_jobs.py -q`; if green, flip T4 [x], commit `feat(job-progress-accuracy): …`, set impl_done.
  - F6 T10 (App.tsx, CLAUDE.md, docs/setup.md) — verify `npm --prefix <wt>/apps/desktop run test` (expect only App.roster + siblings green) and `run type`; if green, flip T10 [x]; T11 is manual verification (no test file) — run `tauri dev` smoke or mark it for the consolidated UI validation; commit `feat(project-speaker-roster): …`, set impl_done.
- If any of those tests are still red on resume, re-run that task from scratch per the resume table.
- Then: C.4 merge in batch order F1→F6 (`git merge --no-ff sdd/<slug>`), C.5 consolidated quality gate (qa-run.sh auto + evaluator with test-wave.md mismatches + this file), C.6 UI validation gate.
- F2 note: schema.py, index_db.py, service.py were written with CRLF in the worktree (git normalized to LF on commit, so the branch content is LF). Harmless, but the gate's ruff format should be checked on the merged tree.

## UPDATE after session restart
- F3 T4 (jobs.py summarize/export phases) and F6 T10 (App.tsx + CLAUDE.md + docs/setup.md) implementers were STOPPED mid-task when the previous Claude Code process exited. Their partial edits may be on disk in the worktrees. On resume: run the task's test file first; if red or the heading is still [~], re-run the task from scratch per the resume table (an implementer prompt as before; it may build on the partial edits).

## C.4 integration (done)
- Merged in order: F1 014e673, F2 db026ff (fixer: index_db.py union; +1 blank line in test_search_speakers.py for I001), F3 93d4c62, F4 fa6632e, F5 2552561, F6 430ad50 (fixer: union of F4/F5/F6 in SelectionSpeakerMenu/SpeakerTag/TranscriptViewer; F5 wins the `Edit speaker for this turn` label and assign-vs-rename semantics).
- Desktop suite on merged tree: 550/558; 8 red, all stale tests (see below). Python 740 passed post-F2; re-verify on the full merged tree in C.5.
- Known stale tests to fix in C.5 (fixers may edit tests): (a) SpeakerTag.test.tsx Tab+Enter focus assertion order; (b) RecordingPage.test.tsx `/rename maxim/i` → `/edit speaker for this turn/i`; (c) activeJob.test.ts two toEqual need `phase: null`; (d) SpeakerTag.roster.test.tsx: 3 cases query `Rename <name>` → `Edit speaker for this turn`; case "renames the speaker throughout when another roster name is picked" must pass `turnsHeld: 3` and click `All 3 turns of Maxim` (or expect onAssign) — F5 semantics win.
