"""Tests for the markdown -> PDF renderer.

These drive the real :func:`transcription.pdf.render_pdf` (no mocks): the bug
they guard against -- Cyrillic rendered as black boxes -- lives entirely in
which font the PDF backend ends up choosing, so only a real render and a real
read-back of the produced file can see it.

The Arial-dependent cases skip where ``%WINDIR%\\Fonts\\arial.ttf`` is absent
(FR-5: skip cleanly, never pass falsely); Windows CI always has it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pdf_asserts import embedded_base_fonts, extract_text, fonts_drawing_text, strip_subset_tag

from transcription.pdf import render_pdf

_FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

requires_arial = pytest.mark.skipif(
    not (_FONTS_DIR / "arial.ttf").is_file(),
    reason=r"needs the stock Arial family in %WINDIR%\Fonts",
)

requires_consolas = pytest.mark.skipif(
    not (_FONTS_DIR / "consola.ttf").is_file(),
    reason=r"needs the stock Consolas family in %WINDIR%\Fonts",
)

# One document touching every construct FR-1 names: heading, paragraph, list
# item, table cell and a transcript-style line.
CYRILLIC_MD = """\
# Привет, мир

Обычный абзац: русский текст вперемешку с English words.

- Пункт списка: Привет, мир

| Колонка | Значение |
| --- | --- |
| Итого | Привет, мир |

**00:00:12** Спикер 1: Привет, мир, это строка транскрипта.
"""

# Inline code and a fenced block, both carrying Cyrillic (FR-4).
CODE_MD = """\
Настройка задаётся ключом `путь_к_модели` в конфиге.

```
def приветствие(имя):
    return f"Привет, {имя}"
```
"""


def _normalized_text(pdf_path: Path) -> str:
    return " ".join(extract_text(pdf_path).split())


def _faces(pdf_path: Path) -> set[str]:
    return {strip_subset_tag(name) for name in embedded_base_fonts(pdf_path)}


@requires_arial
def test_cyrillic_body_text_is_rendered_in_an_embedded_arial_subset(tmp_path: Path) -> None:
    out_path = render_pdf(CYRILLIC_MD, tmp_path / "export.pdf", base_dir=tmp_path)

    base_fonts = embedded_base_fonts(out_path)
    assert any(name.endswith("+ArialMT") for name in base_fonts), (
        f"no embedded Arial subset in {sorted(base_fonts)}"
    )

    # Every construct in CYRILLIC_MD must be painted by the registered family,
    # not by base-14 Helvetica (which has no Cyrillic glyphs at all).
    painting = {strip_subset_tag(name) for name in fonts_drawing_text(out_path)}
    assert painting, "the render produced no visible text at all"
    assert not [name for name in painting if name.startswith("Helvetica")], (
        f"text runs fell back to base-14 Helvetica: {sorted(painting)}"
    )


@requires_arial
def test_cyrillic_text_extracts_back_intact(tmp_path: Path) -> None:
    out_path = render_pdf(CYRILLIC_MD, tmp_path / "export.pdf", base_dir=tmp_path)

    text = _normalized_text(out_path)
    for needle in (
        "Привет, мир",
        "Обычный абзац",
        "Пункт списка",
        "Колонка",
        "строка транскрипта",
    ):
        assert needle in text, f"{needle!r} missing from the extracted text: {text!r}"
    assert "\u25a0" not in text, f"replacement boxes in the extracted text: {text!r}"


@requires_arial
def test_bold_and_italic_use_the_matching_arial_variants(tmp_path: Path) -> None:
    markdown = "Обычный, **Жирный** и *курсив* в одном абзаце.\n"

    out_path = render_pdf(markdown, tmp_path / "emphasis.pdf", base_dir=tmp_path)

    base_fonts = embedded_base_fonts(out_path)
    assert any(name.endswith("+Arial-BoldMT") for name in base_fonts), (
        f"no embedded Arial bold subset in {sorted(base_fonts)}"
    )
    assert any(name.endswith("+Arial-ItalicMT") for name in base_fonts), (
        f"no embedded Arial italic subset in {sorted(base_fonts)}"
    )


@requires_consolas
def test_code_spans_and_blocks_use_an_embedded_consolas_subset(tmp_path: Path) -> None:
    # FR-4: Courier has no Cyrillic glyphs either, so code runs need the same
    # treatment as body text -- a real monospace face, embedded and subsetted.
    out_path = render_pdf(CODE_MD, tmp_path / "code.pdf", base_dir=tmp_path)

    base_fonts = embedded_base_fonts(out_path)
    assert any(name.endswith("+Consolas") for name in base_fonts), (
        f"no embedded Consolas subset in {sorted(base_fonts)}"
    )

    painting = {strip_subset_tag(name) for name in fonts_drawing_text(out_path)}
    assert "Consolas" in painting, f"no text run is set in Consolas: {sorted(painting)}"
    assert not [name for name in painting if name.startswith("Courier")], (
        f"code runs fell back to base-14 Courier: {sorted(painting)}"
    )


@requires_consolas
def test_cyrillic_inside_code_extracts_back_intact(tmp_path: Path) -> None:
    out_path = render_pdf(CODE_MD, tmp_path / "code.pdf", base_dir=tmp_path)

    text = _normalized_text(out_path)
    for needle in ("путь_к_модели", "приветствие", "Привет, {имя}"):
        assert needle in text, f"{needle!r} missing from the extracted text: {text!r}"
    assert "■" not in text, f"replacement boxes in the extracted text: {text!r}"


def test_a_fontless_machine_still_renders_code_via_the_builtin_monospace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FR-4's fallback half: no Consolas to register must degrade to Courier
    # (Latin-legible), never raise.
    fontless = tmp_path / "nowindows"
    (fontless / "Fonts").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(fontless))

    out_path = render_pdf(CODE_MD, tmp_path / "code.pdf", base_dir=tmp_path)

    assert out_path.read_bytes().startswith(b"%PDF")
    painting = {strip_subset_tag(name) for name in fonts_drawing_text(out_path)}
    assert any(name.startswith("Courier") for name in painting), (
        f"no code run fell back to the built-in monospace: {sorted(painting)}"
    )


@requires_arial
def test_rendering_twice_in_one_process_yields_the_same_fonts_and_text(tmp_path: Path) -> None:
    # The bridge mutates process-global state in xhtml2pdf; a second render
    # must neither fail nor drift.
    first = render_pdf(CYRILLIC_MD, tmp_path / "first.pdf", base_dir=tmp_path)
    second = render_pdf(CYRILLIC_MD, tmp_path / "second.pdf", base_dir=tmp_path)

    assert _faces(second) == _faces(first)
    assert _normalized_text(second) == _normalized_text(first)


def test_a_fontless_machine_still_renders_and_reports_the_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FR-3: no Cyrillic-capable font to register -> the render must still
    # succeed (legible Latin) AND the caller must learn about it, so the
    # operator is not handed a silently broken PDF.
    fontless = tmp_path / "nowindows"
    (fontless / "Fonts").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(fontless))
    warnings: list[str] = []

    out_path = render_pdf(
        CYRILLIC_MD, tmp_path / "export.pdf", base_dir=tmp_path, warnings=warnings
    )

    assert out_path.read_bytes().startswith(b"%PDF")
    assert len(warnings) == 1, f"expected exactly one degradation warning, got {warnings}"
    message = warnings[0].lower()
    assert "font" in message, warnings[0]
    assert "latin" in message, warnings[0]


@requires_arial
def test_no_warning_is_reported_when_the_font_family_registers(
    tmp_path: Path,
) -> None:
    warnings: list[str] = []

    render_pdf(CYRILLIC_MD, tmp_path / "export.pdf", base_dir=tmp_path, warnings=warnings)

    assert warnings == []


def test_importing_the_module_pulls_in_neither_reportlab_nor_xhtml2pdf() -> None:
    # NFR-2: the backends stay behind the lazy imports inside render_pdf.
    code = (
        "import sys, transcription.pdf; "
        "print(sorted(m for m in ('reportlab', 'xhtml2pdf') if m in sys.modules))"
    )

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, sys.executable
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
