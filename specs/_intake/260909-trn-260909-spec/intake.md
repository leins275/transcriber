---
batch: 260909-trn-260909-spec
source: D:\Local\Git\transcriber\local\TRN - 260909 - spec.md
created: 2026-09-09
status: approved
---

# Intake: TRN 260909 spec — search index, embeddings, progress and speaker UX

## Source

A plain-markdown operator note (`local/TRN - 260909 - spec.md`, Russian) listing six numbered items, each a `#` heading with optional `-` sub-bullets giving the rationale. No embedded or linked media, no screenshots alongside it, so no extraction step was needed — the document was read directly and `specs/_intake/260909-trn-260909-spec/media/` stays empty. The six items are a mix of one storage question (already-shipped behaviour, see below), one indexing improvement, one progress-reporting complaint and three speaker-UX issues in the desktop app.

## Features

### [p] F1: Store search indexes in the vault, not the app folder  (slug: search-index-in-vault)

**Task text** (verbatim from the source):

> # 1. Хранить индексы в волте, а не в папке приложения

**Attachments**: none.

Notes for the gate: **possibly already implemented on main.** `services/transcription/src/transcription/config.py:421-428` already defaults `index_db_path` to `<vault_root>/.transcriber/index.sqlite3`, with the `<app_dir>/data/index.sqlite3` fallback used only when no vault root is configured. Candidate kept so the operator can drop it explicitly, or narrow it to a residual (e.g. migrating an existing app-dir index into the vault, or removing the app-dir fallback).

### [p] F2: Tag embeddings with speakers  (slug: speaker-tagged-embeddings)

**Task text** (verbatim from the source):

> # 2. Тегировать эмбеддинги дополнительно спикерами

**Attachments**: none.

Scope hint: the chunk breadcrumb built in `services/transcription/src/transcription/search/indexer.py` (`_chunks_from_lines`, `transcript_breadcrumb`) and the doc-level `speakers` column in `search/index_db.py` are the existing surfaces; the ask is that speaker identity travels with the embedded chunk, not only with the document row.

### [p] F3: Make job progress percentages honest for non-transcribe jobs  (slug: job-progress-accuracy)

**Task text** (verbatim from the source):

> # 3. Прогресс бар джоб в процентах по ощущениям нормально показывает только для транскрипта
> - Для остальных задач шкала как будто неравномерная/кривая

**Attachments**: none.

Scope hint: summarize/export/index/diarize progress is currently set from coarse hand-picked fractions in `services/transcription/src/transcription/jobs.py` (`job.progress = 0.05 / 0.1 / 0.5 / 0.9`, plus the 0.9 diarization scale), while transcription drives a real per-second fraction.

### [p] F4: Keep the selection speaker popover inside the window  (slug: selection-menu-viewport-clamp)

**Task text** (verbatim from the source):

> # 4. Предложение спикеров при выделении транскрипта мышкой вываливается иногда за границы окна
> - UI бага, неудобно

**Attachments**: none.

Scope hint: `apps/desktop/src/components/SelectionSpeakerMenu.tsx` positions the popover with raw `left: anchor.x; top: anchor.y` viewport coordinates and never measures itself against the viewport, so a selection near the right or bottom edge pushes it off-screen.

### [p] F5: Rename one turn's speaker without renaming every turn  (slug: per-turn-speaker-reassign)

**Task text** (verbatim from the source):

> # 6. При переименовании спикеров можно переименовать все реплики спикера, в то время как я хотел бы только текущую реплику изменить.
> - Нужно сделать UI более прозрачным и удобным в этом смысле

**Attachments**: none.

Scope hint: `apps/desktop/src/components/SpeakerTag.tsx` deliberately resolves "edit an existing name" as rename-everywhere (`onRename`), leaving reassign-this-turn (`onAssign`) reachable only by clicking an already-known name. The ask is to make both intents explicit and reachable.

### [p] F6: Constrain speaker choice to a per-project roster  (slug: project-speaker-roster)

**Task text** (verbatim from the source):

> # 5. Транскрипт плодит много спикеров
> - Иногда это полезно, особенно если проект только стартует и людей много участвует.
> - Но чаще - список людей на проекте фиксирован
> - Было бы круто как-то ограничить список допустимых спикеров сразу для проекта, чтобы мы выбирали только из доступных вариантов

**Attachments**: none.

Scope hint: today project-level names are an on-demand sibling-meeting scan surfaced as a free-text `datalist` in both `SpeakerTag` and `SelectionSpeakerMenu`; this asks for a maintained, authoritative per-project list to pick from. Touches the same two components as F4 and F5, so it is sequenced last.

## Unassigned content

None — every heading and sub-bullet in the source is carried by a candidate above.

## Decisions log

- 2026-09-09 — Split gate: six features as proposed (F1 kept, flagged possibly-implemented; items 5/6 reordered to F6/F5; F5+F6 kept separate) → Approved
- 2026-09-09 — F1 blueprint gate → Approved (discard old app-dir index, no migration)
- 2026-09-09 — F2 chat auto-scope: hard filter / boost / off → Hard filter; F2 blueprint gate → Approved
- 2026-09-09 — F3 multi-phase presentation: per-phase / weighted / indeterminate → Per-phase bar; F3 blueprint gate → Approved
- 2026-09-09 — F4 blueprint gate (no open questions) → Approved
- 2026-09-09 — F5 intent shape: chooser after Enter / separate icon / confirm → Scope chooser after Enter; F5 blueprint gate → Approved
- 2026-09-09 — F6 roster strictness → Strict; F6 seeding → Manual button; F6 blueprint gate → Approved
- 2026-09-09 — Operator: stop after blueprints (usage limit); factory not started. Resume with `/sdd:ship 260909-trn-260909-spec`.
