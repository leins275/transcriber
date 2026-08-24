---
slug: pdf-cyrillic-rendering
created: 2026-08-24
status: approved
---

# Spec: Fix PDF rendering and Cyrillic output in per-meeting exports

## Summary

The per-meeting PDF export (`export.pdf`) renders every Cyrillic character as a black box because the Cyrillic-capable font the service registers is never actually used by the PDF backend. Fix the font pipeline so Russian (and mixed Russian/English) exports render with real glyphs, correct bold/italic, and a loud job warning when font registration degrades. Scope is the export job only — the report job is deleted by sibling feature F7 (`project-view-recordings-only`).

## Problem & context

- `services/transcription/src/transcription/pdf.py` — `_register_fonts()` registers Arial from `%WINDIR%\Fonts` into **reportlab's** font registry (`pdfmetrics.registerFont` + `addMapping`) and returns family name `Body`, which `render_pdf()` injects into the CSS.
- **Root cause (verified by probe on this machine)**: xhtml2pdf does not consult reportlab's registry when resolving CSS `font-family`. `xhtml2pdf.context.Context` resolves names against its own `fontList`, initialized from `xhtml2pdf.default.DEFAULT_FONT` (`.venv/Lib/site-packages/xhtml2pdf/context.py`, lines 583, 1046–1062) and extended only by CSS `@font-face`. `Body` is not in that list, so every text run silently falls back to built-in Helvetica — which has no Cyrillic glyphs. A probe render of Cyrillic markdown through the project's own `render_pdf` produced a PDF embedding only `Helvetica`/`ZapfDingbats` despite `_register_fonts()` returning `Body`.
- The `@font-face` route is genuinely broken on Windows in xhtml2pdf 0.2.17 (verified: `NamedTemporaryFile` is re-opened while still open → `PermissionError`), confirming the module docstring's warning — so the original authors' avoidance of `@font-face` was correct; the registry route just lacked the one bridge xhtml2pdf needs.
- **Verified fix direction**: registering the family in reportlab *and* adding it to xhtml2pdf's font list (e.g. `DEFAULT_FONT["body"] = "Body"`, or `Context.registerFont`) makes the same render embed true Arial subsets (`ArialMT`, `Arial-BoldMT`, `Arial-ItalicMT`) with correct Cyrillic output. A backend swap is not required.
- Secondary defect: when font registration fails, `_register_fonts()` only writes a service log line; the export job succeeds with no warning, so the operator gets a silently broken PDF (`jobs.py` `_export_sync`, lines ~1008–1030 attach only `PdfRenderError` to `job.warnings`).
- Consumer in scope: `services/transcription/src/transcription/exporting.py` (`build_export_md`) → `jobs.py::_export_sync` → `render_pdf` → `export.pdf` next to `export.md`.
- Consumer **out** of scope: `llm/report.py` / `_report_sync` (`jobs.py` line ~1002) — deleted entirely by F7 per the batch gate decision.

## Users

- The single desktop-app operator, exporting a meeting (summary + action items + facts + transcript, routinely Russian) to PDF for reading/sharing outside the app.

## Profiles

- `desktop` — `apps/desktop/src-tauri/tauri.conf.json` exists (Tauri); NSIS packaging via `installer/installer_hooks.nsh` and the tauri bundle config.
- `web` — `apps/desktop/package.json` names `react` ^18.3.1 and `vite` ^5.4.10 (webview UI; per the desktop profile, UI toolkits come from `web`). This feature itself touches no UI.

## Detected stack

| Layer | Technology | Evidence |
|---|---|---|
| Desktop shell | Tauri (Rust) | `apps/desktop/src-tauri/tauri.conf.json`, `Cargo.toml` workspace |
| Frontend | React 18 + Vite 5 (webview) | `apps/desktop/package.json` |
| Backend service | Python 3.12 (`transcription` package) | `services/transcription/pyproject.toml` |
| PDF pipeline | `markdown` >=3.6 + `xhtml2pdf` >=0.2.16 (0.2.17 installed) on reportlab | `services/transcription/pyproject.toml` lines 16–17; `src/transcription/pdf.py` |
| Testing | pytest (Python, Windows CI), cargo test, npm test | `Makefile` `test` target; `.github/workflows/ci.yml` (python job `runs-on: windows-latest`) |
| Packaging | NSIS installer, Windows-x86_64 release | `installer/installer_hooks.nsh`, `.github/workflows/release.yml` matrix |

Makefile QA targets present: format, lint, type, test (all four; each fans out to cargo + npm + uv).

## Functional requirements

- **FR-1** (must): Cyrillic text anywhere in the export document (headings, paragraphs, list items, table cells, transcript lines) renders as real glyphs in `export.pdf` — the registered Cyrillic-capable family is the font the PDF backend actually uses, not a silent Helvetica substitution.
- **FR-2** (must): Bold and italic runs (`**…**`, `*…*` in the markdown) render in the bold/italic variants of the same Cyrillic-capable family, for Cyrillic and Latin text alike.
- **FR-3** (must): When no Cyrillic-capable font can be registered (e.g. `arial.ttf` absent), the render still succeeds with legible Latin output **and** the export job's `warnings` list tells the operator the PDF degraded to a Latin-only font — not just a service log line.
- **FR-4** (should): Inline code and fenced code blocks use a Cyrillic-capable monospace font when one is available in `%WINDIR%\Fonts` (Consolas: `consola.ttf` family), falling back to Courier otherwise.
- **FR-5** (should): A regression test renders Cyrillic markdown through the real export path and fails if the produced PDF does not embed the Cyrillic-capable font (i.e. would catch this bug's recurrence, including an xhtml2pdf upgrade that changes font resolution).

## Non-functional requirements

- **NFR-1**: Pure-Python wheels only — no external binaries, no system installs beyond fonts already present in `%WINDIR%\Fonts` (preserves the repo's existing no-external-binaries rule cited in `pdf.py`'s docstring).
- **NFR-2**: `import transcription.pdf` still imports neither reportlab nor xhtml2pdf (lazy-import contract of the module is preserved).
- **NFR-3**: Text in the produced PDF is selectable and copyable — extraction yields the original Cyrillic characters (embedded TrueType subsets with ToUnicode maps, which the verified fix provides for free).
- **NFR-4**: The existing export-job test still completes within its current generous first-render timeout; font subsetting must not blow it up.

## Acceptance criteria

- **FR-1**:
  - [ ] Rendering an export document containing `Привет, мир` (in a heading, a paragraph, a list item and a transcript line) produces `export.pdf` whose embedded fonts include a TrueType subset of the registered family (e.g. `/BaseFont /XXXXXX+ArialMT`), and body text is not set in base-14 Helvetica.
  - [ ] Text extraction from that PDF returns the Cyrillic strings intact (no U+25A0-style boxes, no mojibake).
  - [ ] A human-opened sample export of a real Russian meeting shows readable Russian throughout all four sections (Summary, Action items, Facts, Transcript).
- **FR-2**:
  - [ ] Markdown `**Жирный**` / `*курсив*` produce runs in the bold / italic font variants (embedded subsets of e.g. `Arial-BoldMT`, `Arial-ItalicMT`), verified by inspecting the PDF's embedded fonts.
- **FR-3**:
  - [ ] With `WINDIR` pointed at a directory without `arial.ttf` (monkeypatched in a test), the export job still succeeds, `export.pdf` exists, and `job.warnings` contains a message naming the font degradation.
  - [ ] With fonts present, no such warning appears.
- **FR-4**:
  - [ ] A fenced code block containing Cyrillic renders with real glyphs when Consolas is present; with it absent the render still succeeds (Courier, Latin-legible).
- **FR-5**:
  - [ ] `make test` includes a test that renders Cyrillic through `render_pdf` (or the export job) and asserts the embedded-font condition of FR-1; it runs on the project's Windows CI (`ci.yml` python job) and skips cleanly, not falsely passes, where `arial.ttf` is unavailable.

## Out of scope

- The `report` job, `llm/report.py`, `report.pdf` and all report UI — deleted by F7 (`project-view-recordings-only`); binding batch-gate decision.
- Any desktop UI changes — the export flow's UI is untouched.
- Swapping the PDF backend (weasyprint, fpdf2, headless-browser printing) — see Decisions log.
- Bundling font files with the app or non-Windows font provisioning; platform scope is Windows (the shipped app targets `windows-x86_64` only).
- Page headers/footers, page numbers, cover pages or other layout features not present today.
- The export document's content/section structure (`exporting.py` assembly logic) — only its rendering.

## Applicable toolkits

- `testing-toolkit:python-testing-patterns` — Tests layer; signal: pytest suite in `services/transcription/tests` (desktop + web profiles).
- `frontend-toolkit:internal-ui` — UI (internal) layer; signal: React + Vite in `apps/desktop/package.json`, staff-facing (single-operator tool). Listed for completeness; this feature plans no UI tasks.
- `frontend-toolkit:ui-ux-pro-max` — same UI signal as above.
- `devops-toolkit:devops-rollout-plan` — Packaging layer; signal: Tauri bundle config + `installer/installer_hooks.nsh`.

(Not applicable — signal absent: Django rows, Playwright/E2E rows, Docker, PostgreSQL.)

**Mandatory skills**:

- `frontend-toolkit:internal-ui` — mandatory on every internal-tool UI task (carried from the `web` profile via `desktop`; this feature is expected to produce none).

## Strict skills

**Planning** (spec-analyst, architect):

- none

**Development** (implementer, fixer, evaluator, UI validation):

- none

## Open questions

None — root cause, fix direction and platform scope were all determinable from the codebase and verified by probes; see Decisions log.

## Decisions log

- 2026-08-24 — (OPERATOR, batch gate) F7 deletes the `report` job type and `llm/report.py`; F4 is scoped to the per-meeting export path only.
- 2026-08-24 — (AUTO: verified probe) Fix vs backend swap → **keep xhtml2pdf, fix the font bridge**. Probes on this machine proved: (a) current code silently falls back to Helvetica because xhtml2pdf resolves `font-family` against its own `fontList`, never reportlab's registry; (b) xhtml2pdf 0.2.17 `@font-face` is broken on Windows (temp-file `PermissionError`), so the docstring's avoidance of it stands; (c) registering the family in reportlab **and** in xhtml2pdf's font list yields correctly embedded Arial regular/bold/italic subsets with real Cyrillic. Alternatives rejected: weasyprint needs native Pango/GTK DLLs (violates the no-external-binaries rule), fpdf2 would discard the whole markdown→HTML→CSS pipeline for a one-line-class bug.
- 2026-08-24 — (AUTO: repo evidence) Platform scope → Windows only. The release matrix ships `windows-x86_64` exclusively and the Python CI job runs `windows-latest`; Arial is guaranteed present there. Non-Windows keeps the existing legible Latin degradation, now surfaced as a job warning (FR-3).
