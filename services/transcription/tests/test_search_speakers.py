"""Tests for speaker understanding in search/chat retrieval (`search/speakers.py`)."""

from __future__ import annotations

import pytest

from transcription.search.speakers import extract_query_speakers, normalize_speaker_param

# The index's known speaker keys are always casefolded display names, and a
# vault that was diarized but never named keeps the generic labels.
KNOWN = frozenset({"иван петров", "ольга смирнова", "anna", "марк", "speaker 1", "speaker_02"})


def test_a_first_name_in_the_question_names_the_speaker_it_belongs_to() -> None:
    assert extract_query_speakers("что говорил Иван про дедлайн", KNOWN) == {"иван петров"}


def test_a_question_naming_two_people_scopes_to_both() -> None:
    got = extract_query_speakers("что решили у Ивана и Ольги", KNOWN)

    assert got == {"иван петров", "ольга смирнова"}


def test_a_first_name_carrying_a_case_ending_still_names_its_speaker() -> None:
    assert extract_query_speakers("это обсуждалось с Иваном", KNOWN) == {"иван петров"}


def test_a_patronymic_is_not_the_first_name_it_grows_from() -> None:
    assert extract_query_speakers("Иванович на встрече не был", KNOWN) == set()


def test_a_name_ending_in_a_matches_when_that_letter_is_replaced() -> None:
    assert extract_query_speakers("передай Ольге отчёт", KNOWN) == {"ольга смирнова"}


def test_an_everyday_word_sharing_a_stem_with_a_name_is_not_that_name() -> None:
    assert extract_query_speakers("встреча была в марте", KNOWN) == set()


def test_a_full_name_in_the_question_names_its_speaker() -> None:
    assert extract_query_speakers("Иван Петров сказал про сроки", KNOWN) == {"иван петров"}


def test_a_latin_name_is_recognized_whatever_its_case() -> None:
    assert extract_query_speakers("what did ANNA say about the release", KNOWN) == {"anna"}


def test_namesakes_are_both_named_by_a_bare_first_name() -> None:
    known = frozenset({"иван петров", "иван сидоров"})

    got = extract_query_speakers("что говорил Иван", known)

    assert got == {"иван петров", "иван сидоров"}


def test_generic_diarization_labels_are_never_matched() -> None:
    assert extract_query_speakers("which speakers were there", KNOWN) == set()


def test_a_question_naming_nobody_scopes_to_nobody() -> None:
    assert extract_query_speakers("какие были основные решения?", KNOWN) == set()


def test_an_unknown_person_in_the_question_names_no_speaker() -> None:
    assert extract_query_speakers("что сказал Пётр", KNOWN) == set()


def test_an_explicit_speaker_argument_becomes_its_index_key() -> None:
    assert normalize_speaker_param(" Иван Петров ") == "иван петров"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_speaker_argument_means_no_filter(empty: str | None) -> None:
    assert normalize_speaker_param(empty) is None
