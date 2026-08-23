"""Unit tests for the LLM feature's pure modules: registry laziness,
chunking, structured-output shapes, artifact writers, screenshot planning."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from transcription.artifacts import (
    fit_slug,
    list_items,
    parse_front_matter,
    render_front_matter,
    slugify,
    unique_item_dir,
    write_item,
)
from transcription.errors import ServiceError
from transcription.frames import plan_screenshots, screenshot_name
from transcription.llm.chunking import chunk_lines, estimate_tokens
from transcription.llm.extraction import merge_items, snap_timestamps
from transcription.llm.prompts import format_timestamp, render_transcript_lines
from transcription.llm.shapes import (
    ActionItemsOut,
    FactsOut,
    LlmOutputError,
    parse_llm_json,
)

# ---------------------------------------------------------------- registry


def test_importing_the_llm_package_never_imports_an_llm_library() -> None:
    for name in ("llama_cpp", "litellm"):
        sys.modules.pop(name, None)
    import importlib

    import transcription.llm

    importlib.reload(transcription.llm)
    assert "llama_cpp" not in sys.modules

    from transcription.llm import validate_llm_provider_name

    validate_llm_provider_name("llama_cpp")
    validate_llm_provider_name("openai_compat")
    assert "llama_cpp" not in sys.modules

    with pytest.raises(ServiceError):
        validate_llm_provider_name("bogus")


# ---------------------------------------------------------------- chunking


def test_chunk_lines_respects_the_budget_and_never_splits_a_line() -> None:
    lines = [f"line {i} " + "x" * 50 for i in range(40)]
    budget = 100  # tokens ~= 300 chars ~= 5 lines per chunk
    chunks = chunk_lines(lines, budget)

    assert len(chunks) > 1
    assert [line for chunk in chunks for line in chunk.splitlines()] == lines
    for chunk in chunks:
        assert estimate_tokens(chunk) <= budget + estimate_tokens(lines[0])


def test_a_single_oversized_line_still_becomes_a_chunk() -> None:
    huge = "y" * 10_000
    assert chunk_lines([huge], 10) == [huge]


def test_chunk_lines_rejects_a_nonpositive_budget() -> None:
    with pytest.raises(ValueError):
        chunk_lines(["a"], 0)


# ------------------------------------------------------------------ shapes


def test_parse_llm_json_tolerates_code_fences() -> None:
    fenced = '```json\n{"items": [{"type": "task", "title": "T"}]}\n```'
    parsed = parse_llm_json(fenced, ActionItemsOut)
    assert parsed.items[0].title == "T"


def test_parse_llm_json_raises_with_the_raw_output_attached() -> None:
    with pytest.raises(LlmOutputError) as excinfo:
        parse_llm_json("not json at all", FactsOut)
    assert excinfo.value.raw == "not json at all"

    with pytest.raises(LlmOutputError):
        parse_llm_json('{"items": [{"type": "wrong-type", "title": "T"}]}', ActionItemsOut)


def test_merge_items_dedupes_on_normalized_title_and_unions_timestamps() -> None:
    first = ActionItemsOut.model_validate(
        {"items": [{"type": "task", "title": "Fix  Login", "timestamps": [1.0]}]}
    ).items
    second = ActionItemsOut.model_validate(
        {"items": [{"type": "task", "title": "fix login", "timestamps": [2.0]}]}
    ).items
    merged = merge_items([first, second])
    assert len(merged) == 1
    assert merged[0].timestamps == [1.0, 2.0]


def test_snap_timestamps_clamps_and_snaps_to_segment_starts() -> None:
    snapped = snap_timestamps([11.0, 999.0, -5.0, 12.0], [0.0, 10.0, 20.0], 120.0)
    assert snapped == [10.0]


# ------------------------------------------------------------------ frames


def test_plan_screenshots_dedupes_within_the_gap_and_caps_the_count() -> None:
    stamps = [0.0, 1.0, 5.0, 5.5, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0]
    planned = plan_screenshots(stamps, 32.0)
    assert planned == [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]  # capped at 6, 1.0/5.5 collapsed


def test_screenshot_names_are_stable_and_sortable() -> None:
    assert screenshot_name(62.0) == "screenshot-0102.png"
    assert screenshot_name(3671.0) == "screenshot-10111.png"


# ---------------------------------------------------------------- prompts


def test_transcript_lines_carry_timestamps_and_speaker_overrides() -> None:
    segments = [
        {"id": 0, "start": 0.0, "text": "hello", "speaker": "Speaker 1"},
        {"id": 1, "start": 65.0, "text": "world"},
    ]
    lines = render_transcript_lines(segments, {"0": "Alice"})
    assert lines == ["[0:00] Alice: hello", "[1:05] world"]
    assert format_timestamp(3671) == "1:01:11"


# --------------------------------------------------------------- artifacts


def test_slugify_is_windows_safe_and_keeps_non_latin_text() -> None:
    assert slugify('Fix: the "login" <flow>?') == "fix-the-login-flow"
    assert slugify("Починить вход в систему") == "починить-вход-в-систему"
    assert slugify("???") == "item"
    assert len(slugify("long " * 100)) <= 60


def test_front_matter_round_trips_and_reads_as_yaml_style_text(tmp_path: Path) -> None:
    meta = {"type": "task", "title": 'A "quoted" title', "timestamps": [1.5, 2.0]}
    text = render_front_matter(meta) + "\n\nbody text"
    parsed_meta, body = parse_front_matter(text)
    assert parsed_meta == meta
    assert body == "body text"
    # No front matter at all: everything is body.
    assert parse_front_matter("plain") == ({}, "plain")


def test_write_item_then_list_items_round_trip(tmp_path: Path) -> None:
    md_path = write_item(
        tmp_path / "action items",
        title="Fix login",
        meta={"type": "task", "title": "Fix login"},
        body_md="Broken on refresh.",
        images=[("screenshot-0010.png", b"\x89PNGfake")],
    )
    assert md_path.name == "fix-login.md"

    items = list_items(tmp_path / "action items")
    assert len(items) == 1
    assert items[0].meta["type"] == "task"
    assert items[0].screenshot_names == ["screenshot-0010.png"]
    assert "Broken on refresh." in items[0].body
    assert "![screenshot-0010.png](screenshot-0010.png)" in items[0].body


def test_unique_item_dir_suffixes_collisions(tmp_path: Path) -> None:
    first = unique_item_dir(tmp_path, "slug")
    second = unique_item_dir(tmp_path, "slug")
    third = unique_item_dir(tmp_path, "slug")
    assert [first.name, second.name, third.name] == ["slug", "slug (2)", "slug (3)"]


def test_fit_slug_trims_against_the_260_char_budget() -> None:
    # fit_slug is pure path arithmetic -- no filesystem involved.
    deep = Path("C:/v") / ("a" * 180)
    fitted = fit_slug(deep, "s" * 60)
    assert 0 < len(fitted) < 60
    # The item's own md leaf must fit the budget alongside a screenshot.
    assert len(str(deep / fitted / f"{fitted}.md")) <= 260

    hopeless = Path("C:/v") / ("b" * 270)
    with pytest.raises(ServiceError):
        fit_slug(hopeless, "slug")
