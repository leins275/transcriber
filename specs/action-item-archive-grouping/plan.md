---
slug: action-item-archive-grouping
status: approved
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
---

# Plan: Archive status and source grouping for action items

## Architecture overview

Pure data-model feature, no UI. Two production modules change, one Rust module gets a doc mirror:

```
services/transcription/src/transcription/
  artifacts.py   <- contract home: module docstring documents the front-matter
                    field contract (FR-6); gains UNSORTED_DIR_NAME constant
                    (mirror of crates/vault/src/paths.rs:58) and
                    source_date_from_meeting_name() (FR-3 parsing)
  jobs.py        <- _extract_sync (lines ~845-956): the single writer for both
                    action_items and facts jobs; its meta dict (lines 931-942)
                    gains "archived": False and "source_date", and its
                    source_project derivation changes (FR-1/FR-2/FR-3)
  exporting.py   <- unchanged; FR-5 is pinned by a new test only
crates/vault/src/
  artifacts.rs   <- doc-only mirror of the field contract (FR-6)
```

**Data flow (unchanged shape)**: `_extract_sync` builds one `meta` dict per item → `artifacts.write_item` → `render_front_matter` emits `key: <json>` lines → `parse_front_matter` / `list_items` read best-effort → `exporting.build_export_md` consumes `StoredItem`s (title/type/body only, so `archived` is invisible to it by construction).

**The one real design decision — `source_project` derivation (F6 robustness)**: today `_extract_sync` uses `Path(job.output_path).parent.name` (jobs.py:895), which breaks when sibling F6 (different worktree, same base) moves artifact output into the per-meeting folder. The plan re-anchors the derivation on the **meeting directory resolved from job inputs**: `meeting_dir = Path(job.source_path)` (jobs.py:846) is the meeting folder under *both* the current and the post-F6 layout, because it is what the job is submitted with. So:

```python
parent = meeting_dir.parent
project_name = None if parent.name.casefold() == UNSORTED_DIR_NAME else parent.name
```

This is exactly the pattern `_export_sync` already uses (jobs.py:1011-1012), so the codebase converges on one derivation instead of two. It yields identical values under the current layout (existing test `test_action_items_are_written_with_screenshots_and_front_matter` keeps asserting `source_project == "ELS"`) and stays correct after F6. **Merge order F6→F8 reconciles the layouts**; nothing in this feature reads `output_path` for identity anymore.

**`source_date` (FR-3)**: parsed from the meeting folder's leading `YYMMDD` (vault naming contract `<YYMMDD> - <stem>`, `crates/vault/src/paths.rs`). Century is fixed at `20` (`260824` → `2026-08-24`) — do **not** use `strptime("%y...")`, whose 69–99 → 19xx pivot contradicts the vault contract; validate digits explicitly and construct `date(2000 + yy, mm, dd)` inside try/except, returning `None` on any failure.

**Schema change (the cross-language front-matter contract, FR-6)** — the exact key set `_extract_sync` writes, pinned by a Python test and documented in both `artifacts.py`'s docstring and `artifacts.rs`'s module docs:

| key | JSON type | null | notes |
|---|---|---|---|
| `type` / `kind` | string | no | `type` for action items, `kind` for facts (existing) |
| `title` | string | no | existing |
| `archived` | boolean | no | **new**; always written `false`; flipped only by external editors; absence reads as false |
| `source_project` | string | yes | `null` when the meeting lives under `unsorted/` — never the literal string |
| `source_meeting` | string | no | meeting folder name (existing semantics) |
| `source_recording` | string | yes | stored `source.<ext>` filename; `null` when absent (existing) |
| `source_date` | string `YYYY-MM-DD` | yes | **new**; from leading `YYMMDD`; `null` when unparseable |
| `timestamps` | number[] | no | existing |
| `created` | string (ISO datetime, UTC) | no | existing |
| `model` | string | no | existing |
| `job_id` | string | no | existing |
| `screenshots` | string | no | existing status value |

Plus two contract clauses that are behavior, not fields: unknown keys survive into `StoredItem.meta` (FR-4), and **no production code path rewrites an existing artifact `.md`** after atomic creation (FR-4; a future mutation feature must round-trip unknown keys and the body).

**New dev dependency**: `pyyaml` (dev group only) for NFR-1's "round-trip through a real YAML parser" test. `uv.lock` must be regenerated (`make lint` runs `scripts/verify_locks.py --check`).

## Risks

- **F6 lands in a sibling worktree from the same base** — the biggest risk. Mitigated by anchoring all derivations on `job.source_path` (the meeting dir), never `output_path` position (T2); the plan's derivation is layout-invariant, and merge order F6→F8 reconciles the rest. If F6 merges first and moves this code, the conflict surface is the single meta-dict block in `_extract_sync`.
- **Key-set drift between Python and the Rust doc mirror**: T4 depends on T2 so the mirror copies the test-pinned set verbatim; the Rust docs explicitly cede source-of-truth to `artifacts.py` + its pytest.
- **`%y` century pivot bug** in date parsing: called out above; T1's tests include a value in the 69–99 range (e.g. `990101` → `2099-01-01`) to pin the 20xx rule.
- **FR-4's "no rewrite path" is an absence claim** — not fully provable by a unit test. Mitigated two ways: a test pins that `list_items` leaves file bytes untouched, and the claim is written into both docstrings so the evaluator/review gate checks any future diff against it.
- **Lock-file check**: adding pyyaml without regenerating `services/transcription/uv.lock` fails `make lint` (`verify_locks.py`). T1's Files include the lock.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2 |
| 2 | T3 |
| 3 | T4 |

(T1 and T2 have disjoint file sets and no deps — parallel. T3 needs T1's helper and constant. T4 mirrors T3's pinned key set.)

## Tasks

### [ ] T1: Front-matter tolerance contract: helpers, docs, and pinning tests in artifacts.py  [deps: —]

- **Files**: `services/transcription/src/transcription/artifacts.py`, `services/transcription/tests/test_llm_units.py`, `services/transcription/pyproject.toml`, `services/transcription/uv.lock`
- **Test first**: `services/transcription/tests/test_llm_units.py` — cases:
  - `source_date_from_meeting_name("260824 - standup") == "2026-08-24"` (FR-3); `"990101 - x"` → `"2099-01-01"` (pins the 20xx century, guards the `%y` pivot); `None` for: no leading digits (`"Planning"`), short digits (`"2608 - x"`), invalid calendar date (`"261345 - x"`, `"260230 - x"`), digits not followed by the contract shape but still 6 digits (`"260824standup"` — decide and pin: parse the first 6 chars regardless of what follows, matching the vault crate's verbatim-prefix stance) (FR-3).
  - Obsidian-style rewrite tolerance (FR-4): write a file whose front matter has reordered keys, `archived: true`, an unknown `tags: [x]` key, YAML-quoted strings (`title: "Quoted"`), then `list_items` returns it with every key/value in `StoredItem.meta` (`archived is True` as JSON bool, `tags == ["x"]`, quoted string unquoted by JSON parse) and the body byte-intact.
  - NFR-2 fuzz-ish: `parse_front_matter` never raises over a battery of malformed inputs (unterminated fence, `key:` with no value, non-JSON scalars like `archived: yes` degrading to raw string `"yes"`, binary-ish garbage, empty string).
  - NFR-1: a `write_item`-produced file's front-matter block parses under `yaml.safe_load` (real PyYAML, new dev dep) to the same mapping `parse_front_matter` returns.
  - Read-only listing (FR-4b, testable half): file bytes before and after `list_items` are identical.
- **Implement**: Add `UNSORTED_DIR_NAME = "unsorted"` (mirror of `crates/vault/src/paths.rs:58`) and `source_date_from_meeting_name(name: str) -> str | None` (explicit digit parse + `datetime.date(2000+yy, mm, dd)` in try/except) to `artifacts.py`. Extend the module docstring with the full field-contract table from the plan's Architecture overview, including the null-ability rules, the missing-`archived`-reads-as-false convention, and the "no code path rewrites an existing artifact `.md`; a future mutation feature must round-trip unknown keys and body" clause. Add `pyyaml>=6` to `[dependency-groups] dev` in `pyproject.toml` and regenerate `uv.lock` (`uv lock` in `services/transcription`). No behavior change to `render_front_matter`/`parse_front_matter`/`write_item` — the tests pin existing behavior as contract.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests pass; `make format lint type test` pass (lint includes `verify_locks.py --check`, so the lock must be consistent); `artifacts.py` docstring contains the complete field table.

### [ ] T2: Pin FR-5: export output ignores `archived`  [deps: —]

- **Files**: `services/transcription/tests/test_exporting.py` (new)
- **Test first**: `services/transcription/tests/test_exporting.py` — cases:
  - Build a project tree (`<tmp>/vault/ELS/<meeting>` + `<items parent>` as `exporting.build_export_md` consumes it today via `items_for_meeting`) with one item written by `artifacts.write_item` whose meta has `archived: false`; capture `build_export_md(...)` output; rewrite the item file (in the test, simulating an external editor) flipping to `archived: true`; assert the returned markdown is byte-identical (FR-5).
  - `list_items` includes the `archived: true` item exactly like an unarchived one (no filtering anywhere) (FR-5).
- **Implement**: Test-only task — no production code change; `exporting.py`'s `_item_section` already reads only title/type/body, and this test makes that indifference a pinned contract so a future "hide archived" change fails loudly. Model fixtures on `test_llm_units.py`'s `write_item` round-trip test rather than the heavier job-level fixtures.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: `uv run --directory services/transcription pytest tests/test_exporting.py -q` passes; `make test` passes.

### [ ] T3: Writer: `archived`, `source_date`, layout-robust `source_project` in `_extract_sync`  [deps: T1]

- **Files**: `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_llm_jobs.py` — cases:
  - FR-1: extend `test_action_items_are_written_with_screenshots_and_front_matter` and `test_facts_use_the_kind_key_...` — the raw `.md` text contains the literal line `archived: false`, and `parse_front_matter` yields `meta["archived"] is False` (bool, not string) for both job types.
  - FR-2: existing project case keeps `source_project == "ELS"`, `source_meeting == MEETING_NAME`, `source_recording == "source.mp4"`; new test with a meeting under `<vault>/unsorted/<MEETING_NAME>` asserts `source_project` is JSON `null` (`meta["source_project"] is None`, and the raw line is `source_project: null` — never the string `"unsorted"`); `source_recording is None` when the meeting folder has no `source.<ext>`.
  - FR-3: meeting `"260101 - Planning"` yields `source_date == "2026-01-01"`; a meeting folder named without a valid leading `YYMMDD` (e.g. `"Planning notes"`) yields `meta["source_date"] is None` **and the job still succeeds**.
  - FR-6 drift pin: assert the exact front-matter key set of a written item equals `{"type", "title", "archived", "source_project", "source_meeting", "source_recording", "source_date", "timestamps", "created", "model", "job_id", "screenshots"}` (and the `kind` variant for facts) — fails CI on any accidental add/remove.
- **Implement**: In `_extract_sync` (jobs.py:894-895, 931-942): replace `project_name = Path(job.output_path).parent.name` with the meeting-anchored derivation `parent = meeting_dir.parent; project_name = None if parent.name.casefold() == artifacts.UNSORTED_DIR_NAME else parent.name` (same pattern as `_export_sync` jobs.py:1011-1012 — optionally point that line at the new constant too, it is in this task's Files); add `"archived": False` (right after `title`, so property editors surface it near the top) and `"source_date": artifacts.source_date_from_meeting_name(meeting_dir.name)` to the meta dict. NFR-3 holds automatically — the write still flows through `write_item` → `write_text_atomic`; add no other write path.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: all new/extended tests pass; full `make format lint type test` passes (integration-level proof for this data-only feature is the job-level tests driving the real `JobManager` submit→terminal→files-on-disk path — the desktop profile's launch-the-app check has no UI surface to drive here, per the spec's binding no-UI decision).

### [ ] T4: Mirror the field contract in the Rust vault crate docs  [deps: T3]

- **Files**: `crates/vault/src/artifacts.rs`
- **Test first**: none new — this is a documentation mirror (FR-6); the executable pin lives in T3's Python key-set test, and `cargo test -p vault` must stay green to prove the edit is doc-only.
- **Implement**: Extend the module doc comment of `crates/vault/src/artifacts.rs` with the front-matter field contract (verbatim key names and types from the table T3's test pins, including `archived` and `source_date` null-ability and the unsorted→`null` rule), stating explicitly that (a) the Python side (`services/transcription/src/transcription/artifacts.py` docstring + its pytest key-set test) is the source of truth, (b) any Rust code that later reads/writes artifact front matter must use these names verbatim, and (c) sibling F6/F7 may relocate or delete this module — if F7 deletes it, the contract's single home is the Python side (spec FR-6 wording). No code, no new parsing.
- **Skills**: — (no Rust toolkit in the spec's applicable set; doc-only change)
- **Done when**: `cargo test -p vault` and `cargo clippy --workspace --all-targets -- -D warnings` pass; the doc text and T3's pinned key set list identical names; `make format lint type test` pass.

## QA expectations

All four Makefile targets exist and resolve (`make -n` verified in the spec): `format`, `lint`, `type`, `test`. Notes:

- `make lint` runs `scripts/verify_locks.py --check` — T1's pyyaml dev-dep addition **must** regenerate `services/transcription/uv.lock` or lint fails.
- `make test` is repo-wide (cargo + npm + pytest + scripts); the Python suite runs with `-m "not gpu"` by default, so no GPU/model downloads are involved in any task here.
- mypy is strict but scoped to `src/` — test files are lint-only (ruff, with S101 waived in tests).
- No UI validation phase applies: the spec's binding operator decision is that this feature has no UI surface; frontend-toolkit skills attach to no task by design.

## Notes for the operator (spec interpretations)

- **F6 coordination (flagged)**: the spec targets the post-F6 layout, but F6 lands in a sibling worktree from the same base. The plan encodes `source_project`/`source_meeting` derivation from the job's *input* meeting directory (`job.source_path`), which is correct under both the current project-level layout (existing tests keep passing unchanged in expectation) and the post-F6 per-meeting layout. Merge order F6→F8 reconciles; the expected conflict surface is the single meta-dict block in `_extract_sync`.
- **`YYMMDD` century (interpreted)**: fixed at 20xx (`260824` → `2026-08-24`, `990101` → `2099-01-01`). The vault contract treats the six chars as verbatim; no 19xx pivot.
- **FR-4's repo-wide "no rewrite path" check (interpreted)**: encoded as (a) a test pinning that listing leaves file bytes untouched, plus (b) the contract clause written into both modules' docs so the review gate catches future violations — a source-grepping test would be brittle theater.
- **NFR-1 (interpreted)**: requires a real YAML parser; `pyyaml` is added to the dev dependency group only (not a runtime dep — production code keeps its zero-YAML-dependency stance).
