"""Vault artifact writers and readers for the LLM job types.

Owns the on-disk conventions for derived knowledge:

- ``<PROJECT>/action items/<slug>/<slug>.md`` (+ ``screenshot-*.png``)
- ``<PROJECT>/facts/<slug>/<slug>.md``       (+ ``screenshot-*.png``)
- ``<meeting>/exports/<YYMMDD>/export.md``   (+ ``export.pdf``)

The directory *names* are a cross-language contract shared with the vault
crate (``crates/vault/src/paths.rs``); both sides pin the exact strings with
tests. All text writes are atomic (the ``transcript.write_atomic`` pattern);
item images are written before the markdown so a crash never leaves an
``.md`` referencing missing files.

Front matter is written as ``key: <json value>`` lines -- JSON is a YAML
subset, so the block reads as ordinary YAML front matter to humans and
external tools while staying trivially parseable here without a YAML
dependency.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcription.errors import ErrorKind, ServiceError

# The reserved project-level directory names (mirrored in crates/vault/src/paths.rs).
ACTION_ITEMS_DIR_NAME = "action items"
FACTS_DIR_NAME = "facts"
# Reserved inside a meeting folder (per-recording exports).
EXPORTS_DIR_NAME = "exports"

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


def list_items(kind_dir: Path) -> list[StoredItem]:
    """Read every item under a ``action items/``/``facts/`` directory.

    Best-effort like the vault crate's listing: a subdirectory without a
    readable ``.md`` is skipped, never an error. Sorted by folder name for
    a stable order.
    """
    if not kind_dir.is_dir():
        return []
    items: list[StoredItem] = []
    for child in sorted(kind_dir.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir():
            continue
        md_path = child / f"{child.name}.md"
        if not md_path.is_file():
            continue
        try:
            meta, body = parse_front_matter(md_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        screenshots = sorted(
            entry.name
            for entry in child.iterdir()
            if entry.is_file() and entry.suffix.lower() == ".png"
        )
        items.append(
            StoredItem(
                dir=child,
                md_path=md_path,
                meta=meta,
                body=body,
                screenshot_names=screenshots,
            )
        )
    return items
