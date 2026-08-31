"""Tests for the pure fusion logic (`search/hybrid.py`)."""

from __future__ import annotations

from transcription.search.hybrid import RRF_K, build_fts_match, make_snippet, rrf_fuse


def test_rrf_scores_follow_the_weighted_reciprocal_rank_formula() -> None:
    fused = rrf_fuse({"bm25": [7, 8]}, weights={"bm25": 1.5}, k=RRF_K)

    assert fused == [
        (7, 1.5 / (RRF_K + 1)),
        (8, 1.5 / (RRF_K + 2)),
    ]


def test_a_doc_ranked_by_two_channels_beats_a_doc_ranked_by_one() -> None:
    fused = rrf_fuse({"vector": [1, 2], "bm25": [2, 3]})

    order = [doc_id for doc_id, _score in fused]
    assert order[0] == 2  # first in bm25 AND second in vector


def test_an_exact_title_hit_outranks_a_body_hit() -> None:
    fused = rrf_fuse({"bm25": [1], "exact_title": [9]})

    assert [doc_id for doc_id, _ in fused] == [9, 1]


def test_trigram_alone_barely_registers() -> None:
    # The near-suppressed weight: a trigram-only hit loses to any real
    # relevance signal, however low it ranks there.
    fused = rrf_fuse({"bm25": [1, 2, 3, 4, 5, 6, 7, 8], "trigram": [9]})

    assert [doc_id for doc_id, _ in fused][-1] == 9


def test_an_absent_channel_needs_no_renormalization() -> None:
    with_vector = rrf_fuse({"bm25": [1], "vector": []})
    without_vector = rrf_fuse({"bm25": [1]})

    assert with_vector == without_vector


def test_fts_match_quotes_terms_and_joins_with_or() -> None:
    assert build_fts_match("дедлайн по проекту") == '"дедлайн" OR "по" OR "проекту"'


def test_fts_match_neutralizes_fts5_operators_and_quotes() -> None:
    match = build_fts_match('NEAR("x") AND *')

    # Every term is quoted; embedded quotes are doubled; nothing can parse
    # as an FTS5 operator.
    assert match == '"NEAR(""x"")" OR "AND" OR "*"'


def test_fts_match_of_only_quotes_is_empty() -> None:
    assert build_fts_match('""') == ""


def test_snippet_drops_the_breadcrumb_and_centers_on_the_hit() -> None:
    text = "[ACME / 260831 - Sync / 0:00–0:10]\n" + ("filler " * 50) + "дедлайн" + (" filler" * 50)

    snippet = make_snippet(text, "дедлайн", max_chars=120)

    assert "дедлайн" in snippet
    assert "ACME" not in snippet
    assert len(snippet) <= 122  # max_chars + ellipses


def test_snippet_falls_back_to_the_head_for_a_vector_only_hit() -> None:
    text = "[crumb]\nThe quick brown fox jumps over the lazy dog."

    assert make_snippet(text, "unrelated").startswith("The quick brown fox")
