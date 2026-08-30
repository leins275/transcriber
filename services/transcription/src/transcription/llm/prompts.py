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
                "overview paragraph, then sections for key discussion points "
                "(keep the notable facts -- constraints, metrics, dates, how "
                "things work), decisions made, action items, and open questions. "
                "Action items are concrete follow-up work someone agreed to do: "
                "one bullet each, an imperative phrase naming the owner when the "
                "transcript names one. Omit a section "
                "when the meeting had nothing for it. Do not invent content that "
                "is not in the transcript.\n\nTranscript:\n\n" + transcript_text
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
                "Write a compact Markdown summary of this part only: key points "
                "(keep the notable facts -- constraints, metrics, dates), "
                "decisions, action items (concrete follow-up work someone agreed "
                "to do, with the owner when named), open questions. Do not "
                "speculate about the other parts."
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
                "sections for key discussion points (keep the notable facts -- "
                "constraints, metrics, dates), decisions made, action items "
                "(concrete follow-up work, one bullet each, with the owner when "
                "named), and open questions. Omit a section when the meeting had "
                "nothing for it. Deduplicate overlapping points.\n\n" + numbered
            ),
        },
    ]
