"""Prompt builders for the LLM job types.

Pure string assembly: no filesystem or network access, no imports from the
rest of the package (the ``diarization.py``/``filters.py`` contract for
logic modules). Transcripts are rendered as ``[m:ss] Speaker: text`` lines
so the model can cite timestamps. The per-meeting builders take the target
``language`` -- threaded in by ``jobs.py`` from ``transcript.json`` -- and
pin the answer to it explicitly (these are the operator's meetings; a
Russian meeting gets a Russian summary). Anything outside the supported set
falls back to the soft rule asking the model to mirror the transcript.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

Message = dict[str, str]

_TERMS_RULE = "Keep technical terms, product names and code identifiers as they appear."

_LANGUAGE_RULE = (
    "Write your answer in the same language the transcript is written in. " + _TERMS_RULE
)

# The languages transcription is constrained to; anything else is unpinned.
_LANGUAGE_NAMES = {"ru": "Russian", "en": "English"}


def _language_rule(language: str | None) -> str:
    """The language clause of a system prompt.

    A supported code (case- and whitespace-insensitive) yields a hard
    directive naming the output language; ``None``, a non-string and any
    other code fall back to the soft mirror rule, so a legacy transcript
    without a usable ``language`` field behaves exactly as it always has.
    """
    if not isinstance(language, str):
        return _LANGUAGE_RULE
    name = _LANGUAGE_NAMES.get(language.strip().lower())
    if name is None:
        return _LANGUAGE_RULE
    return f"Write your entire answer in {name}. " + _TERMS_RULE


def format_timestamp(seconds: float) -> str:
    """``[m:ss]`` under an hour, ``[h:mm:ss]`` above it."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_transcript_lines(
    segments: list[dict[str, Any]],
    speaker_overrides: Mapping[str, str] | None = None,
) -> list[str]:
    """One ``[m:ss] Speaker: text`` line per segment.

    ``speaker_overrides`` maps segment ids (as strings -- the
    ``speakers.json`` sidecar's key shape) to operator-assigned names, which
    outrank the diarization label carried on the segment itself.
    """
    overrides = speaker_overrides or {}
    lines: list[str] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        stamp = format_timestamp(float(segment.get("start", 0.0)))
        speaker = overrides.get(str(segment.get("id", ""))) or segment.get("speaker")
        prefix = f"[{stamp}] {speaker}: " if speaker else f"[{stamp}] "
        lines.append(f"{prefix}{text}")
    return lines


def summary_messages(transcript_text: str, *, language: str | None = None) -> list[Message]:
    """Summarize a transcript that fits in one chunk."""
    return [
        {
            "role": "system",
            "content": (
                "You are a meticulous meeting analyst. You write concise, "
                "well-structured Markdown summaries of meeting transcripts. "
                + _language_rule(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Summarize this meeting transcript as Markdown. Structure: a short "
                "overview paragraph, then sections for key discussion points, "
                "decisions made, and open questions. Omit a section when the "
                "meeting had nothing for it. Do not invent content that is not "
                "in the transcript.\n\nTranscript:\n\n" + transcript_text
            ),
        },
    ]


def chunk_summary_messages(
    chunk_text: str, index: int, total: int, *, language: str | None = None
) -> list[Message]:
    """The map half of map-reduce: summarize one chunk of a long transcript."""
    return [
        {
            "role": "system",
            "content": (
                "You are a meticulous meeting analyst summarizing one part of a "
                "longer meeting transcript. " + _language_rule(language)
            ),
        },
        {
            "role": "user",
            "content": (
                f"This is part {index + 1} of {total} of a meeting transcript. "
                "Write a compact Markdown summary of this part only: key points, "
                "decisions, open questions. Do not speculate about the other parts."
                "\n\nTranscript part:\n\n" + chunk_text
            ),
        },
    ]


def merge_summaries_messages(
    partial_summaries: list[str], *, language: str | None = None
) -> list[Message]:
    """The reduce half of map-reduce: merge per-chunk summaries into one."""
    numbered = "\n\n".join(
        f"--- Part {i + 1} summary ---\n{summary}" for i, summary in enumerate(partial_summaries)
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a meticulous meeting analyst. You merge partial summaries "
                "of one meeting into a single coherent Markdown summary. "
                + _language_rule(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Merge these partial summaries of one meeting into a single "
                "Markdown summary. Structure: a short overview paragraph, then "
                "sections for key discussion points, decisions made, and open "
                "questions. Deduplicate overlapping points.\n\n" + numbered
            ),
        },
    ]


_ACTION_ITEM_RULES = (
    "An action item is concrete follow-up work someone should do. Classify each as: "
    "'requirement' (a stated product/system requirement), 'epic' (a large body of "
    "work spanning multiple tasks), 'task' (a concrete, bounded piece of work), or "
    "'spike' (a time-boxed investigation to answer a question). For each item give "
    "a short imperative title, a Markdown description with all relevant context "
    "from the discussion, and the timestamps (in seconds, from the [m:ss] markers) "
    "of the transcript moments where it was discussed."
)


def action_items_messages(chunk_text: str, *, language: str | None = None) -> list[Message]:
    return [
        {
            "role": "system",
            "content": (
                "You extract action items from meeting transcripts and answer in "
                "strict JSON matching the provided schema. "
                + _ACTION_ITEM_RULES
                + " "
                + _language_rule(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract every action item from this transcript part. If there are "
                "none, return an empty items list.\n\nTranscript:\n\n" + chunk_text
            ),
        },
    ]


_FACT_RULES = (
    "A fact is a concrete piece of information stated in the meeting that is worth "
    "remembering (a constraint, a metric, a date, how something works). An answered "
    "question is a question someone asked that got a substantive answer. For each, "
    "give a short declarative title, a Markdown description (for answered questions: "
    "the question and its answer), and the timestamps (in seconds, from the [m:ss] "
    "markers) of the transcript moments involved."
)


def facts_messages(chunk_text: str, *, language: str | None = None) -> list[Message]:
    return [
        {
            "role": "system",
            "content": (
                "You extract notable facts and answered questions from meeting "
                "transcripts and answer in strict JSON matching the provided schema. "
                + _FACT_RULES
                + " "
                + _language_rule(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract the notable facts and answered questions from this "
                "transcript part. If there are none, return an empty items list."
                "\n\nTranscript:\n\n" + chunk_text
            ),
        },
    ]


def repair_messages(original: list[Message], raw_output: str, error: str) -> list[Message]:
    """The one bounded retry after invalid structured output: show the model
    its own output and the validation error, and ask again."""
    return [
        *original,
        {"role": "assistant", "content": raw_output},
        {
            "role": "user",
            "content": (
                "Your previous answer was not valid against the required JSON "
                f"schema: {error}\nAnswer again with only valid JSON."
            ),
        },
    ]


def report_messages(materials_text: str, project: str) -> list[Message]:
    """The project-essence status report over all collected materials."""
    return [
        {
            "role": "system",
            "content": (
                "You are a project analyst. From meeting summaries, action items "
                "and recorded facts you write a single project status report in "
                "Markdown. " + _LANGUAGE_RULE
            ),
        },
        {
            "role": "user",
            "content": (
                f"Write a status report for project {project} based on the "
                "materials below. Structure: a project overview, current status, "
                "key decisions, open questions and risks, and a table of action "
                "items grouped by type (requirement / epic / task / spike). Base "
                "everything strictly on the materials; do not invent progress."
                "\n\nMaterials:\n\n" + materials_text
            ),
        },
    ]
