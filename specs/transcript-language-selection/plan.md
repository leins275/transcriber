---
slug: transcript-language-selection
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Transcript language follows the recording (Russian or English)

## Architecture overview

The per-job `language` channel already exists end-to-end; this feature makes it validated, constrained, honored, recorded, and operator-controllable. No new endpoints, no schema change to `transcript.json`.

```
RecordingPage.tsx ──(Auto|ru|en)──> api.ts transcribeVaultEntry(entryId, language)
        │                                   │ invoke("transcribe_vault_entry", {entryId, language})
        ▼                                   ▼
App.tsx handleTranscribe          commands.rs transcribe_vault_entry (validates ru|en|None)
                                            │
                                  meetings.rs transcribe_vault_entry_handler
                                            │ enqueue_filed(..., language)
                                  jobs.rs PendingWork::Filed{language} → SubmitRequest{language}
                                            │ (drag-drop ingest path stays language: None = Auto)
                                  http.rs SubmitBody{language?} ── POST /v1/jobs ──▶
                                            │
                                  schema.py JobCreate.language: Literal["ru","en"] | None
                                            │ (invalid → RequestValidationError → 400 invalid_request,
                                            │  no ledger row — existing handler in app.py:142)
                                  jobs.py JobManager.submit → ledger.insert_job(language=requested)
                                            │
                                  local_whisper.py transcribe(language=...)
                                    ├─ language given → decode_kwargs["language"] = it (FR-2)
                                    └─ language None  → model.detect_language(...) once,
                                       argmax over {ru, en} only → decode_kwargs["language"] (FR-1)
                                       (same forced kwargs on both BatchedInferencePipeline
                                        and sequential model.transcribe — the constraint is
                                        applied *before* either decode path runs)
                                            │
                                  TranscriptResult.language ∈ {ru,en}, language_probability =
                                    constrained-detection prob (auto) or model-reported (forced)
                                            │
                                  jobs.py success path → transcript.json {language, language_probability}
                                    + ledger.finish_succeeded(..., language=result.language)  ← new:
                                    the ledger row is updated to the *actual* decode language (FR-4;
                                    today `language` is only written at insert time from the request)
```

Component changes, grounded:

- **`services/transcription/src/transcription/local_whisper.py`** (providers/): when `language is None`, run one constrained detection pass via `WhisperModel.detect_language` (one encoder window — NFR-1), restrict `all_language_probs` to `{ru, en}`, force the winner into `decode_kwargs["language"]` (today line ~264 passes `None` straight through). `language_out` becomes the forced value; `language_probability` becomes the constrained probability on auto runs.
- **`schema.py`**: `JobCreate.language` narrows from `str | None` to `Literal["ru", "en"] | None`. Pydantic rejection rides the existing `RequestValidationError → invalid_request 400` handler (`app.py:142-149`) — before `JobManager.submit`, so no ledger row (NFR-3).
- **`config.py`**: `load_config` validates/normalizes the layered `language` value (`None`/`""` → `None`; anything not `ru`/`en` → `ConfigError` naming the allowed values). This covers both the config-file default and `cli.py --language` (overrides flow through `load_config`; `cli.py main()` already maps `ConfigError` to a nonzero `invalid_request` exit, line 305-309).
- **`jobs.py` + `ledger.py`**: `finish_succeeded` gains an optional `language` param (written only when non-None, so LLM job rows are untouched); the transcribe success path passes `result.language` and mirrors it onto the in-memory `JobState` (FR-4).
- **`apps/desktop/src-tauri`**: `transcribe_vault_entry` command gains `language: Option<String>`, validated in the handler (IPC args are untrusted — desktop profile checklist); threaded `meetings.rs → jobs.rs enqueue_filed → PendingWork::Filed → SubmitRequest.language`. The drag-drop ingest path (`jobs.rs:477-481`) stays `language: None` — per Q1 there is no ingest-time control; None means constrained auto, and `SubmitBody`'s existing `skip_serializing_if` omits the field on the wire.
- **`apps/desktop/src`**: `RecordingPage.tsx` gains an Auto / Russian / English choice attached to the existing Transcribe/Re-transcribe button (line ~199-203), default Auto; `onTranscribe(entryId, language)` → `App.tsx` → `api.ts transcribeVaultEntry(entryId, language)`. FR-6: the page shows the loaded transcript's `language` (`TranscriptView.language`, `types.ts:96`); `null` renders nothing.

**Pinned IPC contract** (T5 and T6 build to it in parallel): command `transcribe_vault_entry`, args `{ entryId: string, language: "ru" | "en" | null }` (omitted/null = Auto). Rust side accepts `Option<String>` and rejects any other string with `invalid_argument`.

## Risks

- **`WhisperModel.detect_language` signature drift** across faster-whisper 1.2.x (`audio` vs `features`, VAD kwargs). Mitigation: T3's first step is inspecting the installed package's signature (`uv run --directory services/transcription python -c "import inspect, faster_whisper; print(inspect.signature(faster_whisper.WhisperModel.detect_language))"`) and shaping the call + fakes to it; the fallback (encode features manually and call the ctranslate2 detect op) is explicitly second choice.
- **FR-5 acceptance wording vs Q1** — spec ambiguity I interpreted: FR-5's checkbox says the wire body carries "the operator-selected language ... on both the drag-drop ingest path and the Re-transcribe path", but Q1 put the only control on the recording page. Interpretation: on the ingest path the operator's selection is always Auto, so the wire body legitimately *omits* `language` (asserted by test); only Re-transcribe can carry `"ru"`/`"en"`. This also satisfies FR-5's second checkbox (default = constrained auto, never a silent hard-force).
- **Parallel T5/T6 contract drift** — mitigated by the pinned IPC contract above; T8 exercises the joined flow.
- **Forced-run probability semantics**: faster-whisper reports `language_probability = 1.0` (or omits it) when language is forced; spec explicitly allows "the model-reported value on a forced run" — tests must not over-assert here.
- **Ledger update for FR-4** touches `finish_succeeded`, shared with LLM job types — guarded by only writing `language` when non-None (LLM path passes nothing), and `test_llm_jobs.py` must stay green.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3, T4, T5, T6 |
| 2 | T7 |
| 3 | T8 |

## Tasks

### [ ] T1: Validate `language` on `POST /v1/jobs`  [deps: —]

- **Files**: `services/transcription/src/transcription/schema.py`, `services/transcription/tests/test_api_jobs.py`
- **Test first**: `services/transcription/tests/test_api_jobs.py` — cases: `POST /v1/jobs` with `language="de"` → 400, body `error_kind == "invalid_request"`, ledger has no row and `GET /v1/jobs` lists no job (FR-3, NFR-3); same for `language=""`; `language="ru"`, `"en"`, and omitted are accepted (202, job created).
- **Implement**: Narrow `JobCreate.language` to `Literal["ru", "en"] | None = None` in `schema.py:135`. Rejection flows through the existing `RequestValidationError` handler (`app.py:142`) — no handler changes; assert no-ledger-row in the tests to prove rejection precedes `JobManager.submit`.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests pass; `uv run --directory services/transcription pytest -q` green; `make lint` and `make type` pass.

### [ ] T2: Validate `language` in config layering and the CLI  [deps: —]

- **Files**: `services/transcription/src/transcription/config.py`, `services/transcription/tests/test_config.py`, `services/transcription/tests/test_cli.py`
- **Test first**: `services/transcription/tests/test_config.py` — cases: `load_config` with `language="de"` (from config file, from `TRANSCRIBER_LANGUAGE`, and from overrides) raises `ConfigError` whose message names `ru`/`en`; `""` normalizes to `None`; `"ru"`/`"en"`/unset accepted (FR-3). `services/transcription/tests/test_cli.py` — cases: `main(["transcribe", ..., "--language", "de", ...])` exits nonzero with the allowed values named on stderr; `--language en` proceeds past config loading (FR-3 acceptance: CLI exit).
- **Implement**: In `load_config` (`config.py:201`), after the override layer merges: normalize `language` (`""` → `None`, lowercase) and raise `ConfigError` for any value outside `{None, "ru", "en"}`. `cli.py` needs no change — `main()` already maps `ConfigError` to `EXIT_CODES[ErrorKind.INVALID_REQUEST]` (`cli.py:305-309`), and `serve` startup fails the same way.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests pass; `uv run --directory services/transcription pytest -q` green; `make lint` and `make type` pass.

### [ ] T3: Constrained {ru, en} detection in the local provider  [deps: —]

- **Files**: `services/transcription/src/transcription/providers/local_whisper.py`, `services/transcription/tests/test_provider_local.py`
- **Test first**: `services/transcription/tests/test_provider_local.py` (extend the existing inline fake `WhisperModel`s at lines ~103/127/178 with a `detect_language` returning canned `all_language_probs`) — cases: (a) probabilities ranking `uk` above both `ru` and `en` → decode still receives the higher of `ru`/`en` in `decode_kwargs["language"]` (FR-1 acceptance bullet 2); (b) constraint applies on the sequential path *and* on the `BatchedInferencePipeline` path — assert the pipeline fake's received kwargs (FR-1 bullet 3); (c) explicit `language="en"`/`"ru"` skips detection entirely (no `detect_language` call) and is passed through (FR-2); (d) result `language` equals the forced/chosen value and `language_probability` is the constrained-detection probability on auto runs, the model-reported value on forced runs (FR-4); (e) exactly one `detect_language` call per auto transcribe (NFR-1).
- **Implement**: In `LocalWhisperProvider.transcribe` (`local_whisper.py:247`), before building `decode_kwargs`: if `language is None`, call `self._model.detect_language(...)` once (verify the installed faster-whisper signature first — see Risks), take `all_language_probs`, restrict to `{"ru", "en"}`, argmax → `decode_kwargs["language"]`, remember the probability. Replace `language_out = getattr(info, "language", None) or language` (line 331) with the value actually forced into `decode_kwargs`, and prefer the constrained probability on auto runs.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests pass; `uv run --directory services/transcription pytest -q` green; `make lint` and `make type` pass.

### [ ] T4: Job pipeline records the actual decode language (transcript + ledger)  [deps: —]

- **Files**: `services/transcription/src/transcription/jobs.py`, `services/transcription/src/transcription/ledger.py`, `services/transcription/tests/test_jobs.py`, `services/transcription/tests/test_ledger.py`, `services/transcription/tests/fakes.py`
- **Test first**: `services/transcription/tests/test_jobs.py` (extend `fakes.py`'s `FakeProvider` to record the `language` kwarg it received, e.g. `self.seen_language`) — cases: submit with `language="en"` → provider received `"en"`, `transcript.json.language == "en"`, ledger row `language == "en"` (FR-2 spy acceptance, FR-4); symmetric for `"ru"`; submit with no language and a FakeProvider that "detects" `"ru"` with probability 0.9 → `transcript.json.language == "ru"`, `language_probability == 0.9`, ledger row `language == "ru"` even though it was inserted as `NULL` (FR-4 both bullets). `services/transcription/tests/test_ledger.py` — cases: `finish_succeeded(language="en")` updates the column; `finish_succeeded()` with no language leaves the inserted value untouched (LLM rows unaffected).
- **Implement**: Add optional `language: str | None = None` to `Ledger.finish_succeeded` (`ledger.py:183`), included in the `UPDATE` only when non-None. In `jobs.py`'s transcribe success path (~line 580, where `TranscriptDoc` is built from `result`), pass `language=result.language` to `finish_succeeded` and set it on the in-memory `JobState`. No `transcript.json` shape change (NFR-2).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests pass; `uv run --directory services/transcription pytest -q` green (including `test_llm_jobs.py`, untouched); `make lint` and `make type` pass.

### [ ] T5: Rust plumbing — `transcribe_vault_entry` carries a validated language to the wire  [deps: —]

- **Files**: `apps/desktop/src-tauri/src/commands.rs`, `apps/desktop/src-tauri/src/commands/meetings.rs`, `apps/desktop/src-tauri/src/jobs.rs`, `apps/desktop/src-tauri/src/service/http.rs`, `apps/desktop/src-tauri/src/service/fake.rs`
- **Test first**: `apps/desktop/src-tauri/src/service/http.rs` (existing submit-body test module, ~line 570) — cases: `SubmitBody` with `language: Some("en")` serializes `"language":"en"`; with `None` the key is absent from the JSON body (FR-5 both checkboxes: override carried / Auto omitted). `apps/desktop/src-tauri/src/commands/meetings.rs` tests — cases: handler with `language: Some("de".into())` returns `invalid_argument` and enqueues nothing (IPC args are untrusted — desktop profile); `Some("en")`/`Some("ru")`/`None` accepted. `apps/desktop/src-tauri/src/jobs.rs` tests — case: a `PendingWork::Filed { language: Some("en") }` job produces a `SubmitRequest` with `language: Some("en")`; the ingest path still submits `language: None`.
- **Implement**: Add `language: Option<String>` to the `transcribe_vault_entry` command (`commands.rs:1048`) and `transcribe_vault_entry_handler` (`meetings.rs:593`); validate against `{"ru", "en"}`. Thread it through `enqueue_filed` (`jobs.rs:331`) → `PendingWork::Filed` → the `SubmitRequest` at `jobs.rs:477-481` (ingest arm stays `None`). `service/mod.rs::SubmitRequest` and `http.rs::SubmitBody` already carry the field. Honors the pinned IPC contract (see Architecture overview).
- **Skills**: —
- **Done when**: new tests pass; `cargo test --workspace` green; `cargo clippy --workspace --all-targets -- -D warnings` (make lint) and `cargo check --workspace` (make type) pass.

### [ ] T6: Recording-page language control (Auto / Russian / English) wired to the command  [deps: —]

- **Files**: `apps/desktop/src/components/RecordingPage.tsx`, `apps/desktop/src/components/RecordingPage.test.tsx`, `apps/desktop/src/components/RecordingPage.module.css`, `apps/desktop/src/App.tsx`, `apps/desktop/src/api.ts`, `apps/desktop/src/types.ts`
- **Test first**: `apps/desktop/src/components/RecordingPage.test.tsx` — cases: default control state is Auto and clicking Transcribe/Re-transcribe calls `onTranscribe(entryId, null)` (FR-5: default = constrained auto, no silent hard-force); selecting English then Re-transcribe calls `onTranscribe(entryId, "en")`, symmetric for Russian (FR-5); the control renders only when the button does (`entry.has_source`); existing Transcribe/Re-transcribe tests (line ~107) updated for the new callback signature.
- **Implement**: Add a `TranscriptLanguage = "ru" | "en"` type in `types.ts`; extend `onTranscribe` to `(entryId, language: TranscriptLanguage | null)` in `RecordingPage.tsx:29` with a small select/segmented control beside the button at line ~199 (labels: Auto, Russian, English); pass through `App.tsx handleTranscribe` (line 345) into `api.ts transcribeVaultEntry` (line 97) as `{ entryId, language }` per the pinned IPC contract. Keep it a presentational control — state local to the page, no persistence.
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: new tests pass; `npm --prefix apps/desktop run test` green; `npm --prefix apps/desktop run lint` and `npm --prefix apps/desktop run type` pass.

### [ ] T7: Recording-page language indicator  [deps: T6]

- **Files**: `apps/desktop/src/components/RecordingPage.tsx`, `apps/desktop/src/components/RecordingPage.test.tsx`, `apps/desktop/src/components/RecordingPage.module.css`
- **Test first**: `apps/desktop/src/components/RecordingPage.test.tsx` — cases: a loaded transcript with `language: "en"` shows an English indicator; `language: "ru"` shows Russian; `language: null` (legacy transcript) renders no indicator and no placeholder (FR-6 acceptance, exactly).
- **Implement**: Read `transcript.language` from the already-loaded `TranscriptView` (`types.ts:96`; the page fetches it via `onReadTranscript`) and render a small labeled badge near the transcript header/meta area — visible at a glance so the operator knows when to re-transcribe with an override (FR-6). Map `"en"` → English, `"ru"` → Russian; anything null/unknown → nothing.
- **Skills**: `frontend-toolkit:internal-ui`, `frontend-toolkit:ui-ux-pro-max`
- **Done when**: new tests pass; `npm --prefix apps/desktop run test` green; `npm --prefix apps/desktop run lint` and `npm --prefix apps/desktop run type` pass.

### [ ] T8: Integration — real-audio fixtures and full-stack verification  [deps: T1, T2, T3, T4, T5, T6, T7]

- **Files**: `services/transcription/tests/test_gpu_integration.py`, `services/transcription/tests/data/README.md`
- **Test first**: `services/transcription/tests/test_gpu_integration.py` — extend the opt-in `@pytest.mark.gpu` test (self-skips without a sample/CUDA, per its existing pattern): an English-speech sample submitted with no `language` produces `transcript.json` with `language: "en"` and English text; a Russian sample produces `"ru"` and Russian text; the ledger row matches (FR-1 acceptance bullet 1, FR-4). Samples resolved via `TRANSCRIBER_TEST_SAMPLE`-style env vars (`TRANSCRIBER_TEST_SAMPLE_EN` / `_RU`), documented in `tests/data/README.md`.
- **Implement**: Follow the file's existing structure (`JobManager` + `Ledger` against real weights). Then run the whole-repo verification: all four make targets, plus the desktop-profile launch check — start the app (`npm --prefix apps/desktop run tauri dev` or the operator's usual dev command), open a recording, and drive Re-transcribe with English/Russian/Auto, confirming the job submits and (with the service running) the wire behavior matches FR-5. CLI profile check: one real `transcription-service transcribe --language en` invocation plus `--language de` asserting the nonzero exit and stderr message.
- **Skills**: `testing-toolkit:python-testing-patterns`, `frontend-toolkit:internal-ui`
- **Done when**: `make format`, `make lint`, `make type`, `make test` all pass; the gpu-marked tests pass on the CUDA machine (or self-skip cleanly elsewhere); the launched-app Re-transcribe flow and the CLI invocations behave as specified.

## QA expectations

- `make format` — cargo fmt + prettier (npm) + ruff format. Exists.
- `make lint` — clippy `-D warnings` + eslint + ruff check + version/lock sync scripts. Exists.
- `make type` — `cargo check` + `tsc` + mypy. Exists.
- `make test` — `cargo test --workspace` + vitest + `pytest -q` + scripts tests. Exists. GPU integration tests are deselected by default (`-m "not gpu"`) and self-skip without a configured sample — they never block the default suite.
- Known slow: first `cargo test --workspace` in a fresh worktree rebuilds the Tauri workspace (native deps, cmake caches — see project memory on knf-rs-sys); budget time accordingly, nothing here is flaky.
