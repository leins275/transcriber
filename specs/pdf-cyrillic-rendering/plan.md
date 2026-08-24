---
slug: pdf-cyrillic-rendering
status: approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Fix PDF rendering and Cyrillic output in per-meeting exports

## Architecture overview

Single-module fix in the existing PDF pipeline; no new components, no backend swap.

```
exporting.build_export_md ──> jobs.py::_export_sync ──> pdf.render_pdf ──> export.pdf
                                     │                        │
                                     │                        ├── _register_fonts()  (reportlab registry — exists)
                                     │                        └── NEW: bridge family into xhtml2pdf's font list
                                     └── NEW: font-degradation message lands in job.warnings
```

- `services/transcription/src/transcription/pdf.py` — the whole fix lives here.
  - `_register_fonts()` already registers Arial regular/bold/italic with reportlab (`pdfmetrics.registerFont` + `addMapping`, lines 40–76). Root cause (probe-verified, per spec): xhtml2pdf resolves CSS `font-family` against its **own** `fontList` (seeded from `xhtml2pdf.default.DEFAULT_FONT`, see `.venv/Lib/site-packages/xhtml2pdf/context.py` lines 583, 1046–1062), never reportlab's registry — so `Body` silently falls back to Helvetica. Fix: after reportlab registration succeeds, also add the family to xhtml2pdf's list (mutate `xhtml2pdf.default.DEFAULT_FONT` with the lowercased CSS name → registered face, inside `render_pdf`'s lazy-import scope; idempotent, process-global). `@font-face` stays banned — probe-verified broken on Windows in xhtml2pdf 0.2.17.
  - `_register_fonts()` grows a structured return (body family, mono family, degradation message or `None`) instead of a bare family string; `render_pdf` gains an **optional, backward-compatible** `warnings: list[str] | None = None` kwarg it appends degradation messages to. Backward-compatible on purpose: the `_report_sync` call site (`jobs.py` line 1002) must not be touched — sibling F7 deletes it in another worktree.
  - FR-4: register Consolas (`consola.ttf` / `consolab.ttf` / `consolai.ttf` / `consolaz.ttf`) as a `Mono` family the same way; `code`/`pre` CSS uses it when present, Courier otherwise.
- `services/transcription/src/transcription/jobs.py` — one minimal edit in `_export_sync` (lines ~1026–1031): pass `job.warnings` into `render_pdf` so font degradation surfaces on the job, not just the service log (FR-3).
- Tests — new `tests/test_pdf.py` (unit-level, real `render_pdf`) plus a shared helper module `tests/pdf_asserts.py` (pypdf-based embedded-font and text-extraction assertions; the flat `from fakes import ...` convention already used by `test_llm_jobs.py` line 18 covers this import style). Job-level regression cases extend `tests/test_llm_jobs.py`'s existing export section (lines 482–534). `pypdf` is already installed as xhtml2pdf's own dependency (`.venv/Lib/site-packages/pypdf`); it gets declared explicitly in the `dev` dependency group since tests now import it directly.

Contracts preserved: NFR-1 (pure wheels — pypdf is already in the tree), NFR-2 (all new imports stay inside `render_pdf` / `_register_fonts` lazy scope), NFR-3 (TrueType subsets + ToUnicode come free with the bridge), NFR-4 (same render path, no new work per render beyond a dict write).

## Risks

- **Merge adjacency with F7** — F7 deletes `_report_sync` (`jobs.py` ~lines 986–1006) in a sibling worktree; our `_export_sync` edit sits directly below it. Mitigation: T2 keeps the `jobs.py` diff to the minimal call-site change and does not alter `render_pdf`'s positional signature, so the untouched report call site still compiles in either merge order.
- **Process-global `DEFAULT_FONT` mutation** — mutating xhtml2pdf module state could surprise repeated renders. Mitigation: the mapping is idempotent (same key → same face) and tests render twice in-process (T1 case list).
- **False-green on machines without Arial** — FR-5 demands skip, never false-pass. Mitigation: every Arial-dependent test carries an explicit `skipif` on `%WINDIR%\Fonts\arial.ttf`; the degraded-path tests (FR-3/FR-4 fallback) monkeypatch `WINDIR` and run everywhere. Windows CI (`ci.yml`, `windows-latest`) has Arial, so CI always runs the real assertions.
- **xhtml2pdf internals drift on upgrade** — the bridge touches a semi-private surface (`xhtml2pdf.default.DEFAULT_FONT`). Mitigation: FR-5's regression test asserts the *outcome* (embedded Arial subset), so any upgrade that breaks resolution fails loudly instead of silently reverting to boxes.
- **Human-visual criterion** (FR-1 third bullet) cannot be automated. Mitigation: T4 produces a sample Russian export PDF and reports its path for the operator to open at the review gate.

## Waves

Parallelism is inherently limited: the whole fix lives in one module, so T1–T3 contend on `pdf.py`/`test_pdf.py`. T3 and T4 have disjoint file sets and independent deps, so they run in parallel.

| Wave | Tasks |
|---|---|
| 1 | T1 |
| 2 | T2 |
| 3 | T3, T4 |

## Tasks

### [ ] T1: Bridge the registered font family into xhtml2pdf's font list  [deps: —]

- **Files**: `services/transcription/src/transcription/pdf.py`, `services/transcription/tests/test_pdf.py`, `services/transcription/tests/pdf_asserts.py`, `services/transcription/pyproject.toml`
- **Test first**: `services/transcription/tests/pdf_asserts.py` — pypdf helpers: `embedded_base_fonts(pdf_path)` (walk page `/Resources` → `/Font` → `/BaseFont`, return the set of names) and `extract_text(pdf_path)`. `services/transcription/tests/test_pdf.py` — cases (all Arial-dependent ones `skipif` `%WINDIR%\Fonts\arial.ttf` missing, per FR-5 "skips cleanly, not falsely passes"):
  - Cyrillic markdown exercising a heading, paragraph, list item, table cell and transcript-style line (`Привет, мир` et al.) through the real `render_pdf` → embedded fonts include a TrueType subset of Arial (name matching `+ArialMT`), and no body run resolves to base-14 `Helvetica` (FR-1, FR-5).
  - Text extraction from that PDF returns the Cyrillic strings intact — no U+25A0 boxes, no mojibake (FR-1, NFR-3).
  - `**Жирный**` and `*курсив*` produce embedded subsets of `Arial-BoldMT` and `Arial-ItalicMT` respectively (FR-2).
  - Rendering twice in one process succeeds identically (guards the idempotency of the global-state bridge).
  - Fresh-subprocess check (`uv run python -c "..."` via `sys.executable`): `import transcription.pdf` leaves `reportlab` and `xhtml2pdf` out of `sys.modules` (NFR-2).
- **Implement**: In `render_pdf`'s lazy-import scope, after `_register_fonts()` returns a registered family, add it to xhtml2pdf's resolution list — `xhtml2pdf.default.DEFAULT_FONT[family.lower()] = <registered reportlab face>` (the probe-verified bridge; xhtml2pdf's `Context` seeds its `fontList` from `DEFAULT_FONT` and never consults reportlab's registry). Do NOT introduce `@font-face` (broken on Windows, xhtml2pdf 0.2.17). Declare `pypdf>=3` in the `[dependency-groups] dev` list of `services/transcription/pyproject.toml` (already installed transitively via xhtml2pdf; tests now import it directly).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests fail against the unbridged code and pass after; `make format`, `make lint`, `make type`, `make test` all pass.

### [ ] T2: Surface font degradation on the export job's warnings  [deps: T1]

- **Files**: `services/transcription/src/transcription/pdf.py`, `services/transcription/src/transcription/jobs.py`, `services/transcription/tests/test_pdf.py`, `services/transcription/tests/test_llm_jobs.py`
- **Test first**: `services/transcription/tests/test_pdf.py` — cases:
  - With `WINDIR` monkeypatched to a fontless tmp dir, `render_pdf(..., warnings=w)` still succeeds (PDF written, `%PDF` magic) and `w` gains a message naming the font degradation (FR-3).
  - With real fonts present, `w` stays empty (`skipif` Arial absent) (FR-3).
  `services/transcription/tests/test_llm_jobs.py` — case (export section, after line 534):
  - Export job with `WINDIR` monkeypatched fontless: job status `succeeded`, `export.pdf` exists, and `job.warnings` contains the degradation message — not just a log line (FR-3 acceptance).
- **Implement**: Refactor `_register_fonts()` to return a small `NamedTuple`/dataclass (body family, degradation message or `None`); add optional keyword `warnings: list[str] | None = None` to `render_pdf` and append the message there. In `jobs.py::_export_sync` (lines ~1026–1031) pass `warnings=job.warnings`. Do not touch `_report_sync` or its `render_pdf` call at line 1002 (F7 deletes it in a sibling worktree); keep the signature change purely additive so that call site compiles unmodified.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests fail before / pass after; existing export-job test (`test_export_assembles_sections_in_order_and_renders_a_pdf`) still passes within its current timeout (NFR-4); `make format`, `make lint`, `make type`, `make test` pass.

### [ ] T3: Cyrillic-capable monospace for code spans and blocks  [deps: T2]

- **Files**: `services/transcription/src/transcription/pdf.py`, `services/transcription/tests/test_pdf.py`
- **Test first**: `services/transcription/tests/test_pdf.py` — cases:
  - A fenced code block and inline code containing Cyrillic render with an embedded Consolas subset when `%WINDIR%\Fonts\consola.ttf` is present (`skipif` when absent) and extraction returns the Cyrillic intact (FR-4).
  - With `WINDIR` monkeypatched fontless, the same document still renders successfully (Courier fallback, Latin-legible; no exception, valid PDF) (FR-4).
- **Implement**: Extend `_register_fonts()` to also register Consolas (`consola.ttf`, bold `consolab.ttf`, italic `consolai.ttf`, bold-italic `consolaz.ttf`) as a `Mono` family via the same `pdfmetrics.registerFont` + `addMapping` + `DEFAULT_FONT` bridge, returning the mono family name (Courier when absent); `render_pdf` swaps the hardcoded `font-family: Courier` in `code`/`pre` CSS (`_BASE_CSS` lines 32–33) for the resolved mono family.
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: new tests fail before / pass after; `make format`, `make lint`, `make type`, `make test` pass.

### [ ] T4: Job-level Cyrillic regression through the real export path  [deps: T2]

- **Files**: `services/transcription/tests/test_llm_jobs.py`
- **Test first**: this task IS the test. `services/transcription/tests/test_llm_jobs.py`, export section — new case `test_export_renders_cyrillic_with_embedded_fonts` (`skipif` Arial absent, per FR-5):
  - Seed the `meeting_dir` fixture with Russian content in all four sections — `summary.md`, an action item (`write_item`), a fact, and Cyrillic transcript segment text — run the real export job through `JobManager`, and assert on `export.pdf` via `pdf_asserts`: embedded fonts include the Arial TrueType subset (FR-1, FR-5) and extracted text returns the Russian strings from every section intact (FR-1, NFR-3).
  - The job completes within the existing generous first-render timeout (`_wait_until_terminal`, 60 s) — no timeout change permitted (NFR-4).
- **Implement**: Test-only task; reuse `pdf_asserts` helpers (flat import, same as `from fakes import ...`). Per the desktop profile's verification rule, the affected flow is this backend export job (no UI is touched), and driving it through the real `JobManager` is the flow-level check. Additionally render one sample Russian export and report its filesystem path in the task summary so the operator can open it at the review gate (FR-1 human-visual criterion).
- **Skills**: `testing-toolkit:python-testing-patterns`
- **Done when**: the new test fails when T1's bridge is reverted (spot-check) and passes on current code; full `make test` green on Windows; sample PDF path reported for operator review.

## QA expectations

- All four Makefile targets exist and are required per task: `make format`, `make lint`, `make type`, `make test` (each fans out to cargo + npm + uv; only the uv/Python leg is affected by this feature).
- Python CI runs on `windows-latest` (`.github/workflows/ci.yml`), where `arial.ttf` and `consola.ttf` are guaranteed — the skip guards exist for non-Windows dev machines only and must skip, never false-pass (FR-5).
- Known timing note: the first PDF render in a test session pays a multi-second lazy xhtml2pdf/reportlab import (documented at `test_llm_jobs.py` lines 77–78); the existing 60 s terminal-wait absorbs it and must not be raised (NFR-4).
- No UI tasks — `frontend-toolkit:internal-ui` (mandatory for UI work) is intentionally unused, as the spec anticipates.
