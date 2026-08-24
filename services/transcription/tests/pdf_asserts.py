"""pypdf-based inspection helpers for the rendered PDFs.

Imported flat (``from pdf_asserts import ...``), the convention the suite
already uses for ``tests/fakes.py``. These read a produced ``.pdf`` back the
way a reader application would: which fonts the document actually embeds,
and what text can be selected out of it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

# A subsetted font's ``/BaseFont`` carries a six-uppercase-letter tag, e.g.
# ``/BCDEEE+ArialMT``; the tag is arbitrary per render, the face name is not.
_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")


def embedded_base_fonts(pdf_path: Path) -> set[str]:
    """Every ``/BaseFont`` name reachable from the document's pages.

    Walks page ``/Resources`` -> ``/Font``, following ``/DescendantFonts``
    (composite fonts) and nested form-XObject resources, so a font used only
    inside a table cell or a header still shows up.
    """
    names: set[str] = set()
    reader = PdfReader(str(pdf_path))
    for page in reader.pages:
        _collect_resources(page.get("/Resources"), names, set())
    return names


def fonts_drawing_text(pdf_path: Path) -> set[str]:
    """The ``/BaseFont`` of every font that actually paints visible text.

    Narrower than :func:`embedded_base_fonts` on purpose: a page's ``/Font``
    resources routinely declare a font that some style block selected and the
    next operator replaced before any glyph was shown, so "which fonts are
    listed" cannot answer "which font is this paragraph set in".
    """
    names: set[str] = set()

    def visitor(text: str, _cm: Any, _tm: Any, font_dict: Any, _size: Any) -> None:
        if not text or not text.strip() or font_dict is None:
            return
        base_font = font_dict.get("/BaseFont")
        if base_font is not None:
            names.add(str(base_font).lstrip("/"))

    reader = PdfReader(str(pdf_path))
    for page in reader.pages:
        page.extract_text(visitor_text=visitor)
    return names


def strip_subset_tag(base_font: str) -> str:
    """``BCDEEE+ArialMT`` -> ``ArialMT``; other names pass through unchanged."""
    return _SUBSET_TAG.sub("", base_font.lstrip("/"))


def extract_text(pdf_path: Path) -> str:
    """All selectable text of the document, pages joined by newlines."""
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _collect_resources(resources: Any, names: set[str], seen: set[int]) -> None:
    if resources is None:
        return
    resolved = resources.get_object()
    if id(resolved) in seen:
        return
    seen.add(id(resolved))

    fonts = resolved.get("/Font")
    if fonts is not None:
        for font in fonts.get_object().values():
            _collect_font(font, names, seen)

    xobjects = resolved.get("/XObject")
    if xobjects is not None:
        for xobject in xobjects.get_object().values():
            _collect_resources(xobject.get_object().get("/Resources"), names, seen)


def _collect_font(font: Any, names: set[str], seen: set[int]) -> None:
    resolved = font.get_object()
    if id(resolved) in seen:
        return
    seen.add(id(resolved))

    base_font = resolved.get("/BaseFont")
    if base_font is not None:
        names.add(str(base_font).lstrip("/"))

    descendants = resolved.get("/DescendantFonts")
    if descendants is not None:
        for descendant in descendants.get_object():
            _collect_font(descendant, names, seen)

    # A Type3 font carries its own resource dictionary.
    _collect_resources(resolved.get("/Resources"), names, seen)
