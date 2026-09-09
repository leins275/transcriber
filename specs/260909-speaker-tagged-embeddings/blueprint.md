---
slug: 260909-speaker-tagged-embeddings
created: 2026-09-09
status: approved
base_ref: <git sha, recorded at blueprint approval>
---

# Blueprint: Speaker-tagged embeddings (per-chunk speaker tags + speaker-scoped retrieval)

## Summary

Today the search index knows speakers only at the document level (`docs.speakers`, a space-joined string feeding the trigram title channel); the chunks that are embedded and retrieved carry speaker names only incidentally inside their `[m:ss] Speaker: text` lines, and nothing in search, chat or MCP can scope retrieval to what one person said. This feature makes speaker identity travel with every embedded chunk: each transcript chunk records the distinct speakers it contains (a `chunk_speakers` link table), its breadcrumb line — the first line of the embedded text — names them, and every retrieval channel accepts a speaker filter. `POST /v1/search` and the MCP `hybrid_search` tool get an explicit `speaker` parameter (mirroring the existing `date`), and the chat applies the filter automatically when a question names a speaker known to the index (mirroring `extract_query_dates`). No Rust or React change: existing indexes are rebuilt automatically by the schema-version bump on the app's next start.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` and `tauri = { version = "2" }` in `apps/desktop/src-tauri/Cargo.toml` (Tauri 2 shell).
- `web` — `apps/desktop/package.json` names `react` and `vite` (webview UI); the backend is FastAPI, not Django, so only the UI and Tests rows of this profile apply.
- `cli` — `[project.scripts]` in `services/transcription/pyproject.toml` (`transcription-service`, `transcriber-mcp`).

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Service backend | Python 3.12, FastAPI + Pydantic v2, `uv`-managed | `services/transcription/pyproject.toml` |
| Search index | SQLite (FTS5 unicode61 + trigram, sqlite-vec vec0), schema versioned by `PRAGMA user_version` | `services/transcription/src/transcription/search/index_db.py` |
| Embeddings | bge-m3 GGUF via llama-cpp (CPU), `FakeEmbedder` for tests | `services/transcription/src/transcription/llm/`, `services/transcription/tests/fakes.py` |
| MCP | `mcp` FastMCP stdio server over the same index (read-only) | `services/transcription/src/transcription/mcp_server.py` |
| Desktop shell | Tauri 2 / Rust | `apps/desktop/src-tauri/Cargo.toml`, `commands/search.rs` |
| UI | React 18 + Vite, vitest | `apps/desktop/package.json` |
| Vault rules | Rust crate | `crates/vault/` |
| Tests | pytest (`-m "not gpu"` by default), cargo test, vitest | `services/transcription/pyproject.toml` `[tool.pytest.ini_options]`, `Makefile` |

Makefile QA targets present: format | lint | type | test (no aggregate `qa`).

## Requirements

- **FR-1** (must): Every transcript chunk stored in the index records the distinct speakers whose lines it contains.
  - [ ] After `index_vault` over a transcript whose segments carry `speaker` labels, each stored chunk can be looked up by any speaker who has at least one line in it, and not by a speaker who has none.
  - [ ] Operator names from `speakers.json` outrank the diarization label, exactly as they do in the chunk text (`render_transcript_lines` semantics).
  - [ ] Lines without a speaker contribute no tag; summary and note chunks carry no speaker tags.
  - [ ] Speaker matching in the index is case-insensitive for Cyrillic and Latin alike (the stored key is `str.casefold()` of the display name).
  - [ ] Replacing a doc (`upsert_doc`) or sweeping it (`delete_docs_not_in`) leaves no orphan speaker tags behind.
- **FR-2** (must): The embedded text of a transcript chunk names its speakers.
  - [ ] A transcript chunk's first line (the breadcrumb) has the form `[<project> / <meeting dir> / <m:ss>–<m:ss> / <speaker>, <speaker>]`, speakers in order of first appearance; the speaker segment is omitted when the chunk has no named lines.
  - [ ] Summary/note breadcrumbs are unchanged (`[<project> / <meeting dir> / <kind>]`).
  - [ ] `make_snippet` output is unchanged (it already drops line one), so search hits show no breadcrumb.
- **FR-3** (must): Existing indexes are rebuilt automatically, never migrated in place.
  - [ ] `INDEX_SCHEMA_VERSION` is bumped; opening a version-1 index file read-write deletes it and starts empty (the existing stale-index path), so the app's startup `reindex_vault` catch-up repopulates it — no operator action.
  - [ ] Opening an older-version index **read-only** (the MCP server) reports it as stale instead of serving partially-broken queries; the MCP tools answer the existing "index has not been built yet — open the app once" message.
- **FR-4** (must): Retrieval can be scoped to one or more speakers across every channel.
  - [ ] `SearchService.search` / `retrieve` accept `speakers: set[str] | None` (casefolded names, OR semantics). With a filter, the vector and BM25 channels consider only chunks tagged with one of those speakers; the exact-title and trigram channels consider only docs having at least one such chunk; the chunk returned as a hit's snippet / chat context is one of the matching chunks.
  - [ ] A filter naming a speaker absent from the index yields no results, not an error.
  - [ ] No filter → behaviour and ranking are identical to today.
  - [ ] `POST /v1/search` accepts an optional `speaker` string (max 200 chars); an empty/whitespace value means no filter. The response wire shape is unchanged.
  - [ ] MCP `hybrid_search` accepts an optional `speaker` argument with the same semantics.
- **FR-5** (must): The chat scopes retrieval automatically when the question names a known speaker.
  - [ ] `extract_query_speakers(text, known)` (pure logic, `search/speakers.py`) returns the subset of `known` speaker keys the text mentions: the full name as whole words, case-insensitively; or the first-name token (letters only, ≥ 3 chars) as a whole word, allowing a Cyrillic inflection of up to two trailing letters (with the final `а`/`я` of a ≥ 4-letter name treated as part of the ending: "Ольга" matches "Ольге", "Иван" matches "Ивана"/"Иваном", "Марк" does not match "марта").
  - [ ] Generic diarization labels (`Speaker 1`, `SPEAKER_02`, …) are never auto-matched.
  - [ ] Two known speakers sharing a first name both match (OR filter); text naming nobody returns an empty set.
  - [ ] `POST /v1/chat` derives the known speakers from the index scoped to the request's `project` (`IndexDb.known_speakers(project)`), applies `extract_query_speakers` to the question, and passes the result as the retrieval filter alongside the date filter. A question naming a speaker yields sources only from that speaker's chunks; a question naming nobody retrieves as today.
- **FR-6** (should): Documentation follows the behaviour.
  - [ ] `services/transcription/README.md` "Hybrid search and the MCP server" describes the per-chunk speaker tags, the `speaker` parameter on `/v1/search` and `hybrid_search`, the chat's automatic speaker filter, and the one-time rebuild on upgrade.

**Non-functional**:

- **NFR-1**: The default pytest run stays model-free, GPU-free and network-free (< 30 s); every new test uses `FakeEmbedder` and tmp-path SQLite files.
- **NFR-2**: The speaker filter is an indexed lookup (`chunk_speakers(speaker_key)` indexed), never a scan over chunk text.

## Out of scope

- Any Rust/React change: no speaker picker or filter chip in the library search or the chat tab; `SearchResultView` and the chat IPC are untouched.
- Returning per-chunk speakers on the search wire shape (`SearchResultModel` unchanged).
- Speaker-aware ranking boosts (a "speaker" RRF channel) — the filter is a hard scope like the date filter.
- Morphological name matching beyond the two-letter Cyrillic inflection rule (no stemming library, no transliteration, no nickname tables).
- Auto-detecting speakers from the free-text `/v1/search` query (explicit `speaker` parameter only, matching how `date` works there).
- In-place migration of an existing index (the module's rule is delete-and-rebuild).
- Changes to `speakers.json`, the diarization pipeline or `speaker_matching.py`.

## Skills

- `testing-toolkit:testing-best-practices` — every test-authoring task; **mandatory** (desktop, web, cli profiles).
- `testing-toolkit:python-testing-patterns` — pytest suite in `services/transcription/tests/`.
- `frontend-toolkit:internal-ui` — internal-tool React UI; **mandatory** on UI tasks (web profile). Not installed on this machine and no task in this blueprint touches the UI, so it binds nothing here.
- `frontend-toolkit:ui-ux-pro-max` — React UI layer (no UI task in this blueprint).
- `devops-toolkit:devops-rollout-plan` — packaging/installer layer (`installer/`, `tauri.conf.json` bundle); no task here touches it.

**Strict skills**:

- planning: `testing-toolkit:testing-best-practices`
- development: `testing-toolkit:testing-best-practices`, `frontend-toolkit:internal-ui` (unavailable locally; no UI domain task in this plan)

## Architecture

All changes live in `services/transcription/src/transcription/` (the Python package stays self-contained).

**`search/index_db.py`** — `INDEX_SCHEMA_VERSION = 2`. New table in `_SCHEMA`:

```sql
CREATE TABLE chunk_speakers(
  chunk_id INTEGER NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  speaker_key TEXT NOT NULL,
  PRIMARY KEY(chunk_id, speaker_key)
);
CREATE INDEX chunk_speakers_by_key ON chunk_speakers(speaker_key);
```

`ChunkRecord` gains `speakers: tuple[str, ...] = ()` (display names; `upsert_doc` stores `name.casefold()` distinct). `_delete_doc_locked` already deletes chunk rows explicitly with `PRAGMA foreign_keys=ON`, so the cascade clears tags. Read side: `fts_query`, `best_chunk_for`, `vec_query`, `title_trigram_query`, `exact_title_docs` gain `speakers: set[str] | None = None`, applied as `chunk_id IN (SELECT chunk_id FROM chunk_speakers WHERE speaker_key IN (...))` for chunk-level channels and `doc_id IN (SELECT chunks.doc_id FROM chunks JOIN chunk_speakers USING(chunk_id) WHERE speaker_key IN (...))` for doc-level ones (`vec_query`'s post-filter loop gains the same check, still within `VEC_OVERFETCH_FACTOR`). New `known_speakers(project: str | None) -> list[str]` (distinct `speaker_key` of chunks in scope). Read-only opens compare `PRAGMA user_version` to `INDEX_SCHEMA_VERSION` and expose `schema_stale: bool` (never migrate).

**`search/indexer.py`** — `_Line` gains `speaker: str | None`; `_transcript_lines` fills it from the same override-or-label resolution it already does for `names`. `_chunks_from_lines` computes each chunk's distinct speakers in first-appearance order, passes them to the breadcrumb callback (`breadcrumb_of(first, last, speakers)`) and stores them on the `ChunkRecord`. `transcript_breadcrumb` renders `[project / meeting / window / A, B]`; `flat_breadcrumb` ignores speakers. The module docstring's "speakers" claim becomes true.

**`search/speakers.py`** (new, pure logic, sibling of `dates.py`) — `GENERIC_LABEL = re.compile(r"^speaker[ _]?\d+$")`, `normalize_speaker_param(value) -> str | None` (strip + casefold, `None` for empty — the `/v1/search`/MCP counterpart of `normalize_date_param`), `extract_query_speakers(text, known: Iterable[str]) -> set[str]` implementing FR-5's rule against a casefolded copy of the text.

**`search/service.py`** — `_ranked` / `search` / `retrieve` take `speakers: set[str] | None` and thread it into every `IndexDb` read call.

**`schema.py`** — `SearchRequest.speaker: str | None = Field(default=None, max_length=200)`.

**`api/search_routes.py`** — `search` passes `normalize_speaker_param(payload.speaker)`; `chat` computes `question_speakers = extract_query_speakers(question, db.known_speakers(payload.project))` via `manager.index_db()` inside the same `run_serial` callable as `retrieve` (the index handle is only touched on the serial executor), and passes `speakers=question_speakers or None`.

**`mcp_server.py`** — `hybrid_search(..., speaker: str | None = None)`; `_Vault.index()` returns `None` when the read-only handle reports `schema_stale`.

**Data flow**: transcript.json + speakers.json → `_transcript_lines` (line + speaker) → `_chunks_from_lines` (chunk text with speaker breadcrumb, `speakers` tuple) → `_embed_chunks` (the embedded text now leads with the names) → `upsert_doc` (chunks + chunk_speakers) → query time: explicit `speaker` (search/MCP) or auto-extracted speakers (chat) → `SearchService` → filtered channels → RRF → hits whose best chunk is one the speaker took part in.

**Risks**:

- One-time full re-embed of every vault on first start after upgrade (bge-m3 on CPU; minutes for tens of meetings). Accepted: the module's documented policy; the index-status chip shows progress. T1 owns the version bump; the README (T5) states it.
- False-positive auto-filter in chat when a first name is also a common word stem (hard filter hides results). Mitigated by the conservative whole-word + ≤2-letter-ending rule, the generic-label exclusion, and scoping `known_speakers` to the chat's project (T3, T5). Open question 1 offers boost semantics as the alternative.
- Sibling features in this batch (`260909-project-speaker-roster`, `260909-per-turn-speaker-reassign`) may touch `speakers.json` handling; this plan reads speakers only through the existing `load_speaker_overrides` / `render_transcript_lines` seam, so it is insulated as long as that seam holds.
- `sqlite-vec` may be unavailable in some test environments; T1's vector-filter case must skip (not fail) when `db.vec_available` is false, as `test_index_db.py` already does.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T3 |
| 2 | T2 |
| 3 | T4 |
| 4 | T5, T6 |

## Tasks

### [ ] T1: Per-chunk speaker tags in the index DB, speaker-filtered reads, schema v2  [deps: —]

- **Files**: `services/transcription/src/transcription/search/index_db.py`
- **Test first**: `services/transcription/tests/test_index_db_speakers.py` — cases (real tmp-path SQLite, `FakeEmbedder` dims, seeded through `upsert_doc` with `ChunkRecord(speakers=...)`): a chunk is found by `fts_query(..., speakers={"иван петров"})` when tagged "Иван Петров" and not when the filter names "anna" (FR-1, FR-4); the filter is casefold-insensitive for Cyrillic (`"ИВАН ПЕТРОВ"` stored, `"иван петров"` queried) (FR-1); `best_chunk_for` with a speaker filter returns a chunk tagged with that speaker even when another chunk of the doc ranks higher on the MATCH (FR-4); `exact_title_docs` / `title_trigram_query` with a filter return only docs having a tagged chunk (FR-4); `vec_query` with a filter returns only tagged chunks — skipped when `db.vec_available` is false (FR-4); a filter naming an unknown speaker returns `[]` from every read (FR-4); `known_speakers()` lists distinct keys and `known_speakers(project)` scopes them (FR-5); re-upserting a doc with different speakers and `delete_docs_not_in` leave `known_speakers()` consistent (FR-1); a file written with `user_version = 1` (write the pragma by hand on a fresh sqlite3 connection) is recreated empty on a read-write open and reports `schema_stale` on a read-only open without being modified (FR-3); `ChunkRecord()` without `speakers` still round-trips (existing callers unaffected).
- **Implement**: Bump `INDEX_SCHEMA_VERSION` to 2, add `chunk_speakers` + its index to `_SCHEMA`, extend `ChunkRecord`, write tags in `upsert_doc` (casefolded, de-duplicated), add the `speakers` parameter and the two IN-subquery shapes to the five read methods, add `known_speakers`, and compute `schema_stale` for read-only opens (read-write opens keep the existing recreate path). Keep parameters bound (`?` placeholders), no f-string values.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; existing `tests/test_index_db.py` unchanged and green; `make lint`, `make type`, `make test` pass.

### [ ] T3: Speaker mention extraction (pure logic)  [deps: —]

- **Files**: `services/transcription/src/transcription/search/speakers.py`
- **Test first**: `services/transcription/tests/test_search_speakers.py` — cases for `extract_query_speakers` with `known = {"иван петров", "ольга смирнова", "anna", "марк", "speaker 1", "speaker_02"}`: "что говорил Иван про дедлайн" → `{"иван петров"}`; "у Ивана и Ольги" → both; "Иваном" (2-letter ending) → hit, "Иванович" → no hit; "Ольге" (final `а` swapped) → hit; "какая марка машины" → `{"марк"}` is NOT required — assert "в марте" → empty (FR-5); "Иван Петров сказал" full-name match; case-insensitive Latin "ANNA" → `{"anna"}`; two known speakers "иван петров" and "иван сидоров" both returned for "Иван"; "which speakers were there" → empty (generic labels never match); text naming nobody → empty; `normalize_speaker_param`: `" Иван Петров "` → `"иван петров"`, `""`/`None`/whitespace → `None` (FR-4).
- **Implement**: Module modelled on `dates.py`: compile per-name patterns from the known set (full name whole-word; first token ≥ 3 letters with `\w{0,2}` ending and the `а`/`я` rule), skip `GENERIC_LABEL` matches, run over `text.casefold()` with `re.IGNORECASE` and Unicode word boundaries via `(?<!\w)`/`(?!\w)`. Return casefolded keys, never display names.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `make lint`, `make type`, `make test` pass.

### [ ] T2: Indexer tags chunks with their speakers and names them in the breadcrumb  [deps: T1]

- **Files**: `services/transcription/src/transcription/search/indexer.py`
- **Test first**: `services/transcription/tests/test_indexer_speakers.py` — cases (synthetic vault like `test_indexer.py`, `FakeEmbedder`, tmp-path `IndexDb`): after `index_vault`, `db.fts_query("дедлайн", 10, speakers={"speaker 1"})` finds the labelled meeting and `speakers={"nobody"}` finds nothing (FR-1); a `speakers.json` override renames the tag — the chunk is found under the operator's name and no longer under the diarization label (FR-1); the unlabelled `unsorted` transcript's chunks carry no tags (`known_speakers()` lacks them) and summary/note chunks carry none (FR-1); the stored chunk text's first line equals `[ACME / 260831 - Weekly sync / 0:00–0:04 / Speaker 1, Speaker 2]` for the fixture (read via `db.get_chunk` on the vector hit or `best_chunk_for`) (FR-2); a note's first line is still `[ACME / 260831 - Weekly sync / note]` (FR-2); the embedder received texts whose first line names the speakers (`FakeEmbedder.calls`) (FR-2); a chunk whose lines have no speaker gets a breadcrumb without the speaker segment (FR-2).
- **Implement**: Add `speaker` to `_Line`, fill it in `_transcript_lines` (same resolution as `names`), change `_chunks_from_lines` to collect distinct speakers per range and pass them to the breadcrumb callback and `ChunkRecord.speakers`; update `transcript_breadcrumb` / `flat_breadcrumb` signatures; fix the module docstring. No change to the fingerprint logic (speakers.json is already part of the hash).
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `tests/test_indexer.py` still green; `make lint`, `make type`, `make test` pass.

### [ ] T4: Speaker filter through SearchService and `POST /v1/search`  [deps: T1, T2]

- **Files**: `services/transcription/src/transcription/search/service.py`, `services/transcription/src/transcription/schema.py`, `services/transcription/src/transcription/api/search_routes.py`
- **Test first**: `services/transcription/tests/test_api_search_speakers.py` — cases (app fixture as in `test_api_search.py`, index built by `index_vault` over a vault whose two meetings have different `speaker` labels, one with a `speakers.json` override): `{"query": "дедлайн", "speaker": "Иван Петров"}` returns only the meeting where Иван speaks (FR-4); the same query without `speaker` returns both, ranked as before (FR-4); `"speaker": "   "` behaves as no filter (FR-4); `"speaker": "Nobody"` returns `{"results": []}` with 200 (FR-4); the hit's snippet with a filter comes from a chunk the speaker took part in (assert a phrase unique to that speaker's lines) (FR-4); response keys are exactly the pinned wire shape (no new field) (FR-4); a `speaker` over 200 chars is a 422.
- **Implement**: Add `speakers: set[str] | None = None` to `_ranked`/`search`/`retrieve` and pass it to every `db.*` read; add `SearchRequest.speaker`; in the `search` route convert via `normalize_speaker_param` (from T3's module — import only, T3 is in an earlier wave) into `{key}` or `None`. The `chat` route is untouched in this task (T5 owns it).
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `tests/test_api_search.py` still green; `make lint`, `make type`, `make test` pass.

### [ ] T5: Chat auto-scopes retrieval to the speaker the question names; README  [deps: T3, T4]

- **Files**: `services/transcription/src/transcription/api/search_routes.py`, `services/transcription/README.md`
- **Test first**: `services/transcription/tests/test_api_chat_speakers.py` — cases (fixtures as in `test_api_chat.py` with `FakeLlm`, vault with one meeting whose segments alternate "Иван Петров" / "Anna" and a second meeting where only "Anna" speaks): "что говорил Иван про дедлайн" yields `sources` only from the Иван meeting (FR-5); "что говорила Anna" yields sources from both meetings (FR-5); a question naming nobody yields the same sources as before the feature (FR-5); a question naming an unknown person ("что сказал Пётр") is not filtered (no known match → unscoped retrieval) (FR-5); with `project` set to a project where Иван never speaks, "Иван" is not treated as a filter (known set is project-scoped) (FR-5); the date and speaker filters compose: "что говорил Иван 260830" against a vault where Иван's meeting is 260831 yields no sources (FR-5).
- **Implement**: In the `chat` handler, inside the `run_serial` callable, fetch `manager.index_db().known_speakers(payload.project)`, run `extract_query_speakers(question, known)`, and pass `speakers=... or None` to `service.retrieve` next to `dates`. A missing/empty index means an empty known set (no filter). Update the README's "Hybrid search and the MCP server" section: per-chunk speaker tags and breadcrumb, `speaker` on `/v1/search` and `hybrid_search`, the chat's automatic speaker scope, and the one-time index rebuild on upgrade (FR-6).
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `tests/test_api_chat.py` still green; README section reflects the shipped behaviour; `make lint`, `make type`, `make test` pass.

### [ ] T6: MCP `hybrid_search` speaker argument and stale-index degradation  [deps: T1, T4]

- **Files**: `services/transcription/src/transcription/mcp_server.py`
- **Test first**: `services/transcription/tests/test_mcp_server_speakers.py` — cases (in-process `FastMCP.call_tool` via the `_call` helper pattern of `test_mcp_server.py`, index built with `FakeEmbedder`): `hybrid_search` with `speaker="Иван Петров"` returns only that speaker's meeting and without it returns all (FR-4); an unknown `speaker` returns an empty list, not an error (FR-4); an index file whose `user_version` is 1 (written by hand) makes every search tool answer the "index has not been built yet" message rather than raising (FR-3); the tool's docstring mentions `speaker` (the MCP client's only documentation — assert via the listed tool description).
- **Implement**: Add `speaker: str | None = None` to `hybrid_search`, normalize with `normalize_speaker_param`, pass `speakers={key}`; in `_Vault.index()` return `None` (and do not cache) when the read-only handle reports `schema_stale`. Extend the tool docstring.
- **Skills**: `testing-toolkit:testing-best-practices`, `testing-toolkit:python-testing-patterns`
- **Done when**: all cases green; `tests/test_mcp_server.py` still green; `uv run --directory services/transcription transcriber-mcp --help` (or an import of `build_server`) still starts without touching stdout; `make lint`, `make type`, `make test` pass.

## QA expectations

No aggregate `make qa`. The gate runs `make format`, `make lint`, `make type`, `make test` (each fans out over cargo, npm and `uv`; Rust/TS parts are untouched by this feature and must stay green). Python directly: `uv run --directory services/transcription pytest tests -q` — must stay model-free, GPU-free, network-free and under 30 s; the `gpu` marker is excluded by default. `make lint` also runs `sync_version --check`, `verify_locks --check` and `gen_diarization_runtime --check` — none are affected (no dependency or version change). Known-conditional: sqlite-vec may not load in every environment; vector-channel assertions must skip on `db.vec_available == False` as `test_index_db.py` does.

## Assumptions & decisions

- 2026-09-09 — (AUTO: codebase, `index_db.py` module policy "No ALTER TABLE, ever") How are existing indexes migrated? → `INDEX_SCHEMA_VERSION` 1 → 2; read-write opens delete and recreate, the app's startup `reindex_vault` catch-up (`commands/search.rs`, `App.tsx`) repopulates; read-only (MCP) opens report stale.
- 2026-09-09 — (AUTO: codebase, `search/dates.py` + `chat` route precedent) Explicit vs automatic speaker scoping → `/v1/search` and MCP get an explicit `speaker` parameter like `date`; the chat auto-extracts from the question like `extract_query_dates`.
- 2026-09-09 — (OPERATOR) Chat auto-scope semantics when a question names a known speaker → hard filter (only that speaker's chunks retrievable), consistent with the date filter; boost and off-by-default rejected.
- 2026-09-09 — (AUTO: operator note "prefer server-side automatic tagging that needs no UI change") Rust/React scope → none; wire shapes unchanged.
- 2026-09-09 — (AUTO: testing-toolkit:testing-best-practices) Test style → real tmp-path SQLite (managed dependency, never mocked), `FakeEmbedder` as the only stand-in (unmanaged model inference), behaviour-named cases asserting on query results, not on SQL or internals; new test files per task so no file is shared between tasks.
- 2026-09-09 — (ASSUMPTION) What does "tag embeddings with speakers" mean concretely? → both: speaker names lead the embedded chunk text (breadcrumb) **and** a per-chunk `chunk_speakers` table drives a filter; either alone does not give working speaker-scoped retrieval.
- 2026-09-09 — (ASSUMPTION) Semantics of a speaker filter over a multi-speaker chunk → a chunk qualifies if the speaker has at least one line in it (chunk-level, not line-level); the returned context still includes the other participants' surrounding lines, which is what a "what did X say about Y" question needs.
- 2026-09-09 — (ASSUMPTION) Multiple speakers in one filter → OR (union), matching how multiple dates compose today.
- 2026-09-09 — (ASSUMPTION) Name-matching rule for the chat's auto-filter → whole-word full name, or first-name token with a ≤ 2-letter Cyrillic ending (final `а`/`я` treated as an ending); generic `Speaker N` labels excluded; known set scoped to the chat's project. No stemming library.
- 2026-09-09 — (ASSUMPTION) Summary and note chunks carry no speaker tags, so a speaker-filtered query returns transcript hits only.
- 2026-09-09 — (ASSUMPTION) Speaker keys are `str.casefold()` of the display name; no transliteration or alias handling.
