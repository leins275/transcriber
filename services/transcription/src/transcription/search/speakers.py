"""Speaker understanding for search and chat retrieval (pure logic).

Every indexed transcript chunk carries the casefolded names of the people
who speak in it (``chunk_speakers``). These helpers turn what an operator
*types* into those keys, so "что говорил Иван про дедлайн" retrieves only
the chunks Иван took part in.

Two entry points, mirroring ``dates.py``:

* :func:`normalize_speaker_param` -- one explicit ``speaker`` argument
  (``/v1/search``, the MCP tool) folded into an index key;
* :func:`extract_query_speakers` -- the speakers a free-text question
  names, matched against the index's known keys (the chat's automatic
  scope).

The matching rule is deliberately conservative, because the filter is a
hard scope: a name matches as whole words, either in full, or by its
first-name token carrying a Russian case ending of up to two letters (a
name's own final ``а``/``я`` counts as part of that ending, so "Ольга"
matches "Ольге" and "Иван" matches "Иваном", while "Марк" does not match
"марте" and "Иван" does not match "Иванович"). No stemming library, no
transliteration, no nicknames. Generic diarization labels ("Speaker 1",
"SPEAKER_02") are never matched -- a vault that was diarized but never
named would otherwise scope every question mentioning "speakers".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# What diarization calls a voice it has no name for. Such a key is a
# placeholder, not a person, so it never takes part in auto-matching.
GENERIC_LABEL = re.compile(r"^speaker[ _]?\d+$")

# A name token: letters only (Cyrillic and Latin alike), no digits, no
# underscores -- ``\w`` would swallow "speaker_02" and "s2".
_LETTER = r"[^\W\d_]"
_LETTERS_ONLY = re.compile(rf"^{_LETTER}+$")

# Shortest first name that may be matched on its own: below that, an
# ending of up to two letters would match far too much.
_MIN_FIRST_NAME = 3

# A name of this length keeps a meaningful stem after its final `а`/`я`
# is treated as an ending.
_MIN_INFLECTED_NAME = 4

_FEMININE_ENDINGS = ("а", "я")


def normalize_speaker_param(value: str | None) -> str | None:
    """One explicit speaker argument (API/MCP) -> an index key, or ``None``.

    Empty and whitespace-only values mean "no filter" rather than "a
    speaker with no name", degrading to an unfiltered search.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    return raw.casefold()


def _first_name_pattern(key: str) -> re.Pattern[str] | None:
    """Whole-word first-name match with a short case ending, if allowed."""
    first = key.split()[0]
    if not _LETTERS_ONLY.match(first) or len(first) < _MIN_FIRST_NAME:
        return None
    stem = first
    if len(first) >= _MIN_INFLECTED_NAME and first.endswith(_FEMININE_ENDINGS):
        stem = first[:-1]
    return re.compile(rf"(?<!\w){re.escape(stem)}{_LETTER}{{0,2}}(?!\w)")


def _full_name_pattern(key: str) -> re.Pattern[str]:
    """Whole-word match of the whole name, whitespace kept flexible."""
    parts = [re.escape(part) for part in key.split()]
    return re.compile(rf"(?<!\w){r'\s+'.join(parts)}(?!\w)")


def _mentions(text: str, key: str) -> bool:
    if _full_name_pattern(key).search(text):
        return True
    first_name = _first_name_pattern(key)
    return first_name is not None and first_name.search(text) is not None


def extract_query_speakers(text: str, known: Iterable[str]) -> set[str]:
    """Every known speaker a free-text question names, as index keys.

    ``known`` are the index's speaker keys in scope (see
    ``IndexDb.known_speakers``); the result is a subset of them,
    casefolded. Purely additive signal for retrieval filtering: an empty
    set means the question names nobody and retrieval stays unfiltered.
    """
    haystack = text.casefold()
    found: set[str] = set()
    for name in known:
        key = name.casefold()
        if not key or GENERIC_LABEL.match(key):
            continue
        if _mentions(haystack, key):
            found.add(key)
    return found
