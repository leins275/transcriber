---
slug: pdf-cyrillic-rendering
base_ref: 4098ac7a2057b86f72fe89b7e96aa5b335e7df56
round: 1
---

# Evaluation report: Fix PDF rendering and Cyrillic output in per-meeting exports

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 0 | 0 | 0 |
| major | 0 | 0 | 0 |
| minor | 0 | 0 | 0 |

The diff implements the spec faithfully and completely. The probe-verified fix direction (reportlab registration + `DEFAULT_FONT` bridge, no `@font-face`) is exactly what was built; the change surface is confined to `pdf.py`, one call-site edit in `jobs.py::_export_sync`, tests, and the pypdf dev dependency. All five FRs and all four NFRs are implemented and covered by tests that assert outcomes (embedded font subsets, extracted text), not internals. Independently verified on this machine: the full Python suite passes (only two pre-existing symlink skips, unrelated), `ruff` and `mypy src` are clean, the human-review sample PDF exists and paints every text run in Arial/Consolas subsets with intact Cyrillic extraction, and — via a scratch simulation that neutralizes the bridge — the regression tests genuinely fail in the unbridged world (render collapses to Helvetica/ZapfDingbats), satisfying FR-5's "catches recurrence" promise. `_report_sync` (F7's territory) is untouched, and the `render_pdf` signature change is purely additive as the plan required.

## Findings

None.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (Cyrillic glyphs, real family used) | `services/transcription/src/transcription/pdf.py:172` (DEFAULT_FONT bridge), `pdf.py:75-108` (registration) | `tests/test_pdf.py::test_cyrillic_body_text_is_rendered_in_an_embedded_arial_subset`, `::test_cyrillic_text_extracts_back_intact`; job-level `tests/test_llm_jobs.py::test_export_renders_cyrillic_with_embedded_fonts`; human sample produced (verified: only Arial/Consolas subsets paint text) | ✓ |
| FR-2 (bold/italic variants) | `pdf.py:94-104` (`addMapping` regular/bold/italic/bold-italic) | `tests/test_pdf.py::test_bold_and_italic_use_the_matching_arial_variants` | ✓ |
| FR-3 (degradation → job warnings, render still succeeds) | `pdf.py:125-133,162-164`; `jobs.py:1027-1035` (`warnings=job.warnings`) | `tests/test_pdf.py::test_a_fontless_machine_still_renders_and_reports_the_degradation`, `::test_no_warning_is_reported_when_the_font_family_registers`; job-level `tests/test_llm_jobs.py::test_export_warns_on_the_job_when_the_pdf_font_degrades` | ✓ |
| FR-4 (Cyrillic-capable monospace, Courier fallback) | `pdf.py:67-72,120-123,173,176` | `tests/test_pdf.py::test_code_spans_and_blocks_use_an_embedded_consolas_subset`, `::test_cyrillic_inside_code_extracts_back_intact`, `::test_a_fontless_machine_still_renders_code_via_the_builtin_monospace` | ✓ |
| FR-5 (regression test, skips cleanly without Arial) | `tests/pdf_asserts.py` helpers | `tests/test_llm_jobs.py::test_export_renders_cyrillic_with_embedded_fonts` + unit tests, all under `requires_arial` skipif; teeth verified empirically (bridge neutralized → both assertions fail) | ✓ |
| NFR-1 (pure wheels) | `pyproject.toml` — pypdf (pure Python, already in tree) added to dev group only | inspection; no runtime dependency change | ✓ |
| NFR-2 (lazy imports) | `pdf.py:157-159` (in-function imports; top level imports only stdlib) | `tests/test_pdf.py::test_importing_the_module_pulls_in_neither_reportlab_nor_xhtml2pdf` (fresh subprocess) | ✓ |
| NFR-3 (selectable/copyable text) | TrueType subsets + ToUnicode via the bridge | extraction assertions in `test_cyrillic_text_extracts_back_intact`, `test_cyrillic_inside_code_extracts_back_intact`, job-level test | ✓ |
| NFR-4 (timeout unchanged) | no timeout edits anywhere in the diff | existing `test_export_assembles_sections_in_order_and_renders_a_pdf` passes in full-suite run | ✓ |

## Positive notes

- `pdf_asserts.fonts_drawing_text` distinguishes fonts that *paint* text from fonts merely declared in page resources — this matters in practice (the sample PDF declares an unused base-14 `Helvetica` resource that would false-fail a naive "no Helvetica anywhere" assertion). Keep this helper as-is.
- The degraded-path `DEFAULT_FONT` writes (`helvetica`→`Helvetica`, `courier`→`Courier`) were checked against xhtml2pdf 0.2.17's shipped defaults and are exact no-ops — the process-global mutation is genuinely idempotent in every branch, and the render-twice test guards it.
- `_register_family` maps missing bold/italic files back to the regular face instead of failing the family, and a bad font file degrades (with a reason string) rather than raising — good resilience for a background job.
- The `render_pdf` signature change is keyword-only and optional, so the untouched `_report_sync` call site (`jobs.py:1002`) compiles unmodified in either F7 merge order, exactly as the plan's merge-adjacency mitigation demanded.
- Test warnings assertions key on the spec-mandated content ("font", "latin") rather than exact phrasing, and the fontless tests run everywhere (no skip), so non-Windows dev machines still exercise the degradation path.
