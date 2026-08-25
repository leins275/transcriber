"""Vault artifact writers and readers for the LLM job types.

Owns the on-disk conventions for derived knowledge:

- ``<meeting>/action items/<slug>/<slug>.md`` (+ ``screenshot-*.png``)
- ``<meeting>/exports/<YYMMDD>/export.md``    (+ ``<project> - <date> - <title>.pdf``,
  see :func:`export_pdf_filename`)

Extracted items live *inside the recording's own folder*, alongside its
``transcript.json``, ``summary.md`` and ``exports/`` -- so they travel with
the recording when it is filed, renamed or synced, and unfiled (``unsorted/``)
recordings can be extracted too. Two legacy trees are never read and never
written here; they stay on disk untouched for external tools: the
project-level ``<PROJECT>/action items/`` and ``<PROJECT>/facts/`` trees
from before the per-meeting move, and the per-meeting ``<meeting>/facts/``
trees from before the facts job was retired (summaries carry the notable
facts now; the directory name stays reserved in the vault crate).

The directory *names* are a cross-language contract shared with the vault
crate (``crates/vault/src/paths.rs``); both sides pin the exact strings with
tests. All text writes are atomic (the ``transcript.write_atomic`` pattern);
item images are written before the markdown so a crash never leaves an
``.md`` referencing missing files.

Item paths are one level deeper than the project-level layout they replace,
so ``fit_slug`` trims the item slug against the 260-character Windows budget
using the meeting-level parent it is handed.

Front matter is written as ``key: <json value>`` lines -- JSON is a YAML
subset, so the block reads as ordinary YAML front matter to humans and
external tools while staying trivially parseable here without a YAML
dependency.

Front-matter field contract
---------------------------

The field set below is a cross-language contract, exactly like the directory
names above. **This docstring plus the pytest that pins the written key set
(``services/transcription/tests/test_llm_jobs.py``) is the source of truth**;
the vault crate's ``crates/vault/src/artifacts.rs`` mirrors it in docs. Any
code -- Python or Rust, now or later -- that reads or writes artifact front
matter must use these names verbatim.

Every key is written on every extraction item by ``jobs._extract_sync``:

===================  ==================  =====  =================================
key                  JSON type           null?  notes
===================  ==================  =====  =================================
``type``             string              no     the action-item type (legacy
                                                facts items carry ``kind``
                                                instead)
``title``            string              no
``archived``         boolean             no     always written ``false``; flipped
                                                only by external editors; an
                                                absent key reads as false
``source_project``   string              yes    the vault project folder holding
                                                the meeting; ``null`` when the
                                                meeting lives under
                                                ``unsorted/`` -- never the
                                                literal string ``"unsorted"``
``source_meeting``   string              no     the meeting folder's name
``source_recording`` string              yes    the stored ``source.<ext>``
                                                filename; ``null`` when absent
``source_date``      string YYYY-MM-DD   yes    from the meeting's leading
                                                ``YYMMDD`` (century fixed at
                                                20xx); ``null`` when unparseable
``timestamps``       number[]            no     transcript offsets, seconds
``created``          string (ISO, UTC)   no
``model``            string              no
``job_id``           string              no
``screenshots``      string              no     screenshot-capture status value
===================  ==================  =====  =================================

Two clauses of the contract are behaviour rather than fields:

- **Unknown keys survive.** Front matter hand-edited by an external property
  editor (Obsidian and friends) -- reordered keys, YAML-quoted strings, added
  keys, ``archived`` flipped to ``true`` -- round-trips into
  ``StoredItem.meta``; a value that is not valid JSON degrades to its raw
  string, and ``parse_front_matter`` never raises.
- **Nothing here rewrites an existing artifact ``.md``.** After its atomic
  creation an item file is read-only to this app; reading never touches bytes.
  A future mutation feature must round-trip unknown keys and the body
  byte-exactly outside the keys it changes.

The app never acts on ``archived``: exports and listings include archived
items exactly like unarchived ones. It exists for the operator's external
tools.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from transcription.errors import ErrorKind, ServiceError

# Reserved inside a meeting folder, alongside exports/ (mirrored in
# crates/vault/src/paths.rs; the exact strings are the cross-language
# contract). `facts` is also still reserved over there, but only as a legacy
# name nothing here writes or reads any more.
ACTION_ITEMS_DIR_NAME = "action items"
# Reserved inside a meeting folder (per-recording exports).
EXPORTS_DIR_NAME = "exports"

# The reserved vault-root directory for meetings with no project
# (mirrored from crates/vault/src/paths.rs `UNSORTED_DIR_NAME`).
UNSORTED_DIR_NAME = "unsorted"

# Windows path budget, matching the vault crate's `paths::check_len`.
MAX_PATH_LEN = 260


# Illegal-in-Windows-filename characters, matching vault's `ILLEGAL_CHARS`.
_ILLEGAL = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_DASH_RUNS = re.compile(r"-{2,}")

_MAX_SLUG_CHARS = 60

# The longest sibling filename that must also fit in the budget:
# "screenshot-hmmss.png" is 20 chars.
_LONGEST_SIBLING = 20


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


def slugify(title: str, *, fallback: str = "item") -> str:
    """A Windows-safe, human-readable folder slug from an item title.

    Lowercased, illegal/control characters and whitespace to ``-``, runs
    collapsed, trimmed of dots/spaces/dashes, capped at 60 characters (on a
    character boundary). Non-Latin text (Cyrillic titles) passes through --
    Windows folder names are Unicode; only the illegal set is replaced.
    """
    cleaned = _ILLEGAL.sub("-", title.strip().casefold())
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = _DASH_RUNS.sub("-", cleaned).strip("-. ")
    cleaned = cleaned[:_MAX_SLUG_CHARS].rstrip("-. ")
    return cleaned or fallback


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
    (unlike ``slugify``, this names a file people share, not a machine
    slug)."""
    cleaned = _ILLEGAL.sub("-", part)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip("-. ")


def export_pdf_filename(meeting_dir: Path) -> str:
    """The export PDF's share-ready name: ``<project> - <date> - <title>.pdf``.

    Anchored on the meeting folder exactly like ``jobs._extract_sync``'s
    provenance fields: the project is the meeting's parent folder (dropped
    for meetings under the reserved ``unsorted/`` root), the date is the
    ISO form of the folder's leading ``YYMMDD``, and the title is the rest
    of the folder name. Absent parts drop out of the name; if nothing
    usable remains the historical ``export.pdf`` is the fallback.
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


def fit_slug(parent: Path, slug: str) -> str:
    """Trim ``slug`` until ``parent/slug/<slug>.md`` (and the longest
    screenshot sibling) fits the 260-character Windows budget.

    Raises ``ServiceError(INVALID_REQUEST)`` when even a single-character
    slug cannot fit -- the vault root itself is too deep.
    """
    base_len = len(str(parent)) + 1  # parent + separator
    candidate = slug
    while candidate:
        # parent/slug/<slug>.md and parent/slug/screenshot-....png,
        # plus room for a " (n)" collision suffix on the directory.
        dir_len = base_len + len(candidate) + 4
        leaf = max(len(candidate) + len(".md"), _LONGEST_SIBLING)
        if dir_len + 1 + leaf <= MAX_PATH_LEN:
            return candidate
        candidate = candidate[:-1].rstrip("-. ")
    raise ServiceError(
        ErrorKind.INVALID_REQUEST,
        f"vault path is too deep to store artifacts under {parent.name!r}",
    )


def unique_item_dir(parent: Path, slug: str) -> Path:
    """Create and return ``parent/slug``, suffixing `` (n)`` on collision
    (the vault crate's ``suffixed`` convention)."""
    parent.mkdir(parents=True, exist_ok=True)
    fitted = fit_slug(parent, slug)
    candidate = parent / fitted
    n = 1
    while candidate.exists():
        n += 1
        candidate = parent / f"{fitted} ({n})"
    candidate.mkdir()
    return candidate


def render_front_matter(meta: dict[str, Any]) -> str:
    """``---`` block with one ``key: <json>`` line per entry (YAML-compatible)."""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a leading ``---`` front-matter block; returns ``(meta, body)``.

    Best-effort: a missing or malformed block yields ``({}, text)``; a
    malformed value line is skipped rather than failing the whole read.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text

    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        key, sep, raw_value = line.partition(":")
        if not sep or not key.strip():
            continue
        raw_value = raw_value.strip()
        try:
            meta[key.strip()] = json.loads(raw_value)
        except json.JSONDecodeError:
            meta[key.strip()] = raw_value
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def write_item(
    parent_dir: Path,
    *,
    title: str,
    meta: dict[str, Any],
    body_md: str,
    images: list[tuple[str, bytes]],
) -> Path:
    """Write one extraction item: ``parent/<slug>/<slug>.md`` + screenshots.

    Images land first, then the markdown atomically -- the ``.md`` is the
    commit point, so a crash never leaves an item referencing missing files
    (an imageless directory is inert junk, cleaned by the next successful
    write's collision suffixing being unaffected by it).
    """
    item_dir = unique_item_dir(parent_dir, slugify(title))
    for name, data in images:
        (item_dir / name).write_bytes(data)

    sections = [render_front_matter(meta), "", f"# {title}", ""]
    if body_md.strip():
        sections.extend([body_md.strip(), ""])
    if images:
        sections.append("\n".join(f"![{name}]({name})" for name, _ in images))
        sections.append("")
    md_path = item_dir / f"{item_dir.name}.md"
    write_text_atomic("\n".join(sections), md_path)
    return md_path


@dataclass(frozen=True)
class StoredItem:
    """One item read back from an artifact directory."""

    dir: Path
    md_path: Path
    meta: dict[str, Any]
    body: str
    screenshot_names: list[str]


def read_item(item_dir: Path) -> StoredItem | None:
    """Read one ``<kind>/<slug>/`` item directory; ``None`` when it holds no
    readable ``<slug>.md`` (best-effort, like the listing)."""
    if not item_dir.is_dir():
        return None
    md_path = item_dir / f"{item_dir.name}.md"
    if not md_path.is_file():
        return None
    try:
        meta, body = parse_front_matter(md_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    screenshots = sorted(
        entry.name
        for entry in item_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".png"
    )
    return StoredItem(
        dir=item_dir,
        md_path=md_path,
        meta=meta,
        body=body,
        screenshot_names=screenshots,
    )


def list_items(kind_dir: Path) -> list[StoredItem]:
    """Read every item under an ``action items/`` directory.

    Best-effort like the vault crate's listing: a subdirectory without a
    readable ``.md`` is skipped, never an error. Sorted by folder name for
    a stable order.
    """
    if not kind_dir.is_dir():
        return []
    items: list[StoredItem] = []
    for child in sorted(kind_dir.iterdir(), key=lambda p: p.name.casefold()):
        item = read_item(child)
        if item is not None:
            items.append(item)
    return items
