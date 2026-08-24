---
slug: transcript-language-selection
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 2
---

# Evaluation report: Transcript language follows the recording (Russian or English)

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 1 | 0 |
| minor | 0 | 1 | 0 |

The diff implements every FR and NFR of the spec, with tests at every seam: pydantic-level HTTP validation before any ledger row (NFR-3 verified by test), constrained {ru, en} detection applied before both decode paths, explicit-language passthrough with a spy provider, the ledger corrected to the actual decode language via a COALESCE guard that provably leaves LLM rows alone, exact-contract IPC validation in the Tauri handler, wire-body tests proving the override is carried and Auto omits the field, and the UI control plus indicator with the legacy-null case tested exactly as the acceptance criterion words it. Round 2: both round-1 findings are verified fixed in code and pinned by tests; pytest 443 passed / 2 skipped, ruff and mypy clean. No open findings.

## Findings

### E1 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/providers/local_whisper.py:305` (round 1); fix in `_detection_audio` (`local_whisper.py:160-187`) and the call site (`:370-372`)
- **Spec ref**: FR-1 (constrained auto-detection is the fix for mis-detected recordings); FR-4 (`language_probability` becomes F3's ground truth)
- **Expected**: The constrained detection should be at least as accurate as the unconstrained detection it replaces. In faster-whisper 1.2.1's `transcribe()` (the old auto path), VAD filtering runs *before* language detection — detection sees speech. The decode path here still uses `vad_filter=True` with tightened parameters.
- **Actual (round 1)**: `model.detect_language(audio=audio_input)` was called with the default `vad_filter=False`, so the language was chosen from the raw first ~30 s window — a recording opening with silence, hold music, or keyboard noise got a near-random ru/en pick with a meaningless probability.
- **Round-2 verification**: Fixed. `_detection_audio()` runs `get_speech_timestamps(prefix, VadOptions(**vad_parameters))` over a 10-minute prefix using the *same* `vad_parameters` dict shared with the decode call (verified: single dict built at `transcribe():347-350`, handed to both), assembles the speech via the real `collect_chunks`, and falls back to the raw prefix (pre-fix behaviour) when the VAD reports no speech — never an empty array, which faster-whisper's encoder rejects. The failure path is inside the existing try/except, so a detection blow-up still maps to a classified `ServiceError`. My round-1 suggested call shape (`detect_language(vad_filter=True, vad_parameters=<dict>)`) is confirmed **wrong for 1.2.1** and the fixer's rejection of it is upheld: `detect_language` (transcribe.py:1804) passes `vad_parameters` verbatim to `get_speech_timestamps`, which only converts kwargs to `VadOptions` when the option argument is `None` — a plain dict raises `AttributeError` at `vad_options.threshold`; and that path would also Silero-scan the whole file, breaking NFR-1's overhead budget (hence the bounded prefix). Pinned by four new tests: `test_auto_detection_detects_on_vad_filtered_speech` (detection sees exactly the speech chunk, decode still gets the unfiltered full waveform for timestamp mapping), `test_detection_vad_uses_the_same_tightened_parameters_as_the_decode_pass` (asserts a real `VadOptions` instance carrying the operator's `vad_min_silence_ms=700` and `speech_pad_ms=400`, equal to `decode_kwargs["vad_parameters"]`), `test_detection_falls_back_to_raw_audio_when_vad_finds_no_speech`, and `test_detection_vad_scans_only_a_bounded_prefix_of_a_long_recording` (prefix bound asserted, decode untouched by the budget). Only Silero itself is stubbed in these tests; the real `collect_chunks` runs.
- **Suggested fix (round 1, superseded)**: ~~Pass `vad_filter=True, vad_parameters=<the same tightened dict>` to `detect_language`~~ — not viable in 1.2.1, see above.

### E2 [minor] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/providers/local_whisper.py:146-157` (`_constrain_language` fallback branch)
- **Spec ref**: FR-4 acceptance: "`language_probability` is populated on auto-detected runs"
- **Expected**: Every auto run records a non-null `language_probability`.
- **Actual (round 1)**: When `all_language_probs` named neither `ru` nor `en`, the fallback returned `("en", None)`, so an auto run would write `language_probability: null`. Defensive branch, unreachable with real faster-whisper.
- **Round-2 verification**: Fixed exactly as suggested. The fallback now returns `_NO_EVIDENCE_PROBABILITY = 0.0` with a comment explaining the "no evidence" semantics, and `test_auto_detection_falls_back_to_english_when_neither_target_is_reported` asserts `result.language_probability == 0.0` alongside the forced-`en` decode.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 constrained auto | `local_whisper.py` (`_constrain_language`, `_detection_audio`, detection pass before either decode path) | `test_provider_local.py::test_auto_detection_picks_the_stronger_of_ru_en_even_when_another_language_wins`, `::test_auto_detection_constraint_applies_on_the_batched_pipeline_path`, `::test_auto_detection_falls_back_to_english_when_neither_target_is_reported`, plus the four E1 VAD tests; real-audio: `test_gpu_integration.py::test_auto_detection_decodes_in_the_spoken_language_on_cuda[en/ru]` (opt-in, self-skips) | ✓ |
| FR-2 explicit honored | `local_whisper.py` (explicit skips detection) | `test_provider_local.py::test_explicit_language_is_passed_through_without_any_detection[en/ru]`; `test_jobs.py::test_explicit_language_reaches_the_provider_and_is_recorded` (spy provider, transcript + ledger) | ✓ |
| FR-3 validation everywhere | `schema.py:139` (`Literal["ru","en"] \| None`), `config.py:_normalize_language` + `load_config` (covers config file, `TRANSCRIBER_LANGUAGE`, CLI override) | `test_api_jobs.py` (de/"" → 400 `invalid_request`, no ledger row, no job; ru/en/omitted → 202), `test_config.py` (8 cases incl. layering), `test_cli.py` (`--language de` → nonzero exit, allowed values on stderr; ru/en succeed) | ✓ |
| FR-4 trustworthy field | `local_whisper.py` (`language_out = decode_language`), `jobs.py:600-613`, `ledger.py:finish_succeeded` (`COALESCE(?, language)`) | `test_jobs.py::test_auto_language_job_records_the_decoded_language` (NULL at insert → corrected), `test_ledger.py` (update + LLM-row untouched), `test_provider_local.py` forced/auto probability tests (fallback now 0.0, never null) | ✓ |
| FR-5 app passes language | `commands.rs:1048`, `meetings.rs::validate_language` + handler, `jobs.rs` (`PendingWork::Filed{language}` → `SubmitRequest`, ingest arm stays `None`), `http.rs::SubmitBody` | `meetings.rs` handler tests (de/""/"EN"/"ru " rejected `invalid_argument`, nothing enqueued; ru/en/None reach the service), `jobs.rs` (filed carries language; ingest submits None), `http.rs` (wire body carries `"language":"en"`; Auto omits the key), `RecordingPage.test.tsx` (default Auto sends null) | ✓ |
| FR-6 language indicator | `RecordingPage.tsx` (pill from `transcript.language`, `LANGUAGE_NAMES` map) | `RecordingPage.test.tsx` (en → English, ru → Russian, null → no indicator and no placeholder, unknown code → nothing) | ✓ |
| NFR-1 no second decode | one `detect_language` call, waveform decoded once and reused for the decode pass; detection VAD bounded to a 10-min prefix | `test_provider_local.py::test_auto_transcribe_detects_language_exactly_once`, `::test_auto_run_decodes_the_audio_once_and_reuses_it_for_the_decode_pass`, `::test_detection_vad_scans_only_a_bounded_prefix_of_a_long_recording`, `::test_forced_run_hands_the_path_straight_to_the_model` | ✓ |
| NFR-2 no schema change | `TranscriptDoc` untouched; same `language`/`language_probability` fields | full pytest suite green incl. existing transcript consumers; `meetings.rs` parser untouched | ✓ |
| NFR-3 classified rejection | pydantic rejection rides existing `RequestValidationError → 400 invalid_request` handler, before `JobManager.submit` | `test_api_jobs.py::test_post_job_with_an_unsupported_language_is_rejected_before_any_ledger_row` (asserts empty ledger and empty job list) | ✓ |

FR-5's acceptance wording ("both the drag-drop ingest path and the Re-transcribe path") is satisfied under the plan's documented interpretation: the ingest path has no control (Q1), so its wire body legitimately omits `language` — asserted by `jobs.rs::the_drag_drop_ingest_path_still_submits_without_a_language` and `http.rs::submit_omits_the_language_key_entirely_when_the_choice_is_auto`. The in-app click-through was evidenced by the unbroken tested chain UI callback → `api.ts` → command handler → registry → `SubmitRequest` → HTTP body (every seam has its own test) plus a live wire check rather than GUI automation; for a single-operator internal tool with the acceptance criterion itself pointing at the `http.rs` submit-body tests, that evidence is sufficient.

## Positive notes

- The `COALESCE(?, language)` guard in `ledger.py:finish_succeeded` is exactly the right shape for the shared-with-LLM-jobs risk the plan flagged, and both sides of it are tested (`test_finish_succeeded_without_language_leaves_the_inserted_value`).
- Provider tests are behavior-first: spy on `decode_kwargs`/`detect_language_calls` rather than internals, including the batched-pipeline path and the "detection failure maps to a classified ServiceError, decode never runs" case — an error path the spec never asked for.
- The E1 fix is better than the round-1 suggestion: it caught that faster-whisper 1.2.1's `detect_language` does *not* convert dict `vad_parameters` (the type hint lies), shares the literal same dict between detection VAD and decode VAD so the two passes cannot drift, and bounds the extra Silero sweep to keep NFR-1's budget — each of those three properties has its own test.
- The Rust `validate_language` enforces the pinned IPC contract literally, rejecting `""`, `"EN"`, and `"ru "` — the handler test iterates those exact adversarial values and asserts nothing was enqueued (desktop-profile IPC checklist satisfied).
- Auto is the *absence* of the wire field, not `null` or `""`, and there is a dedicated test asserting the key is absent from the JSON body — precisely what keeps F2's constrained detection in charge by default.
- Reusing the `decode_audio` waveform for both detection and decode is a clean NFR-1 solution (one file read, one extra encoder window), with tests pinning both the single decode and the identity of the reused array; the decode pass still gets the unfiltered waveform so segment timestamps stay on the original timeline.
- UI details are careful: the picker travels with the button it modifies, has an accessible name, disappears with `has_source`, and the language pill uses `aria-label="Language: …"` with the legacy-null and unknown-code cases both rendering nothing rather than a placeholder — FR-6's acceptance criterion implemented to the letter.
- `tests/data/README.md` documents the new EN/RU sample env vars, the model-path subtlety, and the CUDA DLL gotcha — the GPU tests self-skip with messages naming the missing env var.
