"""Vault artifact writers for the LLM job types.

Owns the on-disk conventions for derived knowledge: ``summary.md`` (written
by the summarize job through :func:`write_text_atomic`) and the export pair
``export.md`` + ``<project> - <date> - <title>.pdf`` (see
:func:`export_pdf_filename`), written into the meeting folder itself.

Legacy trees are never read and never written here; they stay on disk
untouched for external tools: the project-level ``<PROJECT>/action items/``
and ``<PROJECT>/facts/`` trees from before the per-meeting move, and the
per-meeting ``<meeting>/facts/``, ``<meeting>/action items/`` and
``<meeting>/exports/`` trees from before those features were retired (the
summary carries the notable facts and the action items now; the directory
names stay reserved in the vault crate).

The directory *names* are a cross-language contract shared with the vault
crate (``crates/vault/src/paths.rs``); both sides pin the exact strings with
tests. All text writes are atomic (the ``transcript.write_atomic`` pattern).
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

# Reserved inside a meeting folder (mirrored in crates/vault/src/paths.rs;
# the exact strings are the cross-language contract). Legacy names nothing
# here writes or reads any more -- `action items` and `facts` are also still
# reserved over there for the same reason.
EXPORTS_DIR_NAME = "exports"

# Reserved artifact file names inside a meeting folder (mirrored from
# crates/vault/src/paths.rs). `note.md` is the operator's own note, written
# by the app's note editor -- the service only ever reads it (indexing).
TRANSCRIPT_FILE_NAME = "transcript.json"
SUMMARY_FILE_NAME = "summary.md"
NOTE_FILE_NAME = "note.md"
SPEAKERS_FILE_NAME = "speakers.json"

# The reserved vault-root directory for meetings with no project
# (mirrored from crates/vault/src/paths.rs `UNSORTED_DIR_NAME`).
UNSORTED_DIR_NAME = "unsorted"


# Illegal-in-Windows-filename characters, matching vault's `ILLEGAL_CHARS`.
_ILLEGAL = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def write_text_atomic(text: str, target: Path) -> Path:
    """Write ``text`` to ``target`` atomically (tmp file + ``os.replace``)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".artifact-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def source_date_from_meeting_name(name: str) -> str | None:
    """ISO ``YYYY-MM-DD`` from a meeting folder's leading ``YYMMDD``.

    The vault names meetings ``<YYMMDD> - <stem>`` (``crates/vault/src/paths.rs``)
    and treats those six characters verbatim, so the century is fixed at
    ``20``: ``990101`` is 2099, not 1999. (Deliberately not ``strptime("%y")``,
    whose 69-99 -> 19xx pivot would contradict the vault contract.)

    Returns ``None`` -- never raises -- when the prefix is missing, too short,
    not ASCII digits, or not a real calendar date.
    """
    digits = name[:6]
    if len(digits) != 6 or not digits.isascii() or not digits.isdigit():
        return None
    try:
        parsed = date(2000 + int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError:
        return None
    return parsed.isoformat()


_MAX_EXPORT_STEM_CHARS = 120


def _filename_part(part: str) -> str:
    """One human-facing filename component: Windows-illegal characters
    replaced, whitespace collapsed -- case, spaces and non-Latin text kept
    (this names a file people share, not a machine slug)."""
    cleaned = _ILLEGAL.sub("-", part)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip("-. ")


def export_pdf_filename(meeting_dir: Path) -> str:
    """The export PDF's share-ready name: ``<project> - <date> - <title>.pdf``.

    Anchored on the meeting folder: the project is the meeting's parent
    folder (dropped for meetings under the reserved ``unsorted/`` root), the
    date is the ISO form of the folder's leading ``YYMMDD``, and the title
    is the rest of the folder name. Absent parts drop out of the name; if
    nothing usable remains the historical ``export.pdf`` is the fallback.
    """
    parent_name = meeting_dir.parent.name
    project = None if parent_name.casefold() == UNSORTED_DIR_NAME else parent_name
    iso_date = source_date_from_meeting_name(meeting_dir.name)
    title = meeting_dir.name
    if iso_date is not None:
        title = title[6:].lstrip(" -")
    cleaned = [c for c in (_filename_part(p) for p in (project, iso_date, title) if p) if c]
    stem = " - ".join(cleaned)[:_MAX_EXPORT_STEM_CHARS].rstrip("-. ")
    return f"{stem}.pdf" if stem else "export.pdf"
