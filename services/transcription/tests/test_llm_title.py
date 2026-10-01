"""Unit tests for the `suggest_title` job's pure halves.

`llm/title.py` turns whatever the model answered into something a meeting
folder can be named after, and `prompts.title_messages` is the request that
answer comes from. Pure string assertions -- no model, no filesystem.
"""

from __future__ import annotations

import pytest

from transcription.llm.prompts import title_messages
from transcription.llm.reasoning import split_reasoning
from transcription.llm.title import MAX_TITLE_CHARS, sanitize_title

# ------------------------------------------------------------ sanitize_title


def test_a_clean_title_passes_through_unchanged() -> None:
    assert sanitize_title("Quarterly budget review") == "Quarterly budget review"


def test_a_cyrillic_title_passes_through_unchanged() -> None:
    assert sanitize_title("Обсуждение бюджета на квартал") == "Обсуждение бюджета на квартал"


@pytest.mark.parametrize(
    "raw",
    [
        "Budget review - Q3 plans",
        "Budget review – Q3 plans",  # en dash
        "Budget review — Q3 plans",  # em dash
        "Budget review − Q3 plans",  # minus sign
        "Budget review‑Q3 plans",  # non-breaking hyphen
        "Budget review－Q3 plans",  # fullwidth hyphen
    ],
)
def test_no_hyphen_or_dash_survives(raw: str) -> None:
    # `-` separates the sections of a meeting name; a title carrying one
    # would be refused by the rename (or parsed as a name plus a type).
    assert sanitize_title(raw) == "Budget review Q3 plans"


def test_a_hyphenated_word_is_split_not_glued() -> None:
    assert sanitize_title("Follow-up on the roll-out") == "Follow up on the roll out"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Budget review"', "Budget review"),
        ("'Budget review'", "Budget review"),
        ("«Обзор бюджета»", "Обзор бюджета"),
        ("“Budget review”", "Budget review"),
        ("„Budget review“", "Budget review"),
        ("`Budget review`", "Budget review"),
        ("**Budget review**", "Budget review"),
        ("# Budget review", "Budget review"),
        ("> Budget review", "Budget review"),
        ('**"Budget review"**', "Budget review"),
    ],
)
def test_wrapping_quotes_and_markdown_are_stripped(raw: str, expected: str) -> None:
    assert sanitize_title(raw) == expected


def test_an_apostrophe_inside_the_title_is_kept() -> None:
    assert sanitize_title("Anna's onboarding plan") == "Anna's onboarding plan"


@pytest.mark.parametrize("char", list('<>:"/\\|?*'))
def test_characters_windows_refuses_in_a_file_name_are_removed(char: str) -> None:
    title = sanitize_title(f"Budget{char}review")
    assert char not in title
    assert title == "Budget review"


def test_control_characters_are_removed() -> None:
    assert sanitize_title("Budget\x00 review\x07\tplans\x1f") == "Budget review plans"


def test_zero_width_characters_are_removed() -> None:
    assert sanitize_title("﻿Budget​ review") == "Budget review"


def test_whitespace_is_collapsed_and_trimmed() -> None:
    assert sanitize_title("   Budget    review \t plans  ") == "Budget review plans"


@pytest.mark.parametrize(
    "raw",
    ["Budget review.", "Budget review...", "Budget review. ", "Budget review!", "Budget review…"],
)
def test_no_trailing_dot_space_or_punctuation(raw: str) -> None:
    assert sanitize_title(raw) == "Budget review"


def test_a_trailing_question_mark_goes_with_the_illegal_characters() -> None:
    assert sanitize_title("What do we ship next?") == "What do we ship next"


@pytest.mark.parametrize(
    "raw",
    ["Title: Budget review", "title:Budget review", "Название: Budget review"],
)
def test_a_leading_label_is_dropped(raw: str) -> None:
    assert sanitize_title(raw) == "Budget review"


def test_only_the_first_usable_line_is_taken() -> None:
    raw = "\n\n  \nBudget review\n\nThis title reflects the main topic of the meeting."
    assert sanitize_title(raw) == "Budget review"


def test_a_line_of_nothing_but_noise_is_skipped() -> None:
    assert sanitize_title('""\n---\nBudget review') == "Budget review"


def test_an_overlong_title_is_cut_on_a_word_boundary() -> None:
    raw = " ".join(["planning"] * 30)
    title = sanitize_title(raw)
    assert len(title) <= MAX_TITLE_CHARS
    assert set(title.split(" ")) == {"planning"}, "no word is cut in half"
    assert len(title) > MAX_TITLE_CHARS - len("planning") - 1, "cut at the last word that fits"


def test_a_title_of_exactly_the_cap_is_kept_whole() -> None:
    raw = "a" * 39 + " " + "b" * 40
    assert len(raw) == MAX_TITLE_CHARS
    assert sanitize_title(raw) == raw


def test_a_word_ending_exactly_at_the_cap_is_kept() -> None:
    raw = "a" * 39 + " " + "b" * 40 + " tail"
    assert sanitize_title(raw) == "a" * 39 + " " + "b" * 40


def test_an_overlong_single_word_is_hard_cut() -> None:
    title = sanitize_title("x" * 300)
    assert title == "x" * MAX_TITLE_CHARS


def test_an_overlong_title_never_ends_in_punctuation_after_the_cut() -> None:
    raw = "word, " * 40
    title = sanitize_title(raw)
    assert len(title) <= MAX_TITLE_CHARS
    assert title.endswith("word")


@pytest.mark.parametrize(
    "raw", ["", "   ", "\n\n", '""', "---", "?", "***", "- \n – \n.", "\x00\x01"]
)
def test_an_answer_with_nothing_usable_yields_an_empty_title(raw: str) -> None:
    assert sanitize_title(raw) == ""


def test_the_result_never_carries_a_separator_or_an_illegal_character() -> None:
    raw = '  **"Re: Q3/Q4 road-map — what\'s <next>? | part*2"**.  '
    title = sanitize_title(raw)
    assert title == "Re Q3 Q4 road map what's next part 2"
    assert not set(title) & set('-<>:"/\\|?*')


# --------------------------------------------- reasoning, then sanitization


def test_a_think_block_is_split_off_before_sanitizing() -> None:
    answer, reasoning = split_reasoning(
        "<think>The summary is about budgets - maybe 'Budget'?</think>\n\nBudget review"
    )
    assert reasoning is not None
    assert sanitize_title(answer) == "Budget review"


def test_the_lone_closer_shape_is_split_off_before_sanitizing() -> None:
    answer, _reasoning = split_reasoning(
        "Let me think about a title.\n1. It is about budgets.\n</think>\n\n«Обзор бюджета».\n"
    )
    assert sanitize_title(answer) == "Обзор бюджета"


def test_reasoning_with_no_answer_after_it_yields_an_empty_title() -> None:
    answer, _reasoning = split_reasoning("<think>Still thinking about it</think>")
    assert sanitize_title(answer) == ""


# ------------------------------------------------------------ title_messages


def test_the_title_prompt_carries_the_summary_and_the_naming_rules() -> None:
    messages = title_messages("## Overview\n\nWe reviewed the budget.")

    assert [message["role"] for message in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert "same language the summary is written in" in system
    assert "We reviewed the budget." in user
    for rule in ("3 to 7 words", "no date", "no project code", "no quotes", "no trailing"):
        assert rule in user
