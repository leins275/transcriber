# Test wave notes — 260909-roster-bounds-diarization (for the evaluator)

Requirement corrections / adjudications needed:
- FR-1 c5 / FR-6 c4 say **422**; the app answers **400 `invalid_request`** for RequestValidationError. Tests (T2) assert 400. T6 docs must say 400. Blueprint text is wrong.
- T5 blueprint case 5 fixture (`Speaker 1` on adjacent segments 0,1) cannot produce a scope question (adjacent same-label segments merge into one turn); the test uses `{0:"Speaker 1",1:"Speaker 2",2:"Speaker 1",3:"Anna"}`.
- T4 case 10 (backfill re-reads roster at submit): only the SECOND job's submission is asserted (asserting the first is a race); enqueue-time read would still fail it.

Coverage gaps (report-only):
- T2: `export`/`index` job types with tuning fields → 400 is untested (only `summarize` parametrized).
- T7: the two `pub const`s are not asserted by tests (exist per blueprint); `set_meetings_root` round-trip of the strict key uncovered (only `set_diarization`).
- T1: `test_without_per_call_bounds_the_config_bounds_still_apply` duplicates `test_diarizer.py::test_speaker_bounds_are_passed_through_to_the_pipeline`.
- Float tolerance (1e-9 / 1e-12) used for derived thresholds (0.35−0.1 is one ulp below 0.25).

Immediately-green cases: all negative/unchanged-behaviour guards (no-bounds paths, open mode, unsorted, no-roster, summarize/export bodies, JobStatus echo, real-name voiceprint control, threshold-under-default control).

Implementer notes:
- T3: test uses full-body assert_eq on the captured request instead of wiremock `body_json` (same pinning strength, better diagnostics).
- T5: `known` list filtered in TranscriptViewer (roster mode) so SelectionSpeakerMenu is untouched; jsdom NaN-left warning from useClampedPosition pre-exists.

Implementer deviations (post-wave):
- T2 reused `search/speakers.py::GENERIC_LABEL` (casefolded, also matches `SPEAKER_00`) instead of a local regex in speaker_matching.py.
- T4 `roster_bounds.rs` case 10 is a race by construction (roster rewritten while the serial worker submits job 1); passed 5/5 here. One unreproduced lib failure on a cold run (name not captured), 6+ green runs since.
- T4 resolves the project via `ensure_inside` + strip_prefix (handles 8.3 short paths/junctions), excludes `unsorted` case-insensitively.
