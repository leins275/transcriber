---
slug: service-log-original-file-name
status: approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Service log shows the original file name

## Architecture overview

Desktop-side only. Zero changes under `services/transcription/` (FR-6 makes that an acceptance criterion, not just a preference), no ledger schema migration, no `crates/vault` changes. The feature is two independent data paths plus a display rule:

**Write path (FR-1, FR-5)** — record the original name at submit time:

```
apps/desktop/src-tauri/src/jobs.rs        process_one():
  PendingWork::Ingest { source_path }  ->  original_file_name = Some(file_name_of(&source_path))
  PendingWork::Filed  { .. }           ->  original_file_name = None   (only source.<ext> exists on disk — never recorded as "original", FR-5)
        |
        v
apps/desktop/src-tauri/src/service/mod.rs   SubmitRequest  + original_file_name: Option<String>
        |
        v
apps/desktop/src-tauri/src/service/http.rs  SubmitBody     + meeting: Option<{"original_file_name": &str}>
                                            (#[serde(skip_serializing_if = "Option::is_none")] — the key is
                                             entirely absent when None, so the wire body for Filed/older flows
                                             is byte-identical to today's)
        |
        v
POST /v1/jobs body: {"audio_path", "output_dir", "language"?, "meeting": {"original_file_name": "..."}}
  -> F2's existing `JobCreate.meeting` (schema.py:138) -> stored verbatim as `meeting_json` (jobs.py:365)
```

`SubmitRequest` gains a field, so its three construction sites must all be updated in the same task: `jobs.rs:477` (production), and the two test helpers `service/http.rs:571` and `service/fake.rs:482`. **Note:** `service/fake.rs` is therefore in this feature's file set even though the batch brief did not list it — the test helper there constructs a `SubmitRequest` literal and will not compile otherwise.

**Read path (FR-2, FR-6, NFR-1, NFR-2)** — surface the recorded name; parse `meeting_json` exactly once, on the Rust side:

```
GET /v1/jobs row: meeting_json is a TEXT column (json.dumps), so it arrives as a JSON *string*
        |
        v
service/http.rs   LedgerJobResponse  + #[serde(default)] meeting_json: Option<String>
                  From<LedgerJobResponse> for LedgerJob parses it leniently:
                    serde_json::from_str -> object -> get("original_file_name") -> non-empty str -> Some(name)
                    anything else (absent key, "not json", {}, non-string, empty) -> None  (NFR-2, never an error)
        |
        v
service/mod.rs    LedgerJob      + original_file_name: Option<String>
        |
        v
commands/ledger.rs LedgerJobView + original_file_name: Option<String>   (serde snake_case, like every field)
        |
        v
apps/desktop/src/types.ts          LedgerJobView + original_file_name: string | null
        |
        v
apps/desktop/src/components/LedgerPanel.tsx   row head label
```

**Display rule (FR-2, FR-3, FR-4, NFR-3)** — in `LedgerPanel.tsx`, the row-head label becomes:

1. `row.original_file_name` when present (FR-2);
2. else, split `source_path` on `/[\\/]/` (both separators, NFR-3): if the base name matches `source.<ext>` (`/^source\.[^.\\/]+$/`) **and** a parent folder component exists, show `<parent folder>.<ext>` (FR-3 — recovers the vault meeting-folder name for all pre-feature rows);
3. else today's behavior exactly: the base name (LLM/derived rows whose `source_path` is a directory are unchanged).

The `title` tooltip stays the full `source_path` (FR-4). The panel stays presentational — it never sees `meeting_json`.

**Contract note (interpretation, flagged):** the FR-3/NFR-2 acceptance bullets say "Vitest: `meeting_json` of `"not json"` (and `{}`) renders via the fallback". Since FR-2 mandates that `meeting_json` parsing happens once on the Rust side, the webview never receives `meeting_json` — malformed-payload tolerance is therefore proven by Rust wiremock tests (malformed `meeting_json` → `original_file_name == None`, T2), and the Vitest side proves the same end state (`original_file_name: null` → fallback, no throw, T3). Together they cover the criterion; a literal Vitest `meeting_json` test is impossible under FR-2's own architecture.

## Risks

- **`SubmitRequest` construction sites** — adding a non-`Option`-defaulted field breaks `fake.rs`/`http.rs` test helpers; T1's Files set includes all three sites so nothing is left half-compiled (also why T1 and T2 share files and cannot run in parallel).
- **Wire-shape drift** — the exact-body wiremock test (`submit_posts_exact_body_keys...`) uses `body_json` (exact match); T1 must extend it with the `meeting` object *and* add a no-`meeting`-key case, or FR-5's "never invents a name" is unverified at the wire level.
- **`meeting_json` arrives as a string, not an object** — the column stores `json.dumps(...)`; a Rust deserializer expecting a JSON object would fail on every real row. T2's tests pin the string form (and the absent-key form, FR-6).
- **Accidental Python edits** — guarded by T4's explicit `git diff --stat -- services/transcription/` check (must be empty) plus the untouched pytest suite.
- **Windows paths** — fallback derivation must handle `\`-separated absolute paths (NFR-3); T3 tests use real `C:\...` fixtures as the acceptance criteria spell.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T3 |
| 2 | T2 |
| 3 | T4 |

T1/T2 both touch `service/mod.rs` + `service/http.rs`, so they serialize; T3 (webview only) has no file overlap with either and runs alongside T1 against the contract fixed above (`original_file_name: string | null`). T4 is the integration/app-level gate.

## Tasks

### [ ] T1: Record the original file name at submit time (write path)  [deps: —]

- **Files**: `apps/desktop/src-tauri/src/service/mod.rs`, `apps/desktop/src-tauri/src/service/http.rs`, `apps/desktop/src-tauri/src/service/fake.rs`, `apps/desktop/src-tauri/src/jobs.rs`
- **Test first**: `apps/desktop/src-tauri/src/service/http.rs` (`#[cfg(test)] mod tests`) and `apps/desktop/src-tauri/src/jobs.rs` (`#[cfg(test)] mod tests`) — cases:
  - extend `submit_posts_exact_body_keys_and_returns_job_id_from_202`: body carries `"meeting": {"original_file_name": "ELS - 260812 - Security issue.mp4"}` alongside `audio_path`/`output_dir` when `SubmitRequest.original_file_name` is `Some` (FR-1, exact `body_json` match);
  - new wiremock case: when `original_file_name` is `None`, the posted body has **no** `meeting` key at all (FR-5 wire level, exact `body_json` match);
  - `jobs.rs`: a dropped file enqueued via `enqueue()` reaches `submit()` with `original_file_name == Some("<dropped base name>")` — use a capturing wrapper service (pattern: `OrderTrackingService`) that records full `SubmitRequest`s (FR-1);
  - `jobs.rs`: a job enqueued via `enqueue_filed()` reaches `submit()` with `original_file_name == None` (FR-5).
- **Implement**: add `original_file_name: Option<String>` to `SubmitRequest` (`mod.rs`); in `jobs.rs::process_one`, capture `file_name_of(&source_path)` in the `PendingWork::Ingest` arm and `None` in the `Filed` arm, and pass it into the `SubmitRequest` literal; in `http.rs`, add a `SubmitMeeting<'a> { original_file_name: &'a str }` serialize struct and a `#[serde(skip_serializing_if = "Option::is_none")] meeting: Option<SubmitMeeting<'a>>` field on `SubmitBody`; update the two test-helper `request()` constructors (`http.rs:571`, `fake.rs:482`) with `original_file_name: None`.
- **Skills**: —
- **Done when**: all new/extended cases pass and the existing suite is green: `cargo test --workspace` passes; `cargo clippy --workspace --all-targets -- -D warnings` and `cargo fmt --all` clean.

### [ ] T2: Surface `meeting_json` through the read path (Rust parse-once)  [deps: T1]

- **Files**: `apps/desktop/src-tauri/src/service/mod.rs`, `apps/desktop/src-tauri/src/service/http.rs`, `apps/desktop/src-tauri/src/commands/ledger.rs`
- **Test first**: `apps/desktop/src-tauri/src/service/http.rs` and `apps/desktop/src-tauri/src/commands/ledger.rs` test modules — cases (wiremock `GET /v1/jobs` unless noted):
  - a row whose `meeting_json` is the string `"{\"original_file_name\": \"ELS - 260812 - Security issue.mp4\"}"` yields `LedgerJob.original_file_name == Some(...)` (FR-2);
  - a row **without** a `meeting_json` key deserializes successfully with `original_file_name == None` (FR-6, serde default — explicit acceptance criterion);
  - `meeting_json` of `"not json"`, of `"{}"`, and of `"{\"original_file_name\": 42}"` each yield `None` with no error (NFR-2);
  - `ledger.rs`: `From<LedgerJob> for LedgerJobView` passes `original_file_name` through, and every pre-existing field is unchanged (FR-4).
- **Implement**: `#[serde(default)] meeting_json: Option<String>` on `LedgerJobResponse`; a small lenient parser in `http.rs` (`serde_json::from_str::<Value>` → `get("original_file_name")` → non-empty `as_str`, everything else `None`) applied in `From<LedgerJobResponse> for LedgerJob`; add `original_file_name: Option<String>` to `LedgerJob` (`mod.rs`) and `LedgerJobView` (`ledger.rs`).
- **Skills**: —
- **Done when**: all cases pass; `cargo test --workspace` green; clippy/fmt clean.

### [ ] T3: Render the recorded name with the meeting-folder fallback (webview)  [deps: —]

- **Files**: `apps/desktop/src/types.ts`, `apps/desktop/src/components/LedgerPanel.tsx`, `apps/desktop/src/components/LedgerPanel.test.tsx`
- **Test first**: `apps/desktop/src/components/LedgerPanel.test.tsx` — cases (extend `buildRow`, which must gain `original_file_name: null` as its default so every existing test still models a pre-feature row):
  - a row with `original_file_name: "ELS - 260812 - Security issue.mp4"` renders that name in the row head, not `source.mp4` (FR-2);
  - a row with `original_file_name: null` and `source_path: "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4"` renders `260812 - Security issue.mp4` (FR-3, NFR-3 — backslash path);
  - same fallback with a `/`-separated path still works (NFR-3);
  - a row with `original_file_name: null` whose `source_path` base name is **not** `source.<ext>` (e.g. an LLM job pointing at a meeting directory, `"D:\\Meetings\\ELS\\260812 - Security issue"`) renders exactly today's base name (FR-3);
  - `original_file_name: null` renders via the fallback with no thrown error — the frontend half of NFR-2 (the malformed-`meeting_json` half is T2's Rust tests; see the contract note in the overview);
  - the row's `title` attribute still equals the full `source_path`, both when the recorded name is shown and when the fallback is (FR-4).
- **Implement**: add `original_file_name: string | null` to `LedgerJobView` in `types.ts` (contract fixed by this plan; T2 produces the same shape from Rust). In `LedgerPanel.tsx`, replace the row-head call to `fileNameOf(row.source_path)` with a `displayNameOf(row)` implementing the three-step rule from the overview (prefer `row.original_file_name`; else `source.<ext>` base + parent folder → `<parent>.<ext>`; else base name); keep the component presentational and the `title` tooltip unchanged.
- **Skills**: `frontend-toolkit:internal-ui` (mandatory), `frontend-toolkit:ui-ux-pro-max`
- **Done when**: all Vitest cases pass: `npm --prefix apps/desktop run test` green; `npm --prefix apps/desktop run lint` and `run type` clean.

### [ ] T4: Integration verification — full QA, zero-Python-diff, app-level check  [deps: T1, T2, T3]

- **Files**: none (verification-only; the only edit is this plan's own status marker)
- **Test first**: no new test files — this task executes the verification the `desktop` profile prescribes and the spec's cross-cutting acceptance criteria:
  - `git diff --stat <base_ref> -- services/transcription/` is empty, and `rg -n "SCHEMA_VERSION" services/transcription/src/transcription/ledger.py` still reads `2` (FR-1, FR-6);
  - `uv run --directory services/transcription pytest -q` passes with zero modifications (FR-6);
  - full `make format`, `make lint`, `make type`, `make test` pass at the repo root.
- **Implement**: app-level check per the desktop profile's Verification section: launch the app (`npm --prefix apps/desktop run tauri dev` or the repo's equivalent dev entry), drop a recording named like `ELS - 260812 - Security issue.mp4`, open the Service log — the new row shows the dropped file's name; confirm `GET /v1/jobs` (curl against the sidecar with its bearer token) returns that row with `meeting_json` containing `"original_file_name"`; confirm a pre-existing row shows its meeting-folder-derived fallback name and its tooltip still shows the full `source_path`. Windows paths are the in-scope platform (NFR-3).
- **Skills**: `frontend-toolkit:internal-ui` (mandatory — judging the rendered panel), `testing-toolkit:python-testing-patterns` (regression run of the untouched service suite)
- **Done when**: all four make targets green; empty `services/transcription` diff; the live-app check observed with both a fresh row (original name) and a pre-existing row (fallback name).

## QA expectations

- Root `Makefile` provides all four targets, each fanning out across the three payloads: `make format` (cargo fmt, prettier via npm, ruff format), `make lint` (clippy `-D warnings`, eslint, ruff check + version/lock checks), `make type` (cargo check, tsc, mypy), `make test` (cargo test --workspace, vitest, pytest ×2).
- Per-task, the narrower direct equivalents (`cargo test --workspace`, `npm --prefix apps/desktop run test`) are sufficient; T4 runs the full four.
- `uv` (not `python`) drives all Python invocations. No known-flaky suites; the wiremock tests bind ephemeral loopback ports and are stable on Windows.
