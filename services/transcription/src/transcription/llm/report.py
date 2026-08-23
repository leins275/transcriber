"""Project-essence report: material collection and LLM orchestration.

Collects everything a project folder holds -- per-meeting summaries (with a
transcript-text excerpt as fallback), action items and facts -- into one
materials text, then produces the status report through the injected
``complete`` callback (single call when it fits, map-reduce when it does
not, like ``summarize.py``).
"""

from __future__ import annotations

from pathlib import Path

from transcription.artifacts import (
    ACTION_ITEMS_DIR_NAME,
    FACTS_DIR_NAME,
    REPORTS_DIR_NAME,
    list_items,
)
from transcription.exporting import load_summary, load_transcript
from transcription.llm.chunking import chunk_lines, estimate_tokens
from transcription.llm.prompts import chunk_summary_messages, report_messages
from transcription.llm.summarize import CompleteFn

# The reserved project-level directories that are not meetings.
_RESERVED_DIR_NAMES = frozenset(
    name.casefold() for name in (ACTION_ITEMS_DIR_NAME, FACTS_DIR_NAME, REPORTS_DIR_NAME)
)

# How much raw transcript text stands in for a missing summary.
_TRANSCRIPT_EXCERPT_CHARS = 1500


def _meeting_dirs(project_dir: Path) -> list[Path]:
    if not project_dir.is_dir():
        return []
    return sorted(
        (
            child
            for child in project_dir.iterdir()
            if child.is_dir() and child.name.casefold() not in _RESERVED_DIR_NAMES
        ),
        key=lambda p: p.name.casefold(),
    )


def collect_project_materials(project_dir: Path) -> str:
    """One text document holding every material the report draws on."""
    sections: list[str] = []

    for meeting_dir in _meeting_dirs(project_dir):
        summary = load_summary(meeting_dir)
        if summary is not None:
            sections.append(f"## Meeting: {meeting_dir.name}\n\n{summary}")
            continue
        transcript = load_transcript(meeting_dir)
        if transcript is not None:
            text = str(transcript.get("text", "")).strip()
            if text:
                excerpt = text[:_TRANSCRIPT_EXCERPT_CHARS]
                suffix = "…" if len(text) > _TRANSCRIPT_EXCERPT_CHARS else ""
                sections.append(
                    f"## Meeting: {meeting_dir.name} (no summary; transcript excerpt)"
                    f"\n\n{excerpt}{suffix}"
                )

    for heading, dir_name, type_key in (
        ("Action items", ACTION_ITEMS_DIR_NAME, "type"),
        ("Facts and answered questions", FACTS_DIR_NAME, "kind"),
    ):
        items = list_items(project_dir / dir_name)
        if not items:
            continue
        lines = [f"## {heading}"]
        for item in items:
            title = str(item.meta.get("title") or item.dir.name)
            label = item.meta.get(type_key)
            source = item.meta.get("source_meeting")
            descriptor = f"- **{title}**"
            if label:
                descriptor += f" ({label})"
            if source:
                descriptor += f" — from {source}"
            lines.append(descriptor)
            body = item.body.strip()
            if body:
                # Drop the item's own `# title` heading; indent the rest as a detail block.
                body_lines = [ln for ln in body.splitlines() if not ln.startswith("# ")]
                lines.extend(f"  {ln}" for ln in body_lines if ln.strip())
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def report_from_materials(
    materials: str,
    project: str,
    complete: CompleteFn,
    *,
    budget_tokens: int,
) -> str:
    """The status report, map-reducing the materials when they overflow the budget."""
    if not materials.strip():
        raise ValueError("project has no materials to report on")

    if estimate_tokens(materials) <= budget_tokens:
        return complete(report_messages(materials, project)).strip()

    chunks = chunk_lines(materials.splitlines(), budget_tokens)
    partials = [
        complete(chunk_summary_messages(chunk, i, len(chunks))).strip()
        for i, chunk in enumerate(chunks)
    ]
    condensed = "\n\n".join(partials)
    return complete(report_messages(condensed, project)).strip()
