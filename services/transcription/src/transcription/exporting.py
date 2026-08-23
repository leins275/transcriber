"""Per-recording export assembly (deterministic -- no LLM involved).

Builds one Markdown document from a meeting folder's existing materials, in
the fixed order the operator specified: Summary, then this recording's
action items, then its facts, then the full speaker-labelled transcript.
The PDF render of this document is the deliverable; the ``.md`` sits next
to it as the source.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from transcription.artifacts import (
    ACTION_ITEMS_DIR_NAME,
    FACTS_DIR_NAME,
    StoredItem,
    list_items,
)
from transcription.llm.prompts import render_transcript_lines

_SCREENSHOT_LINK = re.compile(r"\((screenshot-[^)]+\.png)\)")

# Caps mirroring the desktop app's defensive reads.
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_MAX_SIDECAR_BYTES = 1024 * 1024
_MAX_SUMMARY_BYTES = 4 * 1024 * 1024


def load_transcript(meeting_dir: Path) -> dict[str, Any] | None:
    """The meeting's ``transcript.json`` as a dict, or ``None`` when absent/bad."""
    path = meeting_dir / "transcript.json"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_speaker_overrides(meeting_dir: Path) -> dict[str, str]:
    """The ``speakers.json`` sidecar's manual assignments (segment id -> name)."""
    path = meeting_dir / "speakers.json"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SIDECAR_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assignments = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(assignments, dict):
        return {}
    return {str(key): str(value) for key, value in assignments.items()}


def load_summary(meeting_dir: Path) -> str | None:
    path = meeting_dir / "summary.md"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SUMMARY_BYTES:
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def items_for_meeting(project_dir: Path, kind_dir_name: str, meeting_name: str) -> list[StoredItem]:
    """The project-level items whose front matter cites this meeting."""
    return [
        item
        for item in list_items(project_dir / kind_dir_name)
        if str(item.meta.get("source_meeting", "")) == meeting_name
    ]


def _relocate_screenshot_links(body: str, item_dir: Path, export_dir: Path) -> str:
    """Rewrite an item body's relative ``screenshot-*.png`` links so they
    resolve from the export document's directory."""

    def replace(match: re.Match[str]) -> str:
        rel = os.path.relpath(item_dir / match.group(1), export_dir)
        return f"({rel.replace(os.sep, '/')})"

    return _SCREENSHOT_LINK.sub(replace, body)


def _item_section(item: StoredItem, export_dir: Path) -> str:
    title = str(item.meta.get("title") or item.dir.name)
    item_type = item.meta.get("type") or item.meta.get("kind")
    heading = f"### {title}" + (f" (`{item_type}`)" if item_type else "")
    body = _relocate_screenshot_links(item.body, item.dir, export_dir)
    # The stored body opens with its own `# title` heading; drop it so the
    # export document keeps one coherent outline under our `###` heading.
    body = re.sub(r"\A# [^\n]*\n+", "", body).strip()
    return f"{heading}\n\n{body}\n" if body else f"{heading}\n"


def build_export_md(
    *,
    meeting_dir: Path,
    meeting_name: str,
    project_dir: Path | None,
    export_dir: Path,
) -> tuple[str, list[str]]:
    """Assemble the export document; returns ``(markdown, warnings)``."""
    warnings: list[str] = []
    sections: list[str] = [f"# {meeting_name}", ""]

    summary = load_summary(meeting_dir)
    sections.append("## Summary")
    sections.append("")
    if summary:
        sections.append(summary)
    else:
        warnings.append("no summary.md; the export's summary section is empty")
        sections.append("_No summary has been generated for this recording yet._")
    sections.append("")

    for heading, kind_dir_name in (
        ("Action items", ACTION_ITEMS_DIR_NAME),
        ("Facts", FACTS_DIR_NAME),
    ):
        sections.append(f"## {heading}")
        sections.append("")
        items = (
            items_for_meeting(project_dir, kind_dir_name, meeting_name)
            if project_dir is not None
            else []
        )
        if items:
            for item in items:
                sections.append(_item_section(item, export_dir))
        else:
            sections.append(f"_No {heading.lower()} recorded for this recording._")
            sections.append("")

    sections.append("## Transcript")
    sections.append("")
    transcript = load_transcript(meeting_dir)
    if transcript is not None:
        segments = transcript.get("segments")
        lines = render_transcript_lines(
            segments if isinstance(segments, list) else [],
            load_speaker_overrides(meeting_dir),
        )
        sections.append("\n\n".join(lines) if lines else "_The transcript is empty._")
    else:
        warnings.append("transcript.json is missing or unreadable")
        sections.append("_The transcript could not be read._")
    sections.append("")

    return "\n".join(sections), warnings
