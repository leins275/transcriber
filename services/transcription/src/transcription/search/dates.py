"""Date understanding for search and chat retrieval (pure logic).

Every indexed document carries a ``meeting_date`` tag (ISO, derived from
the vault's ``YYMMDD - `` folder prefix). These helpers turn what an
operator *types* into that tag's form, so a question like "саммари по
встречам за сегодня - 260902" retrieves 260902's meetings and nothing
else.

Recognized spellings, all validated as real calendar dates:

* the vault's own ``YYMMDD`` (century fixed at 20, matching
  ``artifacts.source_date_from_meeting_name``);
* ISO ``YYYY-MM-DD``;
* the ``DD.MM.YYYY`` habit;
* the words ``сегодня``/``today`` and ``вчера``/``yesterday``, resolved
  against the local calendar.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# The lookarounds reject digit and dot-digit neighbours (version strings,
# longer numbers) while still matching before sentence punctuation:
# "за 260902." is a date, "v1.260902.3" is not.
_YYMMDD = re.compile(r"(?<!\d)(?<!\d\.)(\d{6})(?!\.?\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DOTTED = re.compile(r"(?<!\d)(?<!\d\.)(\d{2})\.(\d{2})\.(\d{4})(?!\.?\d)")

_TODAY_WORDS = re.compile(r"\b(?:сегодня|today)\b", re.IGNORECASE)
_YESTERDAY_WORDS = re.compile(r"\b(?:вчера|yesterday)\b", re.IGNORECASE)


def _valid(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_date_param(value: str | None) -> str | None:
    """One explicit date argument (API/MCP) -> ISO, or ``None`` for empty.

    Accepts the vault's ``YYMMDD`` and ISO ``YYYY-MM-DD``; anything else --
    including a syntactically shaped but impossible date -- is ``None``
    rather than an error, degrading to an unfiltered search.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{6}", raw):
        return _valid(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]))
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return _valid(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    return None


def extract_query_dates(text: str, *, today: date | None = None) -> set[str]:
    """Every date a free-text question names, as ISO strings.

    Purely additive signal for retrieval filtering: an empty set means the
    question names no date and retrieval stays unfiltered.
    """
    found: set[str] = set()
    for match in _YYMMDD.finditer(text):
        digits = match.group(1)
        iso = _valid(2000 + int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
        if iso:
            found.add(iso)
    for match in _ISO.finditer(text):
        iso = _valid(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso:
            found.add(iso)
    for match in _DOTTED.finditer(text):
        iso = _valid(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if iso:
            found.add(iso)

    anchor = today if today is not None else date.today()
    if _TODAY_WORDS.search(text):
        found.add(anchor.isoformat())
    if _YESTERDAY_WORDS.search(text):
        found.add((anchor - timedelta(days=1)).isoformat())
    return found
