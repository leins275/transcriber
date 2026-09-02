"""Tests for date understanding in search/chat retrieval (`search/dates.py`)."""

from __future__ import annotations

from datetime import date

from transcription.search.dates import extract_query_dates, normalize_date_param

ANCHOR = date(2026, 9, 2)


def test_extracts_the_vault_yymmdd_form() -> None:
    text = "Мне нужно короткое саммари по всем встречам за сегодня - 260902."
    assert extract_query_dates(text, today=ANCHOR) == {"2026-09-02"}


def test_sentence_punctuation_after_a_date_still_matches() -> None:
    # The anchor differs from the named date, so the word "сегодня" cannot
    # mask a failed digit match.
    assert extract_query_dates("саммари за 260825.", today=ANCHOR) == {"2026-08-25"}
    assert extract_query_dates("встречи 25.08.2026.", today=ANCHOR) == {"2026-08-25"}


def test_extracts_iso_and_dotted_forms() -> None:
    assert extract_query_dates("meetings on 2026-08-25", today=ANCHOR) == {"2026-08-25"}
    assert extract_query_dates("встречи за 25.08.2026", today=ANCHOR) == {"2026-08-25"}


def test_today_and_yesterday_words_resolve_against_the_anchor() -> None:
    assert extract_query_dates("что обсуждали сегодня?", today=ANCHOR) == {"2026-09-02"}
    assert extract_query_dates("what happened yesterday", today=ANCHOR) == {"2026-09-01"}
    assert extract_query_dates("вчера и сегодня", today=ANCHOR) == {"2026-09-01", "2026-09-02"}


def test_impossible_and_non_date_numbers_are_ignored() -> None:
    # 991402: month 14. 123456: month 34. A 6-digit id inside a longer
    # number, a price, a version -- none of them are dates.
    assert extract_query_dates("ticket 991402 build 123456 v1.260902.3", today=ANCHOR) == set()


def test_multiple_dates_all_extract() -> None:
    got = extract_query_dates("сравни 260901 и 260902", today=ANCHOR)
    assert got == {"2026-09-01", "2026-09-02"}


def test_dateless_text_extracts_nothing() -> None:
    assert extract_query_dates("what were the main decisions?", today=ANCHOR) == set()


def test_normalize_accepts_yymmdd_and_iso() -> None:
    assert normalize_date_param("260902") == "2026-09-02"
    assert normalize_date_param("2026-09-02") == "2026-09-02"
    assert normalize_date_param(" 260902 ") == "2026-09-02"


def test_normalize_degrades_bad_values_to_none() -> None:
    for bad in (None, "", "  ", "991402", "2026-13-01", "02.09.2026", "tomorrow"):
        assert normalize_date_param(bad) is None
