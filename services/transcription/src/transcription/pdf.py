"""Markdown -> PDF rendering (pure-python: ``markdown`` + ``xhtml2pdf``).

No external binaries (the FR-7 rule that also keeps ffmpeg out): xhtml2pdf
sits on reportlab, all wheels. Both libraries are imported lazily inside
:func:`render_pdf` so importing this module costs nothing (NFR-1).

Font note: reportlab's built-in Helvetica has no Cyrillic glyphs, and these
documents are routinely Russian. On Windows the stock ``arial.ttf`` family
covers Cyrillic, so it is registered from ``%WINDIR%\\Fonts`` when present;
otherwise the built-ins remain (Latin-only output degrades legibly rather
than failing).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "sane_lists"]

_BASE_CSS = """
@page { size: a4 portrait; margin: 2cm 1.6cm; }
body { font-size: 10.5pt; line-height: 1.45; }
h1 { font-size: 18pt; margin: 0 0 8pt 0; }
h2 { font-size: 14pt; margin: 14pt 0 6pt 0; border-bottom: 1pt solid #999; }
h3 { font-size: 12pt; margin: 10pt 0 4pt 0; }
p { margin: 0 0 6pt 0; }
li { margin: 0 0 3pt 0; }
code { font-family: Courier; font-size: 9pt; }
pre { font-family: Courier; font-size: 9pt; background-color: #f2f2f2; padding: 4pt; }
table { border-collapse: collapse; margin: 6pt 0; }
td, th { border: 0.5pt solid #999; padding: 3pt 5pt; font-size: 9.5pt; }
img { max-width: 480pt; margin: 4pt 0; }
"""


def _register_fonts() -> str:
    """Register a Cyrillic-capable font family with reportlab; returns the
    family name to use in CSS.

    Registered through reportlab's own registry (not CSS ``@font-face``,
    whose xhtml2pdf handling round-trips through a temp file that breaks on
    Windows). Falls back to the built-in Helvetica -- Latin-only, degrading
    legibly -- when the Windows fonts are absent.
    """
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    regular = fonts_dir / "arial.ttf"
    if not regular.is_file():
        return "Helvetica"

    from reportlab.lib.fonts import addMapping  # noqa: PLC0415 - lazy (NFR-1)
    from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415 - lazy (NFR-1)
    from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415 - lazy (NFR-1)

    try:
        pdfmetrics.registerFont(TTFont("Body", str(regular)))
        variants = {
            (1, 0): fonts_dir / "arialbd.ttf",
            (0, 1): fonts_dir / "ariali.ttf",
            (1, 1): fonts_dir / "arialbi.ttf",
        }
        addMapping("Body", 0, 0, "Body")
        for (bold, italic), path in variants.items():
            name = f"Body-{bold}{italic}"
            if path.is_file():
                pdfmetrics.registerFont(TTFont(name, str(path)))
                addMapping("Body", bold, italic, name)
            else:
                addMapping("Body", bold, italic, "Body")
    except Exception:  # noqa: BLE001 - a bad font file degrades, never fails the render
        logger.warning("failed to register the system font; falling back to Helvetica")
        return "Helvetica"
    return "Body"


class PdfRenderError(Exception):
    """The PDF backend reported errors; the caller decides whether to degrade."""


def render_pdf(md_text: str, out_path: Path, *, base_dir: Path) -> Path:
    """Render ``md_text`` to ``out_path``; relative links resolve against ``base_dir``.

    Raises :class:`PdfRenderError` on failure; the caller (the export/report
    jobs) degrades -- the ``.md`` stays the deliverable and the failure lands
    in the job's warnings, never failing the job.
    """
    import markdown  # noqa: PLC0415 - deliberate lazy import (NFR-1)
    from xhtml2pdf import pisa  # noqa: PLC0415 - deliberate lazy import (NFR-1)

    body_html = markdown.markdown(md_text, extensions=_MARKDOWN_EXTENSIONS)
    family = _register_fonts()
    font_css = f"body, h1, h2, h3, td, th, li, p {{ font-family: {family}; }}"
    html = (
        "<html><head><meta charset='utf-8'><style>"
        + font_css
        + _BASE_CSS
        + "</style></head><body>"
        + body_html
        + "</body></html>"
    )

    def link_callback(uri: str, rel: str) -> str:
        # Fonts and screenshots arrive as file paths (absolute for fonts,
        # relative for images embedded from the markdown); resolve the
        # relative ones against the document's own directory.
        candidate = Path(uri)
        if not candidate.is_absolute():
            candidate = (base_dir / uri).resolve()
        return str(candidate)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".pdf.tmp")
    try:
        with tmp_path.open("wb") as handle:
            status = pisa.CreatePDF(
                html, dest=handle, encoding="utf-8", link_callback=link_callback
            )
        if status.err:
            raise PdfRenderError(f"xhtml2pdf reported {status.err} error(s)")
        os.replace(tmp_path, out_path)
    except PdfRenderError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise PdfRenderError(str(exc)) from exc
    return out_path
