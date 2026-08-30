"""Markdown -> PDF rendering (pure-python: ``markdown`` + ``xhtml2pdf``).

No external binaries (the FR-7 rule that also keeps ffmpeg out): xhtml2pdf
sits on reportlab, all wheels. Both libraries are imported lazily inside
:func:`render_pdf` so importing this module costs nothing (NFR-1).

Font note: reportlab's built-in Helvetica and Courier have no Cyrillic
glyphs, and these documents are routinely Russian. On Windows the stock
``arial.ttf`` (body) and ``consola.ttf`` (code) families cover Cyrillic, so
they are registered from ``%WINDIR%\\Fonts`` when present; otherwise the
built-ins remain (Latin-only output degrades legibly rather than failing).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "sane_lists"]

# No ``font-family`` here on purpose: the body and monospace families are only
# known once :func:`_register_fonts` has run, so ``render_pdf`` prepends them.
_BASE_CSS = """
@page { size: a4 portrait; margin: 2cm 1.6cm; }
body { font-size: 10.5pt; line-height: 1.45; }
h1 { font-size: 18pt; margin: 0 0 8pt 0; }
h2 { font-size: 14pt; margin: 14pt 0 6pt 0; border-bottom: 1pt solid #999; }
h3 { font-size: 12pt; margin: 10pt 0 4pt 0; }
p { margin: 0 0 6pt 0; }
li { margin: 0 0 3pt 0; }
code { font-size: 9pt; }
pre { font-size: 9pt; background-color: #f2f2f2; padding: 4pt; }
table { border-collapse: collapse; margin: 6pt 0; }
td, th { border: 0.5pt solid #999; padding: 3pt 5pt; font-size: 9.5pt; }
img { max-width: 480pt; margin: 4pt 0; }
"""


class _FontChoice(NamedTuple):
    """What :func:`_register_fonts` settled on.

    ``family`` dresses the body text, ``mono`` the code spans and blocks.
    ``warning`` is ``None`` on the happy path and an operator-facing sentence
    when the body family degraded, so the caller can put it somewhere the
    operator actually looks (the job's warnings) instead of only the service
    log. A degraded monospace costs no warning: code is near-always Latin and
    Courier reads fine.
    """

    family: str
    mono: str
    warning: str | None = None


# The stock Windows faces, keyed by (bold, italic). Arial and Consolas both
# carry Cyrillic; reportlab's built-in Helvetica/Courier do not.
_BODY_FILES = {
    (0, 0): "arial.ttf",
    (1, 0): "arialbd.ttf",
    (0, 1): "ariali.ttf",
    (1, 1): "arialbi.ttf",
}
_MONO_FILES = {
    (0, 0): "consola.ttf",
    (1, 0): "consolab.ttf",
    (0, 1): "consolai.ttf",
    (1, 1): "consolaz.ttf",
}


def _register_family(family: str, fonts_dir: Path, files: dict[tuple[int, int], str]) -> str | None:
    """Register ``files`` under ``family`` with reportlab.

    Returns ``None`` once the family is usable, or a short phrase saying why
    it is not. Registered through reportlab's own registry (not CSS
    ``@font-face``, whose xhtml2pdf handling round-trips through a temp file
    that breaks on Windows). A missing variant maps back to the regular face
    rather than failing the family.
    """
    regular = fonts_dir / files[0, 0]
    if not regular.is_file():
        return f"{regular.name} is not present in {fonts_dir}"

    from reportlab.lib.fonts import addMapping  # noqa: PLC0415 - lazy (NFR-1)
    from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415 - lazy (NFR-1)
    from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415 - lazy (NFR-1)

    try:
        pdfmetrics.registerFont(TTFont(family, str(regular)))
        addMapping(family, 0, 0, family)
        for (bold, italic), filename in files.items():
            if (bold, italic) == (0, 0):
                continue
            path = fonts_dir / filename
            name = f"{family}-{bold}{italic}"
            if path.is_file():
                pdfmetrics.registerFont(TTFont(name, str(path)))
                addMapping(family, bold, italic, name)
            else:
                addMapping(family, bold, italic, family)
    except Exception:  # noqa: BLE001 - a bad font file degrades, never fails the render
        logger.warning("failed to register %s from %s", family, regular)
        return f"{regular.name} in {fonts_dir} could not be registered"
    return None


def _register_fonts() -> _FontChoice:
    """Register the Cyrillic-capable families with reportlab; returns the
    family names to use in CSS plus any degradation message.

    Falls back to the built-ins -- Helvetica for body, Courier for code, both
    Latin-only, degrading legibly -- when the Windows fonts are absent.
    """
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

    mono_reason = _register_family("Mono", fonts_dir, _MONO_FILES)
    if mono_reason is not None:
        logger.info("no Cyrillic-capable monospace font (%s); using Courier", mono_reason)
    mono = "Courier" if mono_reason else "Mono"

    body_reason = _register_family("Body", fonts_dir, _BODY_FILES)
    if body_reason is None:
        return _FontChoice("Body", mono)
    return _FontChoice(
        "Helvetica",
        mono,
        f"no Cyrillic-capable font for the body text ({body_reason}): the PDF degraded "
        "to a Latin-only font, so any Cyrillic text renders as empty boxes",
    )


class PdfRenderError(Exception):
    """The PDF backend reported errors; the caller decides whether to degrade."""


def render_pdf(
    md_text: str,
    out_path: Path,
    *,
    base_dir: Path,
    warnings: list[str] | None = None,
) -> Path:
    """Render ``md_text`` to ``out_path``; relative links resolve against ``base_dir``.

    Raises :class:`PdfRenderError` on failure; the caller (the export job)
    degrades -- the ``.md`` stays the deliverable and the failure lands in
    the job's warnings, never failing the job.

    ``warnings``, when given, collects non-fatal degradations of the render
    itself -- today the font fallback that leaves Cyrillic text unreadable
    (FR-3). Optional so callers that have nowhere to put them keep working.
    """
    import markdown  # noqa: PLC0415 - deliberate lazy import (NFR-1)
    from xhtml2pdf import default as pisa_default  # noqa: PLC0415 - lazy (NFR-1)
    from xhtml2pdf import pisa  # noqa: PLC0415 - deliberate lazy import (NFR-1)

    body_html = markdown.markdown(md_text, extensions=_MARKDOWN_EXTENSIONS)
    family, mono, font_warning = _register_fonts()
    if font_warning is not None and warnings is not None:
        warnings.append(font_warning)
    # The bridge the reportlab registration alone does not give us: xhtml2pdf
    # resolves a CSS ``font-family`` against its *own* font list (each
    # ``Context`` copies it from ``DEFAULT_FONT``) and never consults
    # reportlab's registry -- so an unbridged family silently falls back to
    # Helvetica, which has no Cyrillic glyphs. Bold/italic still come from
    # reportlab's ``addMapping``. Process-global but idempotent: every render
    # writes the same key to the same face.
    pisa_default.DEFAULT_FONT[family.lower()] = family
    pisa_default.DEFAULT_FONT[mono.lower()] = mono
    font_css = (
        f"body, h1, h2, h3, td, th, li, p {{ font-family: {family}; }}"
        f"code, pre {{ font-family: {mono}; }}"
    )
    html = (
        "<html><head><meta charset='utf-8'><style>"
        + font_css
        + _BASE_CSS
        + "</style></head><body>"
        + body_html
        + "</body></html>"
    )

    def link_callback(uri: str, rel: str) -> str:
        # Fonts and images arrive as file paths (absolute for fonts,
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
