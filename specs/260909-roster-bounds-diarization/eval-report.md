---
slug: 260909-roster-bounds-diarization
base_ref: f4d69bb305844d9123aee9cddbcb14f90a59f3a5
round: 2
---

# Evaluation report: Strict roster mode bounds speaker identification and hides unmatched voices

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 2 | 0 |
| minor | 2 | 2 | 0 |

The diff implements the blueprint's data flow end to end and every wave's tests pass (service: `test_diarizer_bounds.py`, `test_jobs_speaker_bounds.py` plus the two neighbouring suites; shell: `roster_bounds` 12, `speaker_bounds_wire` 5, `strict_threshold_config` 9, `job_phase` 9; UI: 80 tests across the five files). The wire contract is right (both fields skipped when `None`, bodies byte-identical to pre-feature otherwise), validation sits at the trust boundary before any ledger row, the roster read at submit time cannot escape the vault (canonical root + `ensure_inside` + a single `Normal` component), and the UI genuinely withholds generic labels in roster mode while keeping turn boundaries on the raw label. The operator's literal expectation — "in strict roster mode I never see a new person" — is met for a fresh drop and for a re-run of Identify speakers: the cap goes out on both submissions and whatever comes back as `Speaker N` renders as "Unnamed voice". Two things fall short: the *second half* of the feature (the lowered threshold pre-naming a known voice on a re-run) is silently blocked on any meeting the operator has already edited, because the viewer persists seeded `Speaker N` entries into that meeting's own `speakers.json` and `auto_assign_speakers` treats every existing entry as an operator decision (E2); and the one test that evidences FR-2 c6 is a race by construction whose stated premise is false (E1).

## Adjudications (test-wave mismatches)

- **422 vs 400.** The blueprint's "422" (FR-1 c5, FR-6 c4, T2, T6) is stale: `app.py::_validation_error_handler` has always answered `400 invalid_request` for every `RequestValidationError`, and nothing in this feature was meant to change that. Tests and docs correctly say 400. The blueprint text should be corrected; no code finding.
- **T5 case 5 fixture.** `Speaker 1` on adjacent segments 0 and 1 merges into one turn, so the blueprint's fixture cannot raise the scope question. The test's `{0:"Speaker 1",1:"Speaker 2",2:"Speaker 1",3:"Anna"}` exercises exactly the criterion (two turns of one generic voice). Correct adjudication; no finding.
- **T4 case 10 asserting only the second submission.** Asserting the first would be a race; asserting the second is the intent. However the test is still racy for a different reason — see E1.
- **Coverage gaps reported by the wave.** `export`/`index` refusal untested → E4. `set_meetings_root` round-trip → E5. The two `pub const`s are exercised indirectly (the registry's default is `DEFAULT_STRICT_SPEAKER_MATCH_THRESHOLD` and case 1 of `roster_bounds.rs` asserts `0.4` on the wire; the floor is asserted through `a_low_service_threshold_stops_at_the_strict_floor`) — no finding. The duplicated T1 case (`test_without_per_call_bounds_the_config_bounds_still_apply` vs `test_diarizer.py::test_speaker_bounds_are_passed_through_to_the_pipeline`) is harmless redundancy; noted, not a finding.
- **Implementer deviations.** `GENERIC_LABEL` reuse → E3 (minor, decision needed). Resolving the project via `ensure_inside` + `strip_prefix` is an improvement over the blueprint's literal "parent is a direct child of root" (it survives 8.3 short paths and junctions, which `tests/roster.rs` already had to handle) — accepted. The "one unreproduced cold-run lib failure" cannot be adjudicated from the artifact; E1 is the one racy test in the new set.

## Findings

### E1 [major] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/tests/roster_bounds.rs:378-403` (`a_backfill_job_still_queued_when_the_roster_grows_is_submitted_against_the_new_roster`)
- **Spec ref**: FR-2 c6 (roster read at submit time, not enqueue time)
- **Expected**: A deterministic test that holds the second backfill job un-submitted until the roster has been rewritten, then observes the submission.
- **Actual**: The test's premise — "The serial worker submits the second meeting only after the first has finished polling" — is false: `jobs.rs:747-753` (`submit_llm_and_poll`) does `tokio::spawn(poll_until_terminal(...))` and returns, so `worker_loop` submits job 2 immediately after job 1's submit, with only two `spawn_blocking` hops and a `FakeService::submit_llm` (instant) in between. The roster rewrite races that window from the test thread. It passes because the window usually loses to a synchronous file write; there is no synchronisation. When it loses, `submissions[1].max_speakers` is `Some(2)` (or `None` if `read_roster_file` sees a torn write) and the run is red. The blueprint anticipated this ("if the fake's timing cannot hold a job queued long enough...") but the fallback it offered has the same race.
- **Suggested fix**: Make the hold explicit. Either give `FakeService` a test-only gate on `submit_llm` (e.g. a `tokio::sync::Notify` / `oneshot` released by the test after `write_roster`), or enqueue the second meeting only after the fake has recorded the first submission *and* while the fake is blocked in `submit_llm` for it. Alternatively demote the case to what can be asserted deterministically (a roster written after `enqueue_*` returns but before the fake receives *any* submission — gated the same way) and fix the comment.

### E2 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/speaker_matching.py:221-233` (`auto_assign_speakers`: `if name and segment_id not in assignments`), in interaction with `apps/desktop/src-tauri/src/commands/meetings.rs:199-201` (`speakers: diarized` — the view is seeded with raw labels) and `apps/desktop/src/components/TranscriptViewer.tsx:80,124-127` (the viewer saves the whole map).
- **Spec ref**: FR-6 c2/c3 on the `diarize` path; FR-4's stated intent ("a generic label never counts as a named voice"); `docs/setup.md` post-release smoke ("a person you have named in another meeting of the same project should come back already named"); the Summary's "so a cluster that is *nearly* a known voice gets its roster name instead of a generic label".
- **Expected**: On a re-run of "Identify speakers" under a strict roster, the lowered threshold pre-names segments whose voice matches a sibling's named voice; only *operator* assignments are protected ("additive only — operator assignments always win").
- **Actual**: Any meeting the operator has opened and saved once carries `"<id>": "Speaker N"` for every untouched segment in its own `speakers.json` (the view seeds raw labels, the viewer persists the whole map). `auto_assign_speakers` treats every existing key as an assignment to preserve, so on the re-run none of those segments can be pre-named — the lowered threshold and the new embeddings are computed and then discarded. The UI still shows "Unnamed voice" (so the operator does not see a *new* person — the literal complaint is met), but the second half of the feature is inert on exactly the meetings an operator is likely to re-run it on, and the smoke listed in `docs/setup.md` fails there. FR-4 neutralised the *sibling* direction of the seeded-map problem; this is the *own-meeting* direction, which the blueprint's "Out of scope: stripping seeded generic labels on save ... FR-4 neutralizes the consequence instead" did not cover. Fresh drops are unaffected (no `speakers.json` yet), which is why T2's cases 10-11 pass.
- **Suggested fix**: In `auto_assign_speakers`, treat a generic existing assignment as absent using the `_is_generic` already introduced: `if name and (segment_id not in assignments or _is_generic(assignments[segment_id]))`. This keeps hand-given names untouched (they are never generic), changes no on-disk contract, and is consistent with FR-4. Add one T2-style case: a meeting whose `speakers.json` already maps a segment to `"Speaker 1"`, a sibling naming Anna at the near-miss cosine, a `diarize` job at `0.4` → the segment is pre-named "Anna".

### E3 [minor] [spec-drift] [status: fixed]

- **Where**: `services/transcription/src/transcription/speaker_matching.py:30,91-100` vs `apps/desktop/src/lib/turns.ts:233-235`
- **Spec ref**: FR-4 c1; Architecture ("the generic pattern is derived from `diarization.SPEAKER_LABEL_PREFIX`, one regex, one place"); Risks ("both derived from `SPEAKER_LABEL_PREFIX`; each site comments the mirror").
- **Expected**: One Python regex `^Speaker \d+$` derived from `SPEAKER_LABEL_PREFIX`, mirrored exactly by `isGenericSpeakerLabel`.
- **Actual**: Python reuses `search/speakers.py::GENERIC_LABEL` (`^speaker[ _]?\d+$`) on `name.strip().casefold()`, so it also treats `speaker 2`, `Speaker_2`, `SPEAKER_00` and ` Speaker 2 ` as generic; the TS side matches only `Speaker <digits>` exactly. The two "mirrors" therefore disagree on a hand-typed `speaker 3` (a person in the UI, not a voiceprint in the service). The comments on both sides claim they mirror each other, and `turns.ts` references a `_GENERIC_LABEL` that does not exist. Practically harmless (nobody names a person `speaker 3`), and the broader Python match is arguably safer for voiceprints, but it is a drift from an explicit design decision and the comments are now wrong.
- **Suggested fix**: Either (a) revert to the blueprint's local `_GENERIC_LABEL = re.compile(rf"^{re.escape(SPEAKER_LABEL_PREFIX)}\d+$")` without casefold, or (b) keep the reuse and make it a recorded decision: fix the `turns.ts` comment to name `_is_generic` / `GENERIC_LABEL` and state that the service side is deliberately broader. Operator's call.

### E4 [minor] [correctness] [status: fixed]

- **Where**: `services/transcription/tests/test_jobs_speaker_bounds.py:273-296` (`test_speaker_tuning_is_refused_on_a_job_that_never_diarizes`)
- **Spec ref**: FR-1 c5, FR-6 c4 ("either bound / the field on a `summarize` / `export` / `index` job")
- **Expected**: The refusal is evidenced for all three non-diarizing job types.
- **Actual**: Only `summarize` is parametrised. The validator (`schema.py:180-190`) is job-type-generic and `index`'s extra "no paths" rule runs *after* it, so the code is fine — but `export` and `index` are untested acceptance criteria.
- **Suggested fix**: Parametrise the job type (`summarize`, `export`, `index` — the `index` body carries no paths) in the same test.

### E5 [minor] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/tests/strict_threshold_config.rs:119-145`
- **Spec ref**: FR-7 c1 ("preserved across every load → modify → save round-trip (`set_diarization`, `set_meetings_root`)")
- **Expected**: Both named round-trips are evidenced.
- **Actual**: Only `set_diarization` is tested. `set_meetings_root` (`config.rs:342`) mutates the same `Settings` and calls the same `save`, and the field is typed with `skip_serializing_if`, so it does preserve the key — but the criterion names it and it is untested.
- **Suggested fix**: One more case calling `set_meetings_root(dir, app_dir, &mut settings, <abs path>)` on settings loaded from a file holding the strict key, asserting the key survives on disk.

### E6 [minor] [correctness] [status: fixed]

- **Where**: `apps/desktop/src/lib/turns.ts:240-242` (`isGenericSpeakerLabel`, widened in round 2 to `/^speaker[ _]?\d+$/i`); `apps/desktop/src/lib/turns.test.ts` (no case), `apps/desktop/src/components/TranscriptViewer.unattributed.test.tsx:195` (only the two negatives)
- **Spec ref**: FR-3 c1 / FR-4 c1 ("one regex, one place", mirrored on both sides); `testing-toolkit:testing-best-practices`
- **Expected**: The family the UI now hides in roster mode — `SPEAKER_00`, `speaker_2`, `Speaker_3`, mixed case — is evidenced by at least one positive on the TS side, the way `test_speaker_matching.py` evidences it on the Python side.
- **Actual**: Every TS positive is still `Speaker 1` / `Speaker 2`; the widening is only pinned by the two negatives (`Speaker`, `Speaker 7b`). Reverting the regex to the round-1 `^Speaker \d+$` leaves every TS test green, so the "mirror" claim in the comment is unevidenced where it changed. Introduced by the E3 fix; nothing behavioural is wrong.
- **Suggested fix**: A `describe("isGenericSpeakerLabel")` in `turns.test.ts` with the positives `SPEAKER_00`, `speaker_2`, `Speaker_3`, the negative ` Speaker 2` (the UI does not trim) and the existing negatives, so the two sides' agreement is asserted rather than described.

## Round 2 — verification of the fixes

Scoped to `<fix-scope>`: `service/fake.rs`, `tests/roster_bounds.rs`, `speaker_matching.py` + `tests/test_speaker_matching.py`, `tests/test_jobs_speaker_bounds.py`, `lib/turns.ts`. Targeted runs: `roster_bounds` 12/12 (plus the c6 case alone 5 more times, all green), `test_speaker_matching.py` + `test_jobs_speaker_bounds.py` 28/28, `TranscriptViewer.unattributed.test.tsx` 12/12.

| Finding | Claim | Verdict | Evidence |
|---|---|---|---|
| E1 | fixed | **fixed** | The hold is a real happens-before, not a wider window. `hold_llm_submissions` arms a `Semaphore::new(0)` *before* `build_state` (so before the worker exists); job 1's `submit_llm` records its request under `inner`, drops the guard, then parks on `acquire()`. `worker_loop` (`jobs.rs:564-568`) awaits `process_one` per job and `submit_llm_and_poll` (`jobs.rs:707-753`) awaits `service.submit_llm` before returning, so job 2's `roster_bounds` cannot run until job 1's call returns — which only happens on `release_llm_submissions` (`Semaphore::close` wakes every waiter and fails every later `acquire`). The test's `write_roster` is a synchronous write on the test thread *before* `release`, so job 2's read is ordered after it by the semaphore, and `held.len() == 1` is likewise deterministic (only the serial worker calls `submit_llm`). The case still discriminates enqueue-time from submit-time: both jobs were enqueued under the 2-name roster, and only a submit-time read can produce `Some(3)` for job 2. Every other user is untouched: unarmed, `llm_gate` clones to `None` and the call returns as before; dev mode (`lib.rs:120`, `FakeService::new`) never arms it; `e2e_flow.rs`, `roster.rs`, `job_phase.rs`, `speaker_bounds_wire.rs` and `tests/common` never call the two new methods. No `.await` while a `std::sync::Mutex` guard is held (the `async_trait` future stays `Send`). |
| E2 | fixed | **fixed** | `auto_assign_speakers` (`speaker_matching.py:248`) now fills a segment when it is absent **or** its existing value is generic. The invariant holds on both sides: a real operator name is never generic, so `_is_generic(assignments[id])` is `False` and the branch is skipped (`an-operator-name-wins` row of the jobs test and the `"1": "Кто-то другой"` half of the unit test); and no generic label can propagate because `matches` comes only from `collect_project_voiceprints`, which drops generic names at `:152`, and `jobs.py:1053` is the sole caller. `_load_assignments` (`:71-81`) pre-filters values to non-empty strings, so the new `.strip()` can never see a non-string. Docstrings corrected. One behavioural consequence, judged acceptable and consistent with FR-4: a segment the operator *deliberately* relabelled to another generic label (a hand merge of `Speaker 1` into `Speaker 2` in open mode) is now also eligible for a recognized real name — strictly more information, and the blueprint's "a generic label never counts as a named voice" covers it. |
| E3 | fixed | **fixed** | Resolved by option (b) in spirit: instead of narrowing Python, the TS side was widened to the same family (`/^speaker[ _]?\d+$/i` vs `GENERIC_LABEL = ^speaker[ _]?\d+$` on `casefold()`), and both comments now name the real symbols and state the one deliberate difference (the service trims, the UI does not). The negatives still hold (`Speaker` has no digits, `Speaker 7b` has a trailing letter). On "can it hide a name a person typed": in roster mode the name controls are a strict pick-list over roster names, so a hand-typed `speaker 2` can only reach a strict-roster meeting from an earlier open-mode session or from a roster whose *names* contain it; in both cases the service already refuses it as a voiceprint, the assignment stays on disk unchanged (only the display and the reuse list are affected), and the pick-list still offers the roster name. Acceptable. New coverage gap on the TS side → E6. | **Superseded (round 2, late):** E3 was ultimately resolved the other way — the Python predicate was narrowed to `^Speaker \d+$` (from `SPEAKER_LABEL_PREFIX`) and the TS regex restored to `/^Speaker \d+$/`; both sides are now exact and identical, and E6 is pinned against that form.
| E4 | fixed | **fixed** | `test_speaker_tuning_is_refused_on_a_job_that_never_diarizes` is now `summarize x export x index` by `bounds x threshold`, and `error_message == "request validation failed"` matches `app.py:224`'s handler literally — so the `index` row (no paths) can only pass through the schema validator, not through `submit`'s "no vault root" refusal. FR-1 c5 / FR-6 c4 fully evidenced. |
| E5 | open | **still open** | Unchanged; `set_meetings_root` round-trip of `speaker_match_threshold_strict` remains untested. Minor. |
| E6 | — | **new, minor** | The widened TS regex has no positive test for what it was widened to; see the finding. |

No new blocker or major. The fake's gate is the only new non-test code surface and it is test-only by construction (`None` unless a test arms it).

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 c1 (fields ≥ 1, absent ⇒ as today) | `schema.py:172-174`; `diarizer.py:372-378` | `test_diarizer_bounds.py` (cases 1, 3, 4); `test_jobs_speaker_bounds.py::test_a_job_without_bounds_still_drives_an_engine_that_takes_none` | ✓ |
| FR-1 c2 (independent fallback) | `diarizer.py:372-373` | `test_diarizer_bounds.py::test_a_per_call_bound_overrides_its_config_key_and_leaves_the_other_one` | ✓ |
| FR-1 c3 (exact kwargs on both pipeline calls) | `diarizer.py:374-378, 389-391` | `test_diarizer_bounds.py` cases 1, 5 | ✓ |
| FR-1 c4 (both entry points) | `jobs.py:713` (`_diarize_segments`), `jobs.py:1040` (`_diarize_existing_sync`), `_speaker_bounds` `jobs.py:663-678` | `test_jobs_speaker_bounds.py::test_a_transcribe_job_hands_only_the_bound_it_was_given_to_the_engine`, `::test_a_diarize_job_hands_both_of_its_speaker_bounds_to_the_engine` | ✓ |
| FR-1 c5 (invalid ⇒ 400, no ledger row) | `schema.py:176-198` | `::test_out_of_range_speaker_tuning_is_refused_and_leaves_no_job_behind`, `::test_speaker_tuning_is_refused_on_a_job_that_never_diarizes` (summarize x export x index, pinned to the schema validator) | ✓ (round 2) |
| FR-1 c6 (kwargs omitted, not `None`) | `jobs.py:663-678` | `::test_a_job_without_bounds_still_drives_an_engine_that_takes_none` (plain `FakeDiarizer`) | ✓ |
| FR-2 c1 (strict N ⇒ `max_speakers: N`, no `min`) | `roster.rs:247-279`; `jobs.rs:229-238, 660-668`; `http.rs:175-181, 704-705` | `roster_bounds.rs` cases 1, 3; `speaker_bounds_wire.rs` case 1 | ✓ |
| FR-2 c2 (no bound: unsorted/open/missing/unreadable/empty) | `roster.rs:248-279` | `roster_bounds.rs` cases 2, 4, 5, 6; `speaker_bounds_wire.rs` case 2 | ✓ |
| FR-2 c3 (`diarize` job, Identify speakers + backfill) | `jobs.rs:715-724`; `http.rs:197-203, 825-826` | `roster_bounds.rs` cases 7, 10; `speaker_bounds_wire.rs` case 4 | ✓ |
| FR-2 c4 (summarize/export/index unchanged) | `jobs.rs:893-901`; `commands/llm.rs:75-76`; `submit_index_quiet` untouched | `roster_bounds.rs` case 9; `speaker_bounds_wire.rs` case 5 | ✓ |
| FR-2 c5 (re-transcribe via `enqueue_filed`) | `jobs.rs:657-668` (single submit point) | `roster_bounds.rs` case 8 | ✓ |
| FR-2 c6 (read at submit, not enqueue) | `jobs.rs:229-238` (`Shared::roster_bounds` in `process_one` / `submit_llm_and_poll`) | `roster_bounds.rs` case 10 (gated by `FakeService::hold_llm_submissions`) | ✓ (round 2) |
| FR-3 c1 ("Unnamed voice", unassigned style) | `SpeakerTag.tsx:69-71, 218, 224`; `turns.ts:240-242` | `TranscriptViewer.unattributed.test.tsx` cases 1, 2, 9 (`Speaker N` positives, `Speaker` / `Speaker 7b` negatives) | ✓ for `Speaker N`; gap for the widened family (E6) |
| FR-3 c2 (pick-list on placeholder, roster names only) | `SpeakerTag.tsx:72, 166-172, 221`; `SpeakerNameField.tsx` (unchanged) | case 4 | ✓ |
| FR-3 c3 (turn boundaries on raw label) | `groupIntoTurns` untouched; `TranscriptViewer.tsx:89-91` | case 3 | ✓ |
| FR-3 c4 (scope choice, "All N turns of this voice", rename-everywhere) | `SpeakerTag.tsx:118-122, 189`; `commit`/`renameEverywhere` unchanged | cases 5, 6 | ✓ |
| FR-3 c5 (reuse buttons + selection popover exclude generic) | `TranscriptViewer.tsx:108-113` (one filtered `known` for both) | cases 7, 8 | ✓ |
| FR-3 c6 (real names unchanged; open/unfiled unchanged) | `SpeakerTag.tsx:69` (`roster !== undefined` guard); `RecordingPage.tsx:541`; `lib/roster.ts::pickerSource` | cases 2, 10, 11 | ✓ |
| FR-4 c1 (generic assignment ⇒ no voiceprint) | `speaker_matching.py:92-105, 152` | `::test_a_sibling_that_named_a_voice_only_generically_pre_names_nothing` | ✓ (E3 resolved: both sides share `GENERIC_LABEL`'s family) |
| FR-4 c2 (real name still pre-names) | unchanged path | `::test_a_sibling_that_named_a_voice_anna_still_pre_names_it` | ✓ |
| FR-5 (docs) | `services/transcription/README.md:78-79, 327-361, 373-375, 394-400`; `docs/config-contract.md:72-94`; `docs/setup.md:283-303`; `CLAUDE.md:59` | n/a (docs) | ✓ — all four say 400, describe the derivation, the smoke and roster-agnosticism |
| FR-6 c1 (threshold field `[0,1]`, absent ⇒ config) | `schema.py:174`; `jobs.py:680-685` | `::test_a_near_miss_voice_stays_unnamed_under_the_configured_threshold` | ✓ |
| FR-6 c2 (both naming paths) | `jobs.py:826, 1057` | `::test_a_near_miss_voice_is_named_when_the_job_lowers_the_threshold`, `::test_a_diarize_jobs_threshold_reaches_its_naming_pass_too` ; `::test_a_re_run_pre_names_over_a_seeded_label_but_never_over_a_real_name` (both rows); `test_speaker_matching.py::test_auto_assign_replaces_a_seeded_generic_label_but_not_a_real_name` | ✓ (round 2) |
| FR-6 c3 (0.45 not named at 0.5, named at 0.4) | as above | same pair | ✓ |
| FR-6 c4 (1.5 / −0.1 / non-diarizing job ⇒ 400) | `schema.py:174, 180-190` | refusal tests (threshold rows) | ✓ (export/index gap shared with E4) |
| FR-6 c5 (no echo in `JobStatus`) | `JobStatus` untouched | `::test_the_job_status_echoes_neither_the_bounds_nor_the_threshold` | ✓ |
| FR-7 c1 (typed key, never `null`, round-trips) | `config.rs:131-141, 159` | `strict_threshold_config.rs` cases 1, 2, 7, 8, 9 (`set_diarization`) | gap (E5: `set_meetings_root`, still open) |
| FR-7 c2 (resolver: explicit / derived / floor / out-of-range ignored) | `config.rs:46-64, 272-300` | `strict_threshold_config.rs` cases 1-6 | ✓ |
| FR-7 c3 (threshold iff `max_speakers`) | `jobs.rs:237` (`cap.map(...)`) | `roster_bounds.rs` cases 1, 2, 4-7, 9; `speaker_bounds_wire.rs` case 3 | ✓ |
| FR-7 c4 (resolved at startup via `AppState::new_with`) | `commands.rs:456-460`; `jobs.rs:190-196, 321-323, 371-384` | `roster_bounds.rs` cases 11, 12 | ✓ |
| NFR-1 (model/GPU/network-free) | new tests import only `test_diarizer` stubs, `fakes`, `FakeService`, `wiremock` | the targeted runs complete in seconds | ✓ |

## Security / performance / strict-skill review

- **Boundary validation**: all three tuning fields are typed and range-checked by pydantic before `submit` (`Field(ge=1)`, `Field(ge=0.0, le=1.0)`, `model_validator` for `min ≤ max` and job type); `JobManager.submit` re-documents that it relies on it. `GET /v1/jobs/{id}` is unchanged. Nothing new is echoed, logged or persisted (the ledger row does not carry the tuning; `reconcile_interrupted` fails queued jobs on restart, so nothing needs resuming).
- **Roster read at submit time**: `roster_speaker_cap` canonicalises the root, runs `ensure_inside` (lexical-then-canonical containment) on the meeting dir, requires exactly two relative components with the first `Normal`, and reads `canonical_root.join(project)/roster.json` — a path that cannot leave the vault. `unsorted` is excluded case-insensitively; a planted `unsorted/roster.json` is tested to have no effect. Every failure degrades to `None` (no bound, no threshold), never an error. The function is `pub` but not a Tauri command; the meeting dirs reaching it come from ingest or from handlers that already ran `ensure_inside`.
- **No new IPC surface**, no shell execution, no secrets; `speaker_match_threshold_strict` is a tuning number, not a secret. `f64` on the wire cannot be NaN (`strict_speaker_match_threshold` filters `is_finite` on both inputs).
- **Performance**: one ≤64 KiB file read per submission inside `spawn_blocking`; the `StdMutex<f64>` is copied out before any `.await`. Nothing else changes hot paths; the UI filter runs once per transcript in a `useMemo`.
- **`testing-toolkit:testing-best-practices`**: honoured — every new test observes an edge (stubbed pipeline kwargs, wiremock bodies, `FakeService` submissions, `config.json` on disk, rendered DOM, the saved `speakers.json`); `FakeDiarizer` is untouched and the recording subclass lives in the test file; `job_phase.rs` got exactly the two-line fixture edit. E1 is the one place the practice slips (a timing assumption instead of synchronisation).
- **`frontend-toolkit:internal-ui`**: not installed; the UI change reuses the existing `unassigned` idiom and adds no new visual language, as the blueprint prescribed.

## Positive notes

- The two fill points in `jobs.rs` are the *only* places a transcribe/diarize body is built, so fresh drop, re-transcribe, Identify speakers and the backfill all pass through the same roster read — the blueprint's single-submit-point argument holds in the code, and `commands/llm.rs` / `commands/speakers.rs` correctly leave the fields `None`.
- `_speaker_bounds` omits unset keys rather than passing `None`, and case 3 of T2 proves it with the *unmodified* `FakeDiarizer` — the contract every existing engine and fake depends on.
- The threshold derivation clamps to `[0.2, 1.0]` rather than the blueprint's bare `max(0.2, base − 0.1)`: a global key above `1.0` (the README's "disable auto-naming" idiom) now yields a strict value the service will accept instead of a 400. Keep this.
- `roster_speaker_cap`'s use of `ensure_inside` + `strip_prefix` is more robust than the blueprint's literal parent-directory rule (8.3 short paths, junctions) and stays inside the existing path-handling vocabulary.
- The UI change is minimal and correct: `known` is filtered once for both controls, `groupIntoTurns` and the commit path are untouched, and `isGenericSpeakerLabel` stays anchored (the `Speaker` / `Speaker 7b` negatives are tested; since round 2 it shares `GENERIC_LABEL`'s family with the service).
- The wire tests assert whole bodies against literals (better diagnostics than `body_json` matchers, same pinning strength); the docs say 400, describe the derivation with worked numbers, and list the operator's smoke.
- Round 2: the `FakeService` gate is the right shape — it records the request *before* blocking (so `llm_submissions()` is a stable observation point), is `None` unless armed, and `close()` rather than `add_permits` means a release can never be "used up" by an unexpected extra call. Keep it as the pattern for any future ordering assertion against the serial worker.
- Round 2: the refusal test pinning `error_message` to the validator's literal is exactly the discipline that stops a case from passing for the wrong reason.
