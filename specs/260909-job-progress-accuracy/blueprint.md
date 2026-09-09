---
slug: 260909-job-progress-accuracy
created: 2026-09-09
status: approved
base_ref: <git sha, recorded at blueprint approval>
---

# Blueprint: Honest job progress for every job type

Feature F3 of the TRN 260909 batch. The evaluator judges against the FRs below;
the scheduler parses the task blocks below.

## Summary

Only the transcribe job drives its progress bar from a real signal (whisper's
`segment.end / duration`, `providers/local_whisper.py:419`). Every other job
type sets `job.progress` from hand-picked constants in
`services/transcription/src/transcription/jobs.py` — export `0.1 / 0.5`,
diarize `0.05 / 0.9`, transcribe-with-diarization scaled to `0.9` — or from a
number that only looks linear: summarize reports `generated tokens / max_tokens`
(`llm/llama_cpp_local.py:260`, cap = `llm_max_output_tokens + llm_think_headroom_tokens`
= 6144), so a typical 2 000-token completion climbs to ~30 % and then jumps to
100 %, after sitting at 0 % for the whole prompt-evaluation phase. That is the
"uneven / crooked" scale the operator sees.

The fix: `GET /v1/jobs/{id}` gains a `phase` label and a nullable `progress`.
`progress` is always **the current phase's real fraction** (whisper seconds,
pyannote step chunks, index docs) or `null` when the phase has no linear signal;
`phase` names the sub-step ("rendering PDF", "writing summary · 812 tokens",
"segmenting speech"). The Rust shell passes both through to `JobSnapshot`; the
job list shows the phase text, a real bar when there is a number, an
indeterminate track when there is not. No fake percentages anywhere.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` (Tauri 2), `[dependencies] tauri` in `apps/desktop/src-tauri/Cargo.toml`.
- `web` — `react` + `vite` in `apps/desktop/package.json` (the Tauri webview UI; internal-tool, single operator).
- `cli` — `[project.scripts]` in `services/transcription/pyproject.toml` (`transcription-service`, `transcriber-mcp`).

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri 2, Rust (lib crate `transcriber_desktop_lib` + integration tests) | `apps/desktop/src-tauri/Cargo.toml`, `src-tauri/src/lib.rs`, `src-tauri/tests/e2e_flow.rs` |
| UI | React + Vite + TypeScript, vitest, CSS modules | `apps/desktop/package.json`, `apps/desktop/src/components/JobRow.tsx` |
| Service | Python 3.12, FastAPI + CLI, `uv` only | `services/transcription/pyproject.toml`, `src/transcription/app.py`, `cli.py` |
| ML runtime | faster-whisper, llama-cpp-python (streaming), pyannote.audio 3.4 (optional `diarization` extra) | `services/transcription/uv.lock`, `src/transcription/llm/llama_cpp_local.py`, `diarizer.py` |
| Job engine | one serial worker, in-memory `JobState`, sqlite ledger | `src/transcription/jobs.py`, `ledger.py` |
| Vault rules | Rust library | `crates/vault/` |
| Tests | pytest (model/GPU/network-free by default), vitest, cargo test (inline `#[cfg(test)]` + `tests/`) | `services/transcription/tests/`, `apps/desktop/src/**/*.test.ts*`, `apps/desktop/src-tauri/tests/` |

Makefile QA targets present: format, lint, type, test (no aggregate `qa`).

## Requirements

- **FR-1** (must): The job status contract carries honest progress. `GET /v1/jobs/{id}` answers `progress: float | null` and `phase: string | null`.
  - [ ] `progress` is `null` whenever the running phase has no linear signal; when numeric it is the *current phase's* real fraction clamped to `[0, 1]`.
  - [ ] `phase` is a short lowercase display label of the current sub-step, or `null` while queued, while the headline verb already says it all (a transcribe job's decode), and in every terminal state.
  - [ ] A queued job answers `progress: 0.0, phase: null`; a succeeded job (in memory or reconstructed from the ledger) answers `progress: 1.0, phase: null`.
  - [ ] `services/transcription/README.md` documents `progress`/`phase` semantics per job type.
- **FR-2** (must): Summarize reports phases instead of a token/cap percentage.
  - [ ] While the job runs, `progress` is `null` (no bar); the `0.95`-capped `tokens / max_tokens` figure never reaches the job status.
  - [ ] Before the first streamed token of a call the phase reads `reading transcript`.
  - [ ] From the first token on, the phase counts streamed pieces: `writing summary · N tokens` (single-call summary) or `summarizing part k/n · N tokens` (map-reduce; `n` grows with the actual call count, never shrinks, same monotone denominator as today's `planned_calls`).
  - [ ] On success `progress` is `1.0` and `phase` is `null`; `summary.md` and the reasoning sidecar are written exactly as before.
- **FR-3** (must): Export reports its two phases and no constants.
  - [ ] Phase `writing export.md` while `build_export_md` runs, then `rendering PDF` while `render_pdf` runs; `progress` is `null` throughout and `1.0` on success.
  - [ ] The `0.1` / `0.5` assignments are gone from `_export_sync`.
- **FR-4** (must): The pyannote engine reports real step progress through pyannote's pipeline `hook`.
  - [ ] `PyannoteDiarizer.diarize(audio_path, *, cancel, on_progress=None)` accepts an optional `Callable[[str, float | None], None]` reporting `(phase, fraction)`; omitted, behaviour is unchanged.
  - [ ] It reports `("loading speaker model", None)` before `_ensure_pipeline` and `("decoding audio", None)` before `_decode`.
  - [ ] The pipeline is called with a `hook` (on the `return_embeddings=True` call and on the `TypeError` retry alike) that maps pyannote's steps: `segmentation` → `segmenting speech`, `speaker_counting` → `counting speakers`, `embeddings` → `extracting voice embeddings`, `discrete_diarization` → `assigning speakers`; an unknown step is passed through with underscores turned to spaces.
  - [ ] A hook call carrying `completed`/`total` (with `total > 0`) reports `completed / total` clamped to `[0, 1]`; a step-finished call (artifact, no counts) reports `1.0`; a call without counts and without artifact reports `None`. No `ZeroDivisionError` on `total == 0`.
- **FR-5** (must): The transcribe job labels its stalls and drops the `0.9` scale.
  - [ ] From `running` until the provider's first progress callback the job reads `phase: preparing, progress: 0.0` (covers model load, decode and language detection); the first callback clears the phase and progress follows the provider's fraction **unscaled** whether or not diarization is queued behind it.
  - [ ] With `diarize: true`, after transcription the job relays the diarizer's `(phase, fraction)` pairs verbatim (bar restarts at the first counted step), then reads `naming speakers` (progress `null`) during cross-meeting auto-naming, then `1.0` / `null` on success.
  - [ ] A failed diarization pass still degrades to a transcript-with-warning exactly as today, ending at `1.0`.
- **FR-6** (must): The standalone `diarize` job uses the same signal.
  - [ ] Phase `reading transcript` (progress `null`) while the transcript and recording are located, then the diarizer's relayed phases, then `assigning speakers` while labels are written and auto-naming runs, then `1.0` / `null`.
  - [ ] The `0.05` / `0.9` assignments are gone from `_diarize_existing_sync`.
- **FR-7** (must): The Rust shell passes the new fields through unchanged.
  - [ ] `service::JobStatus` carries `progress: Option<f64>` and `phase: Option<String>`; `HttpService::status` decodes `"progress": null` and a body with no `phase` key (an older service) without error.
  - [ ] `jobs::JobSnapshot` gains `phase: Option<String>` (serialised as `"phase"`), copied from every poll along with the nullable progress; a poll whose only change is the phase is still emitted to the `EventSink`.
  - [ ] `FakeService` keeps walking `Queued -> Running -> Done` with non-decreasing `Some(progress)` and `phase: None`.
- **FR-8** (must): The UI renders the phase and an honest bar.
  - [ ] `JobRow`'s running meta line is `<verb> · <phase> · <NN%>` with each part present only when it exists (e.g. `Summarizing · writing summary · 812 tokens`, `Identifying speakers · segmenting speech · 42%`, `Transcribing · 42%`).
  - [ ] A determinate bar (`progressFill` width = progress) renders only when `progress` is a number; while running with `progress: null` an indeterminate track (CSS-animated, same `--track`/`--accent` tokens) renders instead — never an empty 0 % bar.
  - [ ] The header chip (`activeJobView` + `AppHeader`) shows `· NN%` when a percent exists, else `· <phase>` when a phase exists, else nothing.
  - [ ] Queued, done, failed and rejected rows never show a phase.
- **FR-9** (should): The CLI progress loop (`cli.py`) prints `phase: <label>` when the phase changes and `progress: 0.42` only when progress is numeric and changed; it never crashes on a `null` progress.

**Non-functional**:

- **NFR-1**: The default service test run stays model-free, GPU-free and network-free; new tests synchronise on `threading.Event`s, never `sleep`-based timing.
- **NFR-2**: Per-token phase updates cost one string format on the worker thread and nothing on the event loop (status polling reads `JobState` fields only).

## Out of scope

- The `index` job: its progress is already real (`docs processed / total`, `search/indexer.py:255`) and it is not shown in the job list (fired quietly; only the chat tab's `Indexing… NN%` chip reads it). Chunk-level granularity is YAGNI.
- Estimating summary length to fake a summarize percentage (rolling averages, ledger statistics) — explicitly rejected; the label carries the token count instead.
- Prompt-evaluation progress from llama.cpp (no callback exists in `llama-cpp-python` for prompt eval; `reading transcript` is the honest label).
- PDF page-level progress (`xhtml2pdf` renders in one call, seconds at most).
- A weighted single bar across phases (see Assumptions — offered as the open question, not built by default).
- Localised phase labels (the UI has no i18n; existing labels are English).
- Any change to the model/CUDA/embedding/diarization-runtime *download* progress (`model_download.py`, `ModelDownloadStep`) — a separate, already-real byte-based signal.

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task, all three payloads; **mandatory** (desktop, web, cli).
- `testing-toolkit:python-testing-patterns` — pytest tasks in `services/transcription` (desktop/web/cli Tests rows; pytest present).
- `frontend-toolkit:internal-ui` — the two React tasks; **mandatory** (web, internal-tool UI). Not installed on this machine — implementers degrade gracefully: keep `JobRow`/`AppHeader`'s existing visual language and tokens.
- `frontend-toolkit:ui-ux-pro-max` — React tasks (web UI row). Not installed; same degradation.
- `devops-toolkit:devops-rollout-plan` — packaging/release layer (NSIS bundle in `tauri.conf.json`, `installer/`). Signal present; no task in this feature touches packaging, so no task lists it.

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`

## Architecture

**Service (Python)** — the whole signal lives here.

- `jobs.py` `JobState` gains `phase: str | None = None`; `progress` becomes `float | None` (default `0.0`). A tiny helper `_set_phase(job, phase, fraction)` is the only writer used by the runners. `_run_job` (transcribe): `preparing`/`0.0` before the executor call; the provider's `on_progress` clears the phase and writes the raw fraction (the `progress_scale` goes away); `_diarize_segments` hands the diarizer an `on_progress` that relays `(phase, fraction)`; auto-naming sets `naming speakers`. `_diarize_existing_sync`: `reading transcript` → relayed diarizer phases → `assigning speakers`. `_summarize_sync`: `advance()` is replaced by a per-call `on_token` counter (`_complete_text` grows an `on_token` pass-through) that formats the phase; `progress` stays `None`. `_export_sync`: two phase writes, no numbers. The generic `_run_derived_job` success path already writes `1.0`; it now also clears `phase` (and so do the failed/cancelled branches). `_job_state_from_ledger_row` is untouched (`1.0`/`0.0`, phase `None`). `index_activity()` keeps returning the index job's float.
- `schema.py` `JobStatus`: `progress: float | None`, `phase: str | None = None`; the clamp validator skips `None`. `app.py` `get_job` passes `phase`.
- `diarizer.py`: `DiarizerProtocol.diarize` and `PyannoteDiarizer.diarize` gain `on_progress`; a module-level `_progress_hook(on_progress)` builds the pyannote hook (`hook(step_name, step_artifact, file=None, total=None, completed=None)` — pyannote 3.4's `Pipeline.__call__(file, **kwargs)` → `setup_hook` wraps it with `file=`; verified in `services/transcription/.venv/.../pyannote/audio/core/pipeline.py:268` and `pipelines/speaker_diarization.py:337-582`). The hook is passed on both the `return_embeddings=True` call and the retry.
- `cli.py`: the poll loop prints phase changes and skips `None` progress.
- `README.md`: a "Progress and phases" paragraph under the jobs section.

**Shell (Rust)** — pass-through only. `service/mod.rs` `JobStatus { progress: Option<f64>, phase: Option<String>, .. }` + `from_wire`; `service/http.rs` `StatusResponse` with `#[serde(default)]` on both; `service/fake.rs` emits `Some(progress)` / `phase: None`; `jobs.rs` `JobSnapshot.phase`, `apply_status` copies both, `snapshots_equal_ignoring_created_at` compares `phase` (otherwise a phase-only poll is swallowed and the UI never sees it), `new_pending_snapshot` + the `JobSnapshot` literals in `commands.rs`, `commands/llm.rs`, `commands/speakers.rs` get `phase: None`.

**UI (React)** — presentation only. `types.ts` `JobSnapshot.phase: string | null`; `JobRow.tsx` meta line + indeterminate track (`JobRow.module.css`); `lib/activeJob.ts` `ActiveJobView.phase`; `AppHeader.tsx` renders percent-or-phase.

**Data flow**: worker thread writes `job.phase`/`job.progress` → `GET /v1/jobs/{id}` → `HttpService::status` → `apply_status` → `jobs://updated` → `useJobs` → `JobRow` / `activeJobView`. No new IPC commands, no config keys, no ledger columns.

**Risks**:

- Timing-dependent tests (observing a mid-phase state) — mitigated by `threading.Event`-blocking fakes in T1/T3/T4 (NFR-1); never `sleep`.
- A phase-only status change not reaching the UI — T5 extends the snapshot dedupe and tests it.
- Rust struct-literal churn (`JobStatus` in 3 files, `JobSnapshot` in 4) — T5 enumerates every file; inline `#[cfg(test)]` assertions on `progress` change mechanically from `x` to `Some(x)` as part of the type change (Rust tests live in the source files; no other way).
- The hook must accept pyannote's `file=` keyword or every diarization fails — T2 tests the exact call shape pyannote uses.
- The transcribe+diarize bar now visibly restarts after reaching 100 % — deliberate (phase label makes it legible); flagged in Assumptions and the open question.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T5 |
| 2 | T3, T6 |
| 3 | T4, T7 |

## Tasks

### [ ] T1: Service contract — `phase`, nullable `progress`, transcribe `preparing`  [deps: —]

- **Files**: `services/transcription/src/transcription/schema.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/src/transcription/app.py`, `services/transcription/src/transcription/cli.py`, `services/transcription/README.md`
- **Test first**: `services/transcription/tests/test_jobs_phase.py` — cases: (1) `JobStatus` accepts `progress=None` and `phase="rendering PDF"`, still clamps `1.7` to `1.0` and `-0.2` to `0.0` (FR-1); (2) through the FastAPI `TestClient` (fixtures as in `tests/test_api_jobs.py`, `FakeProvider` registered as `"fake"`), a job polled right after submission answers `progress: 0.0, phase: null`, and the succeeded job answers `progress: 1.0, phase: null` (FR-1); (3) a job answered from the ledger after `JobManager` forgets it (the `_job_state_from_ledger_row` path — construct a second manager over the same ledger) reads `1.0` / `null` (FR-1); (4) a test-local provider that blocks on a `threading.Event` *before* its first `on_progress` call is observed via `manager.status()` as `phase == "preparing"`, `progress == 0.0`; after the event is set and it reports `0.5`, the status reads `phase is None`, `progress == 0.5`; success ends `1.0` / `None` (FR-5 first bullet); (5) the CLI transcribe loop (`cli.run_transcribe`/`main` as `tests/test_cli.py` drives it, with the blocking provider) writes a `phase: preparing` line to stderr followed by `progress:` lines, and a status with `progress=None` produces no `progress:` line and no exception (FR-9).
- **Implement**: Add `phase` to `JobState`/`JobStatus`, widen `progress` to `float | None`, skip `None` in the clamp validator, pass `phase` in `app.py::get_job`. In `_run_job` set `phase="preparing"`, `progress=0.0` after `status="running"`; the `on_progress` closure sets `phase=None` and writes the raw fraction (delete `progress_scale`). Clear `phase` on every terminal transition in both runners (`_run_job`, `_run_derived_job`). CLI: track `last_phase`/`last_progress`, print `phase: …` on change, `progress: …` only for numeric values. README: a "Progress and phases" paragraph with the per-type table (transcribe / summarize / export / diarize / index) matching FR-2..FR-6.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: the new file passes; every existing test stays green untouched (`tests/test_api_jobs.py`'s monotonic-progress cases, and `tests/test_jobs_diarization.py` — its `..._scaled_below_one_...` test cannot tell scaled from unscaled with instant fakes, so it keeps passing until T3's author replaces it); `make format lint type test` green for the Python payload.

### [ ] T2: pyannote step progress in `PyannoteDiarizer`  [deps: —]

- **Files**: `services/transcription/src/transcription/diarizer.py`
- **Test first**: `services/transcription/tests/test_diarizer.py` — extend the file's own `FakePipeline` to accept and retain a `hook` kwarg; cases: (1) `diarize(path, cancel=…, on_progress=spy)` records `("loading speaker model", None)` before `FakePipelineClass.from_pretrained` runs and `("decoding audio", None)` before the decode seam runs (FR-4); (2) the pipeline was called with a callable `hook`; invoking it exactly as pyannote does — `hook("segmentation", None, file=audio, total=10, completed=4)` → spy sees `("segmenting speech", 0.4)`; `hook("embeddings", None, file=audio, total=5, completed=5)` → `("extracting voice embeddings", 1.0)`; `hook("speaker_counting", 3, file=audio)` → `("counting speakers", None)`; `hook("discrete_diarization", object(), file=audio)` → `("assigning speakers", None)`; `hook("segmentation", object(), file=audio)` → `("segmenting speech", 1.0)` (FR-4); (3) `total=0` and `completed > total` report `None` and `1.0` respectively, no exception (FR-4); (4) an unknown step `"some_new_step"` with counts reports `("some new step", fraction)` (FR-4); (5) the `TypeError` retry path (existing `test_a_pipeline_without_embedding_support_still_diarizes`) still receives the `hook` on the second call (FR-4); (6) with `on_progress` omitted the pipeline still gets a callable hook (or `None`) and every existing case in the file passes unchanged.
- **Implement**: Add `on_progress: Callable[[str, float | None], None] | None = None` to `DiarizerProtocol.diarize` and `PyannoteDiarizer.diarize`; a module-level `_STEP_PHASES` dict and `_progress_hook(on_progress)` returning a function with pyannote's signature `(step_name, step_artifact, file=None, total=None, completed=None)`; pass `hook=` in both `pipeline(...)` calls. Report the two pre-inference phases in `diarize`.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: `uv run --directory services/transcription pytest tests/test_diarizer.py -q` green; mypy clean (`make type`); `make lint` green.

### [ ] T3: Diarization phases in both job paths  [deps: T1, T2]

- **Files**: `services/transcription/src/transcription/jobs.py`
- **Test first**: `services/transcription/tests/test_jobs_diarization.py` — this task's author also extends `services/transcription/tests/fakes.py::FakeDiarizer` (the only task touching `fakes.py`): an `on_progress=None` kwarg and a scripted `phases: list[tuple[str, float | None]]` it replays before returning, plus an optional `threading.Event` it waits on after replaying (so a test can observe the last replayed phase through `manager.status()`). Cases: (1) transcribe with `diarize=True` and a provider that blocks after reporting `0.75`: status reads `progress == 0.75`, `phase is None` — the fraction is no longer scaled to `0.9` (FR-5; replaces `test_transcription_progress_is_scaled_below_one_while_diarization_remains`); (2) same job, diarizer scripted with `[("segmenting speech", 0.4), ("extracting voice embeddings", 0.6)]` and blocking: status reads `("extracting voice embeddings", 0.6)` (FR-5); (3) after release, the job succeeds at `1.0` / `None`, transcript carries speakers as before (FR-5); (4) a diarizer raising `AUDIO_DECODE` on the transcribe path still yields a transcript with the `diarization.status == "failed"` block and ends `1.0` / `None` (FR-5); (5) the standalone `diarize` job with the blocking scripted diarizer is observed at its last replayed phase with `progress` equal to that phase's fraction, and ends `1.0` / `None` with `speaker_count`/`embeddings` in the manifest as before (FR-6); (6) a `diarize` job cancelled while the diarizer blocks ends `cancelled` with `phase is None` (FR-1); (7) with speaker embeddings and a sibling meeting to match, the final manifest's `auto_named_segments` and the sibling-named speakers are unchanged from today's test — the `naming speakers` phase write is not observed mid-flight (no seam is worth adding for a sub-second step) but must not alter the outcome (FR-5/6).
- **Implement**: `_diarize_segments` passes `on_progress=lambda phase, fraction: _set_phase(job, phase, fraction)` to `diarizer.diarize`, then `_set_phase(job, "naming speakers", None)` around `auto_assign_speakers`. `_diarize_existing_sync`: `reading transcript` at entry, the relay during `diarize`, `assigning speakers` before `_label_with_turns`; delete `0.05`/`0.9`.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: `pytest tests/test_jobs_diarization.py tests/test_jobs.py tests/test_diarizer.py -q` green with the old scaled-progress test replaced; `make format lint type test` green for the Python payload.

### [ ] T4: Summarize and export phases  [deps: T3]

- **Files**: `services/transcription/src/transcription/jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` — cases (subclass `tests/fakes.py::FakeLlm` locally in this file with `threading.Event`s; do not edit `fakes.py`): (1) a summarize job whose LLM blocks *before* streaming is observed as `phase == "reading transcript"`, `progress is None` (FR-2); (2) an LLM that streams its 3 pieces then blocks is observed as `phase == "writing summary · 3 tokens"`, `progress is None` (FR-2); (3) a two-chunk transcript (as `test_a_long_transcript_is_map_reduced` builds one) with an LLM blocking during its second call is observed as `phase == "summarizing part 2/3 · 3 tokens"` (FR-2); (4) success ends `1.0` / `None` and writes `summary.md` (FR-2, keep the existing assertions); (5) an export job observed at the moment `exporting.build_export_md` runs (monkeypatch the module attribute `transcription.jobs.exporting.build_export_md` with a wrapper that captures `manager.status(job_id)` and delegates) reads `("writing export.md", None)`, and at the moment `transcription.jobs.render_pdf` runs reads `("rendering PDF", None)` (FR-3); (6) export ends `1.0` / `None` with both artifacts, and the PDF-font warning test still passes (FR-3).
- **Implement**: `_complete_text` gains `on_token: Callable[[str], None] | None = None` and forwards it. `_summarize_sync`: replace `advance` with a per-call counter closure — `label = "writing summary" if planned_calls == 1 else f"summarizing part {calls_done + 1}/{max(planned_calls, calls_done + 1)}"`; set `reading transcript` before each call, `f"{label} · {n} tokens"` on each token; never write a numeric `progress`. `_export_sync`: two `_set_phase` calls, remove the constants.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: `pytest tests/test_llm_jobs.py tests/test_llm_units.py -q` green; full `uv run --directory services/transcription pytest -q` green under 30 s; `make format lint type test` green for the Python payload.

### [ ] T5: Rust seam — `phase` and nullable `progress` through to `JobSnapshot`  [deps: —]

- **Files**: `apps/desktop/src-tauri/src/service/mod.rs`, `apps/desktop/src-tauri/src/service/http.rs`, `apps/desktop/src-tauri/src/service/fake.rs`, `apps/desktop/src-tauri/src/jobs.rs`, `apps/desktop/src-tauri/src/commands.rs`, `apps/desktop/src-tauri/src/commands/llm.rs`, `apps/desktop/src-tauri/src/commands/speakers.rs`
- **Test first**: `apps/desktop/src-tauri/tests/job_phase.rs` (cargo integration test over the public `transcriber_desktop_lib` surface, like `tests/e2e_flow.rs`; `wiremock` is already a dev-dependency) — cases: (1) `JobStatus::from_wire("running", None, Some("rendering PDF"), None, None)` yields `progress: None`, `phase: Some(..)`; `from_wire("cancelled", Some(0.3), None, ..)` keeps the `"cancelled"` message override (FR-7); (2) `HttpService::new(mock_url, token)` `.status()` against a body `{"status":"running","progress":null,"phase":"rendering PDF"}` decodes to `None` / `Some`; against `{"status":"running","progress":0.5}` (no `phase` key) decodes to `Some(0.5)` / `None` (FR-7); (3) `FakeService` scripted with `FakeTiming { running_polls: 2, .. }` walks Queued → Running → Done with `progress` `Some(_)` non-decreasing and `phase` `None` throughout (FR-7); (4) a `JobRegistry` polling a test-local `TranscriptionService` impl whose consecutive `Running` statuses differ *only* in `phase` emits one `EventSink` snapshot per distinct phase, each carrying that phase (FR-7); (5) `serde_json::to_value(&JobSnapshot{..})` contains a `"phase"` key (`null` when unset) (FR-7).
- **Implement**: Widen `JobStatus`, `StatusResponse` (`#[serde(default)]` on both new/changed fields), `from_wire`; update the `FakeService` literals to `Some(..)` + `phase: None`; add `phase` to `JobSnapshot`, `apply_status`, `snapshots_equal_ignoring_created_at`, `new_pending_snapshot` and the derived-job snapshot constructors in `commands.rs` / `commands/llm.rs` / `commands/speakers.rs`. Update the `http.rs` module doc header (field list) and mechanically adjust the inline `#[cfg(test)]` assertions on `progress` (`x` → `Some(x)`) — they live in the source files being changed.
- **Skills**: `testing-toolkit:testing-best-practices`
- **Done when**: `cargo test -p transcriber-desktop` green (inline + `tests/`); `cargo clippy --workspace --all-targets -- -D warnings` clean; `cargo fmt --all --check` clean.

### [ ] T6: `JobRow` — phase text, real bar or indeterminate track  [deps: T5]

- **Files**: `apps/desktop/src/types.ts`, `apps/desktop/src/components/JobRow.tsx`, `apps/desktop/src/components/JobRow.module.css`
- **Test first**: `apps/desktop/src/components/JobRow.test.tsx` — cases: (1) running `summarize` with `phase: "writing summary · 812 tokens"`, `progress: null` renders meta `Summarizing · writing summary · 812 tokens`, no `%`, and an element with `role="progressbar"` lacking `aria-valuenow` (the indeterminate track) (FR-8); (2) running `diarize` with `phase: "segmenting speech"`, `progress: 0.42` renders `Identifying speakers · segmenting speech · 42%` and a `role="progressbar"` with `aria-valuenow="42"` whose fill width is `42%` (FR-8); (3) running `transcribe` with `phase: null`, `progress: 0.42` still renders `Transcribing · 42%` (existing case stays green) (FR-8); (4) a `queued`, `done` and `failed` row given a stray `phase` renders no phase text and no progressbar (FR-8); (5) `progress: 1.7` is clamped in the rendered width (existing clamp behaviour).
- **Implement**: `JobSnapshot.phase: string | null` in `types.ts`. `metaLine` running branch builds `[verb, phase?, percent?, project?]`. Render `progressTrack` with `role="progressbar"` + `aria-valuenow/min/max` when numeric; otherwise a `progressTrack` carrying `data-indeterminate` with a `progressFill` animated by a `@keyframes` slide (width ~30 %, translating across the track), colours unchanged. Presentational only — no invoke, no fetch.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: `npm --prefix apps/desktop run test -- src/components/JobRow.test.tsx` green; `npm --prefix apps/desktop run lint`, `run type`, `run format` clean.

### [ ] T7: Header chip — percent or phase  [deps: T6]

- **Files**: `apps/desktop/src/lib/activeJob.ts`, `apps/desktop/src/components/AppHeader.tsx`
- **Test first**: `apps/desktop/src/components/AppHeader.test.tsx` — cases (build the view with `activeJobView([...])` and render `AppHeader` with it): (1) a running `export` job with `progress: null`, `phase: "rendering PDF"` renders the chip `Exporting PDF “<name>” · rendering PDF` and no `%` (FR-8); (2) a running `diarize` job with `progress: 0.42` and a phase renders `· 42%` and not the phase text (percent wins; the chip stays short) (FR-8); (3) a queued job renders neither (existing case) (FR-8); (4) the chip still returns to Recordings on click (existing case).
- **Implement**: `ActiveJobView` gains `phase: string | null` (set only while `running`); `AppHeader` renders `· {percent}%` when `percent != null`, else `· {phase}` when `phase`, else nothing. `activeJob.test.ts` stays untouched and green.
- **Skills**: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: `npm --prefix apps/desktop run test` green; `make format lint type test` green across all payloads; then the desktop-profile check: `cd apps/desktop && npm run tauri dev`, re-run "Summarize" on an existing meeting and "Export" on another, and confirm the row reads `Summarizing · reading transcript` → `Summarizing · writing summary · N tokens` with a moving indeterminate track and no percentage, `Exporting PDF · rendering PDF`, and the header chip mirrors the phase while on another tab. If the diarization runtime is fetched on this machine, "Identify speakers" shows `segmenting speech · NN%` then `extracting voice embeddings · NN%`. Record the observations in the PR description.

## QA expectations

No aggregate `make qa`. The gate runs `make format`, `make lint`, `make type`, `make test` — each fans out over cargo, npm and `uv` (Makefile at repo root; direct equivalents commented there). `make lint` additionally runs `sync_version --check`, `verify_locks --check` and `gen_diarization_runtime --check`, none of which this feature touches. The Python suite must stay model/GPU/network-free (<30 s); `tests/test_gpu_integration.py` self-skips. Rust tests are inline `#[cfg(test)]` modules plus `src-tauri/tests/`; `cargo clippy -D warnings` is the strict one. Nothing known-flaky; the new phase tests must use `threading.Event` synchronisation (NFR-1) precisely so they never become flaky.

## Assumptions & decisions
- 2026-09-09 — (OPERATOR) Multi-phase job presentation → per-phase bar: each counted phase drives its own 0→100 % with a label; weighted single bar and indeterminate-for-all rejected.

- 2026-09-09 — (AUTO: codebase) Which job types have a real linear signal? → transcribe (whisper `segment.end/duration`) and index (`docs processed/total`) do; summarize (`tokens/max_tokens`, cap 6144, no prompt-eval callback), export (single `render_pdf` call) and diarize (pipeline call today opaque; pyannote 3.4 `hook` gives per-step `completed/total` for `segmentation` and `embeddings`) do not, except through the hook.
- 2026-09-09 — (AUTO: request text) Fake percentage vs indeterminate → `progress` is the current phase's real fraction or `null`; a `phase` label always says what is happening. Never a hand-picked constant.
- 2026-09-09 — (AUTO: pyannote 3.4.0 in `services/transcription/.venv`) Hook API → `Pipeline.__call__(file, **kwargs)` pops `hook`, wraps it with `file=`; steps `segmentation`/`embeddings` call it with `completed`/`total`; `speaker_counting`/`discrete_diarization` with an artifact only. Hook always passed, including the `TypeError` retry.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test style → behaviour observed through `manager.status()` / HTTP / rendered DOM, never through `JobState` internals; timing resolved with `threading.Event`-blocking fakes, not sleeps; the only spies are on the export controller's module-level collaborators (a controller gets a brief integration test); no mocks of the in-process orchestration otherwise.
- 2026-09-09 — (AUTO: CLAUDE.md) Docs kept current → `services/transcription/README.md` gains the per-type progress/phase table (T1).
- 2026-09-09 — (AUTO: frontend-toolkit:internal-ui unavailable on this machine) UI degradation → no new visual language; the indeterminate track reuses the existing `progressTrack`/`progressFill` tokens and the row's spinner remains the activity cue.
- 2026-09-09 — (ASSUMPTION) Per-phase bar vs one weighted bar → the bar shows the *current phase's* fraction and restarts at the next counted phase (transcribe → segmenting speech → extracting voice embeddings). Weights between phases would be guesses on this machine (diarization has barely run here), and a mis-weighted single bar is exactly the "uneven scale" complained about. Surfaced as the open question below.
- 2026-09-09 — (ASSUMPTION) Summarize shows no bar at all, even for map-reduce → the coarse `k/n` step is carried in the label (`summarizing part 2/3 · N tokens`); a stepwise bar would sit still for minutes between calls, which reads as a stall.
- 2026-09-09 — (ASSUMPTION) Phase labels are English display strings composed service-side → the UI has no i18n and its existing labels are English; one source of wording, no label enum across three languages.
- 2026-09-09 — (ASSUMPTION) The transcribe job keeps a numeric `0.0` during `preparing` rather than `null` → keeps the CLI and `test_api_jobs.py` monotonic-progress contract intact; the label carries the honesty.
- 2026-09-09 — (ASSUMPTION) Header chip shows percent *or* phase, percent winning → the chip is a one-line affordance; `Identifying speakers in “X” · segmenting speech · 42%` is too long for it.
- 2026-09-09 — (ASSUMPTION) Index job untouched → already real, not in the job list; the chat tab's chip keeps reading its float.
- 2026-09-09 — (ASSUMPTION) Rust inline `#[cfg(test)]` assertions on `progress` are adjusted by T5's implementer as part of the type change → Rust tests live inside the changed source files; the new behaviour is tested from `tests/job_phase.rs`, which the test-author owns.
