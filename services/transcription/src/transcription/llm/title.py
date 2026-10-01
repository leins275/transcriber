"""Turning a model's answer into a usable meeting title (pure).

The ``suggest_title`` job asks the LLM for a short name for a meeting; this
module is what stands between that answer and a folder name. The model is
never trusted: whatever it wrote is reduced to one line that the vault's
``<YYMMDD> - <Name>[ - <Type>]`` convention can carry -- no ``-`` (the
section separator), nothing Windows refuses in a file name, no wrapping
quotes or Markdown, no trailing punctuation, and a bounded length.

No filesystem access and no imports from the rest of the package (the
``prompts.py``/``reasoning.py`` contract for logic modules).
"""

from __future__ import annotations

import re
import unicodedata

# Long enough for a descriptive title, short enough to leave the rest of a
# Windows path (vault root, project, date, type, artifact name) its room.
MAX_TITLE_CHARS = 80

# `-` separates the sections of a meeting name, so neither it nor any of its
# typographic relatives may survive into a title.
_DASHES = "-‐‑‒–—―−﹘﹣－"
# Characters Windows refuses in a file name.
_ILLEGAL = '<>:"/\\|?*'
_TO_SPACE = str.maketrans(dict.fromkeys(_DASHES + _ILLEGAL, " "))

# What a model wraps a title in: quotes of every shape, Markdown emphasis,
# heading and list markers, backticks.
_WRAPPERS = "\"'`*_#>«»“”„‟‘’‹› "
# Never left at the end of a title (the trailing dot and space are also the
# two characters Windows silently strips from a folder name).
_TRAILING = " .,;!…"

# A label some models put in front of the answer ("Title: ...").
_LABEL = re.compile(
    r"^(?:title|meeting title|название|заголовок|başlık)\s*[:：]\s*",
    re.IGNORECASE,
)


def sanitize_title(raw: str) -> str:
    """The first usable line of ``raw`` as a clean title, or ``""``.

    An empty result means the answer held nothing a meeting could be named
    after; the caller reports that as a failure rather than inventing one.
    """
    for line in raw.splitlines():
        title = _clean_line(line)
        if title:
            return title
    return ""


def _clean_line(line: str) -> str:
    # Control and format characters (tabs, zero-width marks, BOMs) become
    # spaces first, so they can neither survive nor glue two words together.
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in line)
    text = _LABEL.sub("", _trim(text))
    text = text.translate(_TO_SPACE)
    text = " ".join(text.split())
    return _cap(_trim(text))


def _trim(text: str) -> str:
    """Drop what wraps a title, and whatever punctuation trails it -- in
    either order (``"Title".`` as much as ``"Title."``)."""
    return text.lstrip(_WRAPPERS).rstrip(_WRAPPERS + _TRAILING)


def _cap(text: str) -> str:
    """``text`` cut to ``MAX_TITLE_CHARS`` on a word boundary when it has one."""
    if len(text) <= MAX_TITLE_CHARS:
        return text
    cut = text[: MAX_TITLE_CHARS + 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    else:
        cut = cut[:MAX_TITLE_CHARS]
    return _trim(cut)
