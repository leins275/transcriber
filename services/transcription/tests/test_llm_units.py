"""Unit tests for the LLM feature's pure modules: registry laziness,
chunking, structured-output shapes, artifact writers, screenshot planning."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from transcription.artifacts import (
    ACTION_ITEMS_DIR_NAME,
    FACTS_DIR_NAME,
    MAX_PATH_LEN,
    UNSORTED_DIR_NAME,
    export_pdf_filename,
    fit_slug,
    list_items,
    parse_front_matter,
    render_front_matter,
    slugify,
    source_date_from_meeting_name,
    unique_item_dir,
    write_item,
)
from transcription.errors import ErrorKind, ServiceError
from transcription.frames import plan_screenshots, screenshot_name
from transcription.llm.chunking import (
    MIN_BUDGET_TOKENS,
    PROMPT_OVERHEAD_TOKENS,
    chunk_lines,
    estimate_tokens,
    input_budget_tokens,
    split_oversized,
)
from transcription.llm.extraction import merge_items, snap_timestamps
from transcription.llm.gguf_meta import (
    VRAM_RESERVE_BYTES,
    fit_gpu_layers,
    read_block_count,
)
from transcription.llm.prompts import format_timestamp, render_transcript_lines
from transcription.llm.shapes import (
    ActionItemsOut,
    FactsOut,
    LlmOutputError,
    parse_llm_json,
)

# ---------------------------------------------------------------- registry


def test_importing_the_llm_package_never_imports_an_llm_library() -> None:
    sys.modules.pop("llama_cpp", None)
    import importlib

    import transcription.llm

    importlib.reload(transcription.llm)
    assert "llama_cpp" not in sys.modules

    from transcription.llm import validate_llm_provider_name

    validate_llm_provider_name("llama_cpp")
    assert "llama_cpp" not in sys.modules

    with pytest.raises(ServiceError):
        validate_llm_provider_name("bogus")


def test_the_builtin_llama_cpp_engine_is_the_only_registered_engine() -> None:
    from transcription.llm import BUILTIN_ENGINE, known_llm_provider_names

    assert known_llm_provider_names() == {BUILTIN_ENGINE}
    assert BUILTIN_ENGINE == "llama_cpp"


def test_the_external_openai_compatible_engine_is_rejected() -> None:
    from transcription.llm import validate_llm_provider_name

    with pytest.raises(ServiceError) as excinfo:
        validate_llm_provider_name("openai_compat")

    assert excinfo.value.kind.value == "invalid_request"
    assert "openai_compat" in str(excinfo.value)
    assert "llama_cpp" in str(excinfo.value)


# ---------------------------------------------------------------- chunking


def test_chunk_lines_respects_the_budget_and_never_splits_a_fitting_line() -> None:
    lines = [f"line {i} " + "x" * 50 for i in range(40)]
    budget = 100  # tokens ~= 200 chars ~= 3 lines per chunk
    chunks = chunk_lines(lines, budget)

    assert len(chunks) > 1
    assert [line for chunk in chunks for line in chunk.splitlines()] == lines
    for chunk in chunks:
        assert estimate_tokens(chunk) <= budget + estimate_tokens(lines[0])


def test_the_fallback_estimate_is_conservative_for_cyrillic() -> None:
    # Qwen-family BPE splits Russian at ~2-3 chars/token; len // 2 stays at
    # or above the real count where the old len // 3 undershot it.
    assert estimate_tokens("а" * 300) == 150


def test_chunk_lines_uses_an_injected_token_counter() -> None:
    lines = ["abcdef"] * 6
    # Under the heuristic (len // 2 = 3 tokens/line) all six lines fit one
    # 20-token chunk; a counter that says every char is a token forces more.
    assert len(chunk_lines(lines, 20)) == 1
    assert len(chunk_lines(lines, 20, count_tokens=len)) > 1


def test_an_oversized_line_is_split_and_nothing_is_dropped() -> None:
    words = " ".join(f"word{i}" for i in range(200))
    chunks = chunk_lines([words], 20)
    assert len(chunks) > 1
    reassembled = " ".join(" ".join(chunk.splitlines()) for chunk in chunks)
    assert reassembled == words
    for chunk in chunks:
        assert estimate_tokens(chunk) <= 20 + estimate_tokens("word199")


def test_a_whitespace_free_monster_string_is_hard_sliced() -> None:
    huge = "y" * 10_000
    pieces = split_oversized(huge, 10)
    assert len(pieces) > 1
    assert "".join(pieces) == huge
    for piece in pieces:
        assert estimate_tokens(piece) <= 10


def test_chunk_lines_rejects_a_nonpositive_budget() -> None:
    with pytest.raises(ValueError):
        chunk_lines(["a"], 0)


def test_input_budget_subtracts_output_thinking_and_overhead() -> None:
    assert input_budget_tokens(16384, 4096, 2048) == 16384 - 4096 - 2048 - PROMPT_OVERHEAD_TOKENS
    # A pathologically small context still gets the floor, never zero.
    assert input_budget_tokens(2048, 4096, 2048) == MIN_BUDGET_TOKENS


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


# ------------------------------------------------------------- gpu offload


def _gguf_string(value: str) -> bytes:
    import struct

    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def _minimal_gguf(arch: str = "qwen3moe", block_count: int = 48) -> bytes:
    """A hand-built GGUF v3 header with just enough metadata."""
    import struct

    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", 3)
    # general.architecture = <arch> (string, type 8)
    kv1 = _gguf_string("general.architecture") + struct.pack("<I", 8) + _gguf_string(arch)
    # a skipped array value on the way (type 9, float32 elements)
    kv2 = (
        _gguf_string("tokenizer.scores")
        + struct.pack("<I", 9)
        + struct.pack("<I", 6)
        + struct.pack("<Q", 4)
        + struct.pack("<4f", 0.0, 1.0, 2.0, 3.0)
    )
    # <arch>.block_count = block_count (uint32, type 4)
    kv3 = (
        _gguf_string(f"{arch}.block_count") + struct.pack("<I", 4) + struct.pack("<I", block_count)
    )
    return header + kv1 + kv2 + kv3


def test_read_block_count_from_a_minimal_gguf_header(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(_minimal_gguf(block_count=48))
    assert read_block_count(model) == 48


def test_read_block_count_degrades_to_none_on_junk(tmp_path: Path) -> None:
    junk = tmp_path / "junk.gguf"
    junk.write_bytes(b"not a gguf file at all")
    assert read_block_count(junk) is None
    truncated = tmp_path / "trunc.gguf"
    truncated.write_bytes(_minimal_gguf()[:20])
    assert read_block_count(truncated) is None


def test_fit_gpu_layers_partial_full_and_none() -> None:
    gb = 1_000_000_000
    # A 20 GB / 48-layer model against ~11 GB free: a real partial offload,
    # never more than fits.
    partial = fit_gpu_layers(11 * gb, 20 * gb, 48)
    assert 0 < partial < 48
    assert partial * (20 * gb / 49) <= 11 * gb - VRAM_RESERVE_BYTES

    # A tiny model on a huge card: everything (-1, llama.cpp's "all").
    assert fit_gpu_layers(24 * gb, 2 * gb, 32) == -1

    # No usable VRAM after the reserve: stay on CPU.
    assert fit_gpu_layers(1 * gb, 20 * gb, 48) == 0
    assert fit_gpu_layers(0, 20 * gb, 48) == 0
    assert fit_gpu_layers(11 * gb, 20 * gb, 0) == 0


# --------------------------------------------------------------- reasoning


def test_split_reasoning_handles_the_lone_closer_shape() -> None:
    from transcription.llm.reasoning import split_reasoning

    # llama.cpp's Qwen template opens <think> in the prompt, so the
    # completion is "<thought></think><answer>".
    answer, reasoning = split_reasoning(
        "Here's a thinking process:\n1. Analyze.\n</think>\n\n# Summary\n\nThe answer."
    )
    assert answer == "# Summary\n\nThe answer."
    assert reasoning is not None and "thinking process" in reasoning


def test_split_reasoning_handles_paired_tags_and_plain_text() -> None:
    from transcription.llm.reasoning import split_reasoning

    answer, reasoning = split_reasoning("<think>hmm</think>The answer.")
    assert answer == "The answer."
    assert reasoning == "hmm"

    answer, reasoning = split_reasoning("Just an answer, no thinking.")
    assert answer == "Just an answer, no thinking."
    assert reasoning is None


# ------------------------------------------------------------ runtime fetch


def test_llama_cuda_pins_are_shaped_like_real_artifacts() -> None:
    from transcription.llm.runtime_fetch import LLAMA_CUDA_PACKAGES, llama_cuda_dir

    assert len(LLAMA_CUDA_PACKAGES) == 2
    for pkg in LLAMA_CUDA_PACKAGES:
        assert pkg.size > 0
        assert len(pkg.sha256) == 64
        assert pkg.url.startswith("https://")
    wheel = LLAMA_CUDA_PACKAGES[0]
    assert wheel.extract_prefix == "llama_cpp/"
    assert wheel.dest_subdir == "llama-cuda"
    assert llama_cuda_dir("C:/app").as_posix().endswith("runtime/llama-cuda")


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


def test_artifact_dir_names_pin_the_cross_language_contract() -> None:
    """The exact directory-name strings are shared with the vault crate
    (``crates/vault/src/paths.rs``). Their anchor moved to the meeting folder;
    the strings themselves must never drift on either side."""
    assert ACTION_ITEMS_DIR_NAME == "action items"
    assert FACTS_DIR_NAME == "facts"


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


def test_fit_slug_fits_a_realistically_deep_meeting_level_parent() -> None:
    """NFR-1: items now anchor one level deeper -- inside the meeting folder --
    so the budget is checked against a realistic synced vault path."""
    # ~170-character OneDrive-style sync root, as the operator's vault lives.
    synced_root = (
        Path("C:/Users/operator/OneDrive - Example Corporation")
        / "Documents"
        / "Meeting Recordings Vault"
        / "Shared with the Operations Team"
        / "2026 Recordings Archive"
        / "Synced from the Studio Laptop 2026"
    )
    assert len(str(synced_root)) == 174
    kind_dir = synced_root / "ELS" / "260101 - a long meeting title" / ACTION_ITEMS_DIR_NAME

    fitted = fit_slug(kind_dir, slugify("Chase the vendor about the signed statement of work"))
    assert 0 < len(fitted)
    # unique_item_dir may append a " (n)" collision suffix to the item folder,
    # and the longest screenshot sibling is "screenshot-hmmss.png" (20 chars).
    item_dir = kind_dir / f"{fitted} (2)"
    longest_sibling = screenshot_name(9 * 3600 + 59 * 60 + 59)
    assert len(longest_sibling) == 20
    assert len(str(item_dir / f"{fitted}.md")) <= MAX_PATH_LEN
    assert len(str(item_dir / longest_sibling)) <= MAX_PATH_LEN

    # A meeting folder too deep to hold even a one-character slug is refused.
    hopeless_meeting = synced_root / "ELS" / ("m" * 60) / ACTION_ITEMS_DIR_NAME
    with pytest.raises(ServiceError) as excinfo:
        fit_slug(hopeless_meeting, "slug")
    assert excinfo.value.kind is ErrorKind.INVALID_REQUEST


# ------------------------------------------- front-matter field contract


def test_unsorted_dir_name_mirrors_the_vault_crate() -> None:
    # crates/vault/src/paths.rs: `pub const UNSORTED_DIR_NAME: &str = "unsorted";`
    assert UNSORTED_DIR_NAME == "unsorted"


def test_export_pdf_filename_is_project_date_title() -> None:
    meeting = Path("vault") / "Project core" / "260824 - Weekly sync"
    assert export_pdf_filename(meeting) == "Project core - 2026-08-24 - Weekly sync.pdf"


def test_export_pdf_filename_drops_absent_parts() -> None:
    # Unsorted meetings have no project part.
    unfiled = Path("vault") / "unsorted" / "260824 - Weekly sync"
    assert export_pdf_filename(unfiled) == "2026-08-24 - Weekly sync.pdf"
    # No YYMMDD prefix: no date part, the whole folder name is the title.
    undated = Path("vault") / "ELS" / "Planning"
    assert export_pdf_filename(undated) == "ELS - Planning.pdf"


def test_export_pdf_filename_keeps_case_spaces_and_cyrillic() -> None:
    meeting = Path("vault") / "ЛМК" / "260824 - Обзор спринта"
    assert export_pdf_filename(meeting) == "ЛМК - 2026-08-24 - Обзор спринта.pdf"


def test_export_pdf_filename_replaces_windows_illegal_characters() -> None:
    meeting = Path("vault") / "ELS" / '260824 - a:b<c>d|e?f*g"h'
    name = export_pdf_filename(meeting)
    assert name.endswith(".pdf")
    assert not set('<>:"/\\|?*') & set(name)


def test_export_pdf_filename_falls_back_to_export_pdf() -> None:
    # Nothing usable survives cleaning: keep the historical fixed name.
    meeting = Path("vault") / "unsorted" / "..."
    assert export_pdf_filename(meeting) == "export.pdf"


def test_source_date_reads_the_meetings_leading_yymmdd_as_20xx() -> None:
    assert source_date_from_meeting_name("260824 - standup") == "2026-08-24"
    # The vault contract treats the six chars verbatim: no strptime("%y")
    # 69-99 -> 19xx pivot.
    assert source_date_from_meeting_name("990101 - x") == "2099-01-01"
    # The prefix is what counts; whatever follows it is not our business.
    assert source_date_from_meeting_name("260824standup") == "2026-08-24"


def test_source_date_is_none_when_the_prefix_is_not_a_calendar_date() -> None:
    assert source_date_from_meeting_name("Planning") is None
    assert source_date_from_meeting_name("2608 - x") is None
    assert source_date_from_meeting_name("") is None
    assert source_date_from_meeting_name("261345 - x") is None  # month 13
    assert source_date_from_meeting_name("260230 - x") is None  # Feb 30
    # `str.isdigit()` is True for non-ASCII digits, which `int()` would happily
    # accept; the vault's naming contract is ASCII.
    assert source_date_from_meeting_name("\u0662\u0666\u0660\u0668\u0662\u0664 - x") is None


def test_list_items_tolerates_obsidian_style_rewritten_front_matter(tmp_path: Path) -> None:
    # What an external property editor leaves behind: reordered keys, an
    # unknown key, YAML-quoted strings, `archived` flipped on.
    item_dir = tmp_path / "action items" / "fix-login"
    item_dir.mkdir(parents=True)
    (item_dir / "fix-login.md").write_text(
        '---\ntags: ["x"]\narchived: true\ntitle: "Quoted"\ntype: "task"\n'
        "source_project: null\n---\n\n# Quoted\n\nBroken on refresh.\n",
        encoding="utf-8",
    )

    (item,) = list_items(tmp_path / "action items")
    assert item.meta == {
        "tags": ["x"],
        "archived": True,
        "title": "Quoted",
        "type": "task",
        "source_project": None,
    }
    assert item.meta["archived"] is True  # JSON bool, not the string "true"
    # Body intact apart from the leading blank line and the trailing newline
    # that `splitlines()` normalises away.
    assert item.body == "# Quoted\n\nBroken on refresh."


def test_list_items_never_writes_to_the_files_it_reads(tmp_path: Path) -> None:
    md_path = write_item(
        tmp_path / "action items",
        title="Fix login",
        meta={"type": "task", "title": "Fix login", "archived": False},
        body_md="Broken on refresh.",
        images=[("screenshot-0010.png", b"\x89PNGfake")],
    )
    before = md_path.read_bytes()

    assert len(list_items(tmp_path / "action items")) == 1

    assert md_path.read_bytes() == before


def test_parse_front_matter_never_raises_on_edited_or_garbled_text() -> None:
    malformed = [
        "",
        "---",  # unterminated fence
        "---\n",
        "---\n---\n",
        "---\nkey:\n---\nbody",  # key with no value
        "---\narchived: yes\n---\n",  # YAML bool that is not JSON
        "---\n: novalue\n---\n",  # empty key
        "---\nno colon at all\n---\n",
        "---\ntags: [x]\n---\n",  # unquoted YAML flow scalar
        "---\n\x00\xff\x1b[31m\n---\nbody",
        "not front matter at all",
    ]
    for text in malformed:
        meta, body = parse_front_matter(text)
        assert isinstance(meta, dict)
        assert isinstance(body, str)

    # A non-JSON scalar degrades to the raw string; it never fails the read.
    assert parse_front_matter("---\narchived: yes\n---\n")[0]["archived"] == "yes"
    assert parse_front_matter("---\nkey:\n---\nbody")[0]["key"] == ""
    assert parse_front_matter("---\n: novalue\n---\n")[0] == {}


# -------------------------------------------------------- summarize reduce


def _scripted_complete(script: list[str]) -> tuple[Any, list[list[dict[str, str]]]]:
    """A summarize-callback recorder: returns scripted texts, records calls."""
    calls: list[list[dict[str, str]]] = []

    def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return script[len(calls) - 1] if len(calls) <= len(script) else script[-1]

    return complete, calls


def test_reduce_recurses_when_partials_exceed_the_budget() -> None:
    from transcription.llm.summarize import summarize_chunks

    # Six chunks; a budget that fits only two partials per merge group.
    chunks = [f"chunk {i}" for i in range(6)]
    partial_tokens = 50
    budget = 2 * (partial_tokens + 12) + 5

    def count(text: str) -> int:
        return partial_tokens

    script = [f"partial {i}" for i in range(6)] + ["merge"] * 10
    complete, calls = _scripted_complete(script)
    result = summarize_chunks(chunks, complete, reduce_budget_tokens=budget, count_tokens=count)

    assert result == "merge"
    merge_calls = [c for c in calls if "merge partial summaries" in c[0]["content"]]
    # 6 partials -> 3 merges -> (2 merges or direct) -> 1: more than one
    # round, and every merge prompt held at most 2 partials.
    assert len(merge_calls) >= 3
    for call in merge_calls:
        assert call[1]["content"].count("--- Part") <= 2


def test_single_round_reduce_is_unchanged_when_partials_fit() -> None:
    from transcription.llm.summarize import summarize_chunks

    chunks = ["chunk a", "chunk b"]
    complete, calls = _scripted_complete(["partial a", "partial b", "merged"])
    result = summarize_chunks(
        chunks, complete, reduce_budget_tokens=10_000, count_tokens=estimate_tokens
    )
    assert result == "merged"
    assert len(calls) == 3, "two map calls and exactly one reduce call"


def test_a_truncated_map_call_is_split_and_retried() -> None:
    from transcription.llm.base import LlmTruncatedError
    from transcription.llm.summarize import summarize_chunks

    calls: list[str] = []

    def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages[1]["content"])
        if len(calls) == 1:
            raise LlmTruncatedError("cut off")
        return f"result {len(calls)}"

    def split_chunk(chunk: str, depth: int) -> list[str]:
        half = len(chunk) // 2
        return [chunk[:half], chunk[half:]]

    result = summarize_chunks(
        ["one long chunk"],
        complete,
        reduce_budget_tokens=10_000,
        count_tokens=estimate_tokens,
        split_chunk=split_chunk,
    )
    # The single-chunk call truncated; its halves were map-summarized and
    # the two partials merged.
    assert result.startswith("result")
    assert len(calls) == 4  # 1 truncated + 2 map halves + 1 reduce


def test_truncation_without_a_splitter_propagates() -> None:
    from transcription.llm.base import LlmTruncatedError
    from transcription.llm.summarize import summarize_chunks

    def complete(messages: list[dict[str, str]]) -> str:
        raise LlmTruncatedError("cut off")

    with pytest.raises(LlmTruncatedError):
        summarize_chunks(["chunk"], complete)


# ----------------------------------------------------------- repair prompt


def test_repair_keeps_the_system_message_and_drops_the_transcript() -> None:
    from transcription.llm.prompts import action_items_messages, repair_messages

    original = action_items_messages("[0:00] A: the transcript body", language="ru")
    repair = repair_messages(
        original,
        '{"items": [{"bad": true}]}',
        "does not match the schema",
        output_budget_tokens=1000,
        count_tokens=estimate_tokens,
    )

    assert repair[0] == original[0], "the pinned system message survives verbatim"
    assert len(repair) == 2
    assert "the transcript body" not in repair[1]["content"]
    assert '{"items": [{"bad": true}]}' in repair[1]["content"]
    assert "does not match the schema" in repair[1]["content"]


def test_repair_truncates_a_huge_echoed_output_to_the_budget() -> None:
    from transcription.llm.prompts import facts_messages, repair_messages

    huge = "x" * 40_000
    repair = repair_messages(
        facts_messages("transcript"),
        huge,
        "err",
        output_budget_tokens=100,
        count_tokens=estimate_tokens,
    )
    assert len(repair[1]["content"]) < 1000


# ------------------------------------------------- llama.cpp streaming loop


def _stream_chunks(text_pieces: list[str], finish_reason: str) -> list[dict[str, Any]]:
    """Shaped like llama-cpp-python's streaming chat chunks, with the finish
    reason on a trailing empty-delta chunk."""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": piece}, "finish_reason": None}]} for piece in text_pieces
    ]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
    return chunks


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_streaming_complete_reports_the_finish_reason(finish_reason: str) -> None:
    import threading
    from types import SimpleNamespace

    from transcription.llm.llama_cpp_local import LlamaCppProvider
    from transcription.providers.base import CancelToken

    provider = LlamaCppProvider.__new__(LlamaCppProvider)
    provider._lock = threading.Lock()
    provider._llama = SimpleNamespace(
        create_chat_completion=lambda **kwargs: iter(_stream_chunks(["a", "b"], finish_reason))
    )
    provider._state = "loaded"

    completion = provider.complete(
        [{"role": "user", "content": "hi"}],
        json_schema=None,
        max_tokens=16,
        temperature=0.0,
        on_progress=lambda fraction: None,
        cancel=CancelToken(),
    )
    assert completion.text == "ab"
    assert completion.finish_reason == finish_reason


def test_count_tokens_falls_back_to_the_heuristic_when_loading_fails() -> None:
    from transcription.llm.llama_cpp_local import LlamaCppProvider

    provider = LlamaCppProvider.__new__(LlamaCppProvider)

    def boom() -> Any:
        raise RuntimeError("no model on this machine")

    provider._load = boom  # type: ignore[method-assign]
    assert provider.count_tokens("abcdefgh") == estimate_tokens("abcdefgh")


def test_written_front_matter_parses_identically_under_a_real_yaml_parser(
    tmp_path: Path,
) -> None:
    meta = {
        "type": "task",
        "title": 'A "quoted" title -- с кириллицей',
        "archived": False,
        "source_project": None,
        "source_meeting": "260824 - standup",
        "source_recording": "source.mp4",
        "source_date": "2026-08-24",
        "timestamps": [1.5, 2.0],
    }
    md_path = write_item(
        tmp_path / "action items",
        title="Fix login",
        meta=meta,
        body_md="Broken on refresh.",
        images=[],
    )
    text = md_path.read_text(encoding="utf-8")

    parsed_meta, _ = parse_front_matter(text)
    lines = text.splitlines()
    block = "\n".join(lines[1 : lines.index("---", 1)])

    assert yaml.safe_load(block) == parsed_meta == meta
