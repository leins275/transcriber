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

from collections.abc import Callable, Mapping
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
                "things work), decisions made, and open questions. Omit a section "
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
                "sections for key discussion points (keep the notable facts -- "
                "constraints, metrics, dates), decisions made, and open "
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
    "of the few most important transcript moments where it was discussed -- not "
    "every mention. Separately, in screenshot_timestamps, list only the moments "
    "where the speakers are clearly referring to something visible on a shared "
    "screen -- a demo, a slide, a diagram, a document being walked through "
    "('as you can see here', 'on this slide'). Most items have no such moment: "
    "leave screenshot_timestamps empty unless the transcript makes the visual "
    "reference explicit."
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


def _truncate_to_budget(text: str, budget_tokens: int, count_tokens: Callable[[str], int]) -> str:
    """At most ``budget_tokens`` worth of ``text``, cut from the front."""
    tokens = count_tokens(text)
    if tokens <= budget_tokens:
        return text
    keep = max(1, len(text) * budget_tokens // tokens)
    while keep > 1 and count_tokens(text[:keep]) > budget_tokens:
        keep //= 2
    return text[:keep]


def repair_messages(
    original: list[Message],
    raw_output: str,
    error: str,
    *,
    output_budget_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[Message]:
    """The one bounded retry after invalid structured output: show the model
    its own output and the validation error, and ask again.

    Deliberately bounded by construction: only the system message survives
    from the original call (it carries the extraction rules and the language
    pin) and the echoed output is capped at ``output_budget_tokens`` --
    replaying the whole transcript plus an unbounded failed answer could
    overflow the context window, turning one bad answer into a hard error.
    The transcript itself is not needed: by the time repair runs the output
    was syntactically complete JSON that merely broke the schema, so the
    content to fix is all in the echo.
    """
    system = [message for message in original if message.get("role") == "system"][:1]
    echo = _truncate_to_budget(raw_output, output_budget_tokens, count_tokens)
    return [
        *system,
        {
            "role": "user",
            "content": (
                "Your previous answer was not valid against the required JSON "
                f"schema: {error}\n\nYour previous answer was:\n{echo}\n\n"
                "Answer again with only valid JSON matching the schema."
            ),
        },
    ]
