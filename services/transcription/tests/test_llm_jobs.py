"""Tests for the derived (LLM) job types inside the job manager.

Drives `JobManager` with `FakeLlm` + `FakeFrameExtractor` (tests/fakes.py):
no HTTP, no model, no llama.cpp, no network (FR-15). The vault tree is a
plain tempdir shaped like `<root>/<PROJECT>/<meeting>/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeFrameExtractor, FakeLlm
from pdf_asserts import embedded_base_fonts, extract_text

from transcription.artifacts import parse_front_matter, write_item
from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger

MEETING_NAME = "260101 - Planning"

RUSSIAN_DIRECTIVE = "Write your entire answer in Russian."
ENGLISH_DIRECTIVE = "Write your entire answer in English."
SOFT_RULE = "same language the transcript is written in"

_FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

requires_arial = pytest.mark.skipif(
    not (_FONTS_DIR / "arial.ttf").is_file(),
    reason=r"needs the stock Arial family in %WINDIR%\Fonts",
)

# FR-6: the exact front-matter key set `_extract_sync` writes. Drift in either
# direction (an added or a dropped key) breaks the documented cross-language
# contract in artifacts.py / crates/vault/src/artifacts.rs, so it fails here.
ACTION_ITEM_META_KEYS = {
    "type",
    "title",
    "archived",
    "source_project",
    "source_meeting",
    "source_recording",
    "source_date",
    "timestamps",
    "created",
    "model",
    "job_id",
    "screenshots",
}
FACT_META_KEYS = (ACTION_ITEM_META_KEYS - {"type"}) | {"kind"}


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


@pytest.fixture
def meeting_dir(tmp_app_dir: Path) -> Path:
    """A vault-shaped meeting folder with a transcript and a video source."""
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "source.mp4").write_bytes(b"fake-video-bytes")
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")
    return meeting


def _transcript_doc(segment_count: int = 3) -> dict[str, Any]:
    segments = [
        {
            "id": i,
            "start": float(i * 10),
            "end": float(i * 10 + 8),
            "text": f"segment {i} discussing the plan",
        }
        for i in range(segment_count)
    ]
    return {
        "schema_version": 1,
        "text": " ".join(seg["text"] for seg in segments),  # type: ignore[misc]
        "segments": segments,
        "source": {"path": "source.mp4", "filename": "source.mp4", "duration_sec": 120.0},
    }


def _meeting_with_language(
    tmp_app_dir: Path,
    language: Any,
    *,
    segment_count: int = 3,
    repeat_text: int = 1,
) -> Path:
    """A meeting whose `transcript.json` carries an explicit `language`.

    A sibling of the `meeting_dir` fixture rather than a change to
    `_transcript_doc()`: the fixture keeps the legacy, language-less shape
    every other test asserts against.
    """
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True, exist_ok=True)
    (meeting / "source.mp4").write_bytes(b"fake-video-bytes")
    doc = _transcript_doc(segment_count)
    if repeat_text > 1:
        for seg in doc["segments"]:
            seg["text"] = seg["text"] * repeat_text
    doc["language"] = language
    (meeting / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    return meeting


def _small_ctx(config: Config) -> Config:
    """The same config with a context small enough to force chunking."""
    return Config(
        app_dir=config.app_dir,
        config_path=config.config_path,
        provider="fake",
        allowed_roots=config.allowed_roots,
        db_path=config.db_path,
        token="test-token",  # noqa: S106 -- test fixture
        llm_ctx=2048,
    )


def _system_prompts(llm: FakeLlm) -> list[str]:
    return [call[0]["content"] for call in llm.calls]


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 60.0) -> None:
    # Generous: the first PDF render pays a multi-second xhtml2pdf/reportlab
    # import; every non-PDF job still finishes in milliseconds.
    deadline = time.monotonic() + timeout
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        await asyncio.sleep(0.01)


def _manager(
    config: Config,
    ledger: Ledger,
    llm: FakeLlm,
    extractor: FakeFrameExtractor | None = None,
) -> JobManager:
    frame_extractor = extractor if extractor is not None else FakeFrameExtractor()
    return JobManager(
        config,
        ledger,
        llm_factory=lambda _cfg: llm,
        frame_extractor_factory=lambda: frame_extractor,
    )


async def _run_job(
    manager: JobManager, *, job_type: str, input_path: Path, output_dir: Path
) -> str:
    await manager.start()
    job_id = await manager.submit(
        job_type=job_type, input_path=str(input_path), output_dir=str(output_dir)
    )
    await _wait_until_terminal(manager, job_id)
    return job_id


# ---------------------------------------------------------------- summarize


async def test_summarize_writes_summary_md_and_records_the_manifest(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(responses=["## Summary\n\nWe planned things."])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting_dir, output_dir=meeting_dir
        )

        job = manager.status(job_id)
        assert job.status == "succeeded"
        assert job.progress == 1.0

        summary = (meeting_dir / "summary.md").read_text(encoding="utf-8")
        assert "We planned things." in summary
        assert not list(meeting_dir.glob("*.tmp")), "no temp files left behind"

        row = ledger.get_job(job_id)
        assert row is not None
        assert row["job_type"] == "summarize"
        manifest = json.loads(row["result_json"])
        assert manifest["artifacts"] == [str(meeting_dir / "summary.md")]

        # The default `llm_keep_loaded=False` releases the model afterwards.
        assert llm.unload_calls == 1
        # Free-form output: no schema was requested.
        assert llm.schemas == [None]
    finally:
        await manager.aclose()


async def test_reasoning_is_stripped_from_the_summary_and_saved_to_a_sidecar(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(
        responses=["Here's a thinking process:\n1. Read it all.\n</think>\n\n## The real summary"]
    )
    manager = _manager(config, ledger, llm)
    try:
        await _run_job(
            manager, job_type="summarize", input_path=meeting_dir, output_dir=meeting_dir
        )

        summary = (meeting_dir / "summary.md").read_text(encoding="utf-8")
        assert summary.strip() == "## The real summary"
        assert "thinking process" not in summary

        reasoning = (meeting_dir / "summary.reasoning.md").read_text(encoding="utf-8")
        assert "thinking process" in reasoning
    finally:
        await manager.aclose()


async def test_extraction_tolerates_a_reasoning_prefix_before_the_json(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    payload = _items_json(
        [{"type": "task", "title": "One item", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=[f"Let me think about this.\n</think>\n{payload}"])
    manager = _manager(config, ledger, llm)
    items_dir = meeting_dir / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 1, "the reasoning prefix must not trigger a repair round"
    finally:
        await manager.aclose()


async def test_a_long_transcript_is_map_reduced(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    # ~200 segments x ~90 chars >> the (llm_ctx=2048)//2 = 1024-token budget.
    doc = _transcript_doc(segment_count=200)
    for seg in doc["segments"]:
        seg["text"] = seg["text"] * 3
    (meeting / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")

    small_ctx = Config(
        app_dir=config.app_dir,
        config_path=config.config_path,
        provider="fake",
        allowed_roots=config.allowed_roots,
        db_path=config.db_path,
        token="test-token",  # noqa: S106 -- test fixture
        llm_ctx=2048,
    )
    llm = FakeLlm(responses=["part summary", "part summary", "part summary", "merged summary"])
    manager = _manager(small_ctx, ledger, llm)
    try:
        await _run_job(manager, job_type="summarize", input_path=meeting, output_dir=meeting)

        assert len(llm.calls) >= 3, "expected several map calls plus one reduce"
        summary = (meeting / "summary.md").read_text(encoding="utf-8")
        assert summary.strip() == "merged summary"
    finally:
        await manager.aclose()


async def test_submit_rejects_a_meeting_without_a_transcript(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    manager = _manager(config, ledger, FakeLlm())
    try:
        from transcription.errors import ServiceError

        with pytest.raises(ServiceError) as excinfo:
            await manager.submit(
                job_type="summarize", input_path=str(meeting), output_dir=str(meeting)
            )
        assert excinfo.value.kind is ErrorKind.INVALID_REQUEST
        assert ledger.list_jobs() == [], "a rejected submit creates no ledger row"
    finally:
        await manager.aclose()


async def test_an_llm_load_failure_fails_the_job_and_the_worker_survives(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    failing = FakeLlm(raise_kind=ErrorKind.MODEL_LOAD)
    manager = _manager(config, ledger, failing)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting_dir, output_dir=meeting_dir
        )
        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.MODEL_LOAD
        assert not (meeting_dir / "summary.md").exists()

        # The worker keeps draining: a second (export) job still runs.
        export_dir = meeting_dir / "exports" / "260102"
        second = await manager.submit(
            job_type="export", input_path=str(meeting_dir), output_dir=str(export_dir)
        )
        await _wait_until_terminal(manager, second)
        assert manager.status(second).status == "succeeded"
    finally:
        await manager.aclose()


async def test_cancelling_a_queued_derived_job_never_runs_it(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm()
    manager = _manager(config, ledger, llm)
    try:
        # No worker started: the job stays queued.
        job_id = await manager.submit(
            job_type="summarize", input_path=str(meeting_dir), output_dir=str(meeting_dir)
        )
        await manager.cancel(job_id)

        job = manager.status(job_id)
        assert job.status == "cancelled"
        assert llm.calls == []
        row = ledger.get_job(job_id)
        assert row is not None and row["status"] == "cancelled"
    finally:
        await manager.aclose()


# ------------------------------------------------------- action items / facts


def _items_json(items: list[dict[str, Any]]) -> str:
    return json.dumps({"items": items})


async def test_action_items_are_written_with_screenshots_and_front_matter(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    response = _items_json(
        [
            {
                "type": "task",
                "title": "Fix the login flow",
                "description_md": "The login flow breaks on refresh.",
                "timestamps": [10.0, 20.0],
            },
            {
                "type": "spike",
                "title": "Investigate caching",
                "description_md": "Unclear which layer to cache in.",
                "timestamps": [20.0],
            },
        ]
    )
    llm = FakeLlm(responses=[response])
    extractor = FakeFrameExtractor()
    manager = _manager(config, ledger, llm, extractor)
    items_dir = meeting_dir / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )

        job = manager.status(job_id)
        assert job.status == "succeeded"
        assert job.warnings == []
        # Schema-constrained output was requested.
        assert llm.schemas[0] is not None

        task_dir = items_dir / "fix-the-login-flow"
        md_text = (task_dir / "fix-the-login-flow.md").read_text(encoding="utf-8")
        meta, body = parse_front_matter(md_text)
        assert meta["type"] == "task"
        assert meta["title"] == "Fix the login flow"
        assert meta["source_meeting"] == MEETING_NAME
        assert meta["source_project"] == "ELS"
        assert meta["source_recording"] == "source.mp4"
        assert meta["source_date"] == "2026-01-01"
        assert meta["screenshots"] == "succeeded"
        assert meta["timestamps"] == [10.0, 20.0]
        # FR-1: written explicitly as a JSON boolean, so property editors
        # surface a toggle instead of a string.
        assert "\narchived: false\n" in md_text
        assert meta["archived"] is False
        # FR-6: exact key set, no drift.
        assert set(meta) == ACTION_ITEM_META_KEYS
        assert "# Fix the login flow" in body
        assert "login flow breaks" in body

        screenshots = sorted(p.name for p in task_dir.glob("*.png"))
        assert screenshots == ["screenshot-0010.png", "screenshot-0020.png"]
        assert "![screenshot-0010.png](screenshot-0010.png)" in body

        assert (items_dir / "investigate-caching" / "investigate-caching.md").is_file()
        # Nothing is written to the legacy project-level tree (FR-1, FR-6).
        assert not (meeting_dir.parent / "action items").exists()

        row = ledger.get_job(job_id)
        assert row is not None
        manifest = json.loads(row["result_json"])
        assert manifest["item_count"] == 2
    finally:
        await manager.aclose()


async def test_extraction_repairs_invalid_json_once(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    good = _items_json(
        [{"type": "task", "title": "One item", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=["this is not json", good])
    manager = _manager(config, ledger, llm)
    items_dir = meeting_dir / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 2, "one original call plus one repair call"
        # The repair prompt carries the model's own bad output back to it.
        repair_messages = llm.calls[1]
        assert any("this is not json" in m["content"] for m in repair_messages)
    finally:
        await manager.aclose()


# -------------------------------------------------- extraction language pinning


async def test_facts_pin_russian_for_every_chunk_of_a_long_transcript(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, "ru", segment_count=200, repeat_text=3)
    response = _items_json(
        [{"kind": "fact", "title": "A fact", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=[response])
    manager = _manager(_small_ctx(config), ledger, llm)
    facts_dir = meeting / "facts"
    try:
        job_id = await _run_job(manager, job_type="facts", input_path=meeting, output_dir=facts_dir)
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        assert len(prompts) > 1, "expected a multi-chunk transcript"
        assert all(RUSSIAN_DIRECTIVE in prompt for prompt in prompts)
        assert not any(SOFT_RULE in prompt for prompt in prompts)
    finally:
        await manager.aclose()


async def test_action_items_pin_english_when_the_transcript_says_en(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, "en")
    response = _items_json(
        [{"type": "task", "title": "One item", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=[response])
    manager = _manager(config, ledger, llm)
    items_dir = meeting / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        assert prompts and all(ENGLISH_DIRECTIVE in prompt for prompt in prompts)
        assert not any(SOFT_RULE in prompt for prompt in prompts)
    finally:
        await manager.aclose()


async def test_a_transcript_without_a_language_key_keeps_the_soft_rule(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    response = _items_json(
        [{"kind": "fact", "title": "A fact", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=[response])
    manager = _manager(config, ledger, llm)
    facts_dir = meeting_dir / "facts"
    try:
        job_id = await _run_job(
            manager, job_type="facts", input_path=meeting_dir, output_dir=facts_dir
        )
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        assert prompts and all(SOFT_RULE in prompt for prompt in prompts)
        assert not any("Write your entire answer in" in prompt for prompt in prompts)
    finally:
        await manager.aclose()


@pytest.mark.parametrize("language", [None, "de"])
async def test_a_null_or_unsupported_language_keeps_the_soft_rule(
    config: Config, ledger: Ledger, tmp_app_dir: Path, language: Any
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, language)
    response = _items_json(
        [{"kind": "fact", "title": "A fact", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=[response])
    manager = _manager(config, ledger, llm)
    facts_dir = meeting / "facts"
    try:
        job_id = await _run_job(manager, job_type="facts", input_path=meeting, output_dir=facts_dir)
        assert manager.status(job_id).status == "succeeded", "the language field never fails a job"

        prompts = _system_prompts(llm)
        assert prompts and all(SOFT_RULE in prompt for prompt in prompts)
        assert not any("Write your entire answer in" in prompt for prompt in prompts)
    finally:
        await manager.aclose()


async def test_the_repair_call_replays_the_pinned_system_message(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, "ru")
    good = _items_json(
        [{"type": "task", "title": "One item", "description_md": "d", "timestamps": []}]
    )
    llm = FakeLlm(responses=["this is not json", good])
    manager = _manager(config, ledger, llm)
    items_dir = meeting / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 2, "one original call plus one repair call"

        original, repair = llm.calls
        assert repair[: len(original)] == original
        assert RUSSIAN_DIRECTIVE in repair[0]["content"]
    finally:
        await manager.aclose()


# --------------------------------------------------- summarize language pinning


async def test_a_single_chunk_summary_is_pinned_to_the_transcript_language(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, "ru")
    llm = FakeLlm(responses=["## Итоги"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        assert len(prompts) == 1, "a short transcript is one single-chunk call"
        assert RUSSIAN_DIRECTIVE in prompts[0]
        assert SOFT_RULE not in prompts[0]
    finally:
        await manager.aclose()


async def test_every_map_call_and_the_reduce_call_are_pinned(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = _meeting_with_language(tmp_app_dir, "ru", segment_count=200, repeat_text=3)
    llm = FakeLlm(responses=["сводка части", "сводка части", "сводка части", "общая сводка"])
    manager = _manager(_small_ctx(config), ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        map_prompts = [p for p in prompts if "summarizing one part" in p]
        reduce_prompts = [p for p in prompts if "merge partial summaries" in p]
        assert len(map_prompts) > 1, "expected several map calls"
        assert len(reduce_prompts) == 1, "expected exactly one reduce call"
        assert all(RUSSIAN_DIRECTIVE in prompt for prompt in prompts)
        assert not any(SOFT_RULE in prompt for prompt in prompts)
    finally:
        await manager.aclose()


async def test_a_summary_of_a_language_less_transcript_keeps_the_soft_rule(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(responses=["## Summary"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting_dir, output_dir=meeting_dir
        )
        assert manager.status(job_id).status == "succeeded"

        prompts = _system_prompts(llm)
        assert prompts and all(SOFT_RULE in prompt for prompt in prompts)
        assert not any("Write your entire answer in" in prompt for prompt in prompts)
    finally:
        await manager.aclose()


async def test_persistently_invalid_json_fails_with_llm_output(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(responses=["nope"])  # the single response repeats: repair also fails
    manager = _manager(config, ledger, llm)
    items_dir = meeting_dir / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )
        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.LLM_OUTPUT
        assert not any(items_dir.iterdir()) if items_dir.exists() else True
    finally:
        await manager.aclose()


async def test_screenshot_failure_degrades_but_items_are_still_written(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    response = _items_json(
        [{"type": "task", "title": "A task", "description_md": "d", "timestamps": [10.0]}]
    )
    llm = FakeLlm(responses=[response])
    extractor = FakeFrameExtractor(raise_kind=ErrorKind.AUDIO_DECODE)
    manager = _manager(config, ledger, llm, extractor)
    items_dir = meeting_dir / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )
        job = manager.status(job_id)
        assert job.status == "succeeded", "screenshots never fail the job"
        assert job.warnings and "screenshots failed" in job.warnings[0]

        md_text = (items_dir / "a-task" / "a-task.md").read_text(encoding="utf-8")
        meta, _ = parse_front_matter(md_text)
        assert meta["screenshots"] == "failed (audio_decode)"
        assert not list((items_dir / "a-task").glob("*.png"))
    finally:
        await manager.aclose()


async def test_facts_use_the_kind_key_and_audio_only_recordings_get_no_screenshots(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "source.mp3").write_bytes(b"fake-audio")
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")

    response = _items_json(
        [
            {
                "kind": "answered_question",
                "title": "Which database do we use?",
                "description_md": "Postgres, decided last sprint.",
                "timestamps": [10.0],
            }
        ]
    )
    llm = FakeLlm(responses=[response])
    extractor = FakeFrameExtractor(no_video=True)
    manager = _manager(config, ledger, llm, extractor)
    facts_dir = meeting / "facts"
    try:
        job_id = await _run_job(manager, job_type="facts", input_path=meeting, output_dir=facts_dir)
        assert manager.status(job_id).status == "succeeded"

        item_dirs = [p for p in facts_dir.iterdir() if p.is_dir()]
        assert len(item_dirs) == 1
        md_text = (item_dirs[0] / f"{item_dirs[0].name}.md").read_text(encoding="utf-8")
        meta, _ = parse_front_matter(md_text)
        assert meta["kind"] == "answered_question"
        assert meta["screenshots"] == "none"
        assert not list(item_dirs[0].glob("*.png"))
        # FR-1: facts share the writer, so they share the `archived` field.
        assert "\narchived: false\n" in md_text
        assert meta["archived"] is False
        # FR-6: the facts key set is the action-item set with `kind` for `type`.
        assert set(meta) == FACT_META_KEYS
    finally:
        await manager.aclose()


async def test_unsorted_meetings_get_a_null_source_project_and_no_recording(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    """FR-2: the reserved `unsorted/` root is not a project, and a meeting
    with no stored recording reports `source_recording: null`."""
    meeting = tmp_app_dir / "vault" / "unsorted" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")

    response = _items_json(
        [{"type": "task", "title": "A task", "description_md": "d", "timestamps": [10.0]}]
    )
    manager = _manager(config, ledger, FakeLlm(responses=[response]))
    items_dir = meeting / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"

        md_text = (items_dir / "a-task" / "a-task.md").read_text(encoding="utf-8")
        meta, _ = parse_front_matter(md_text)
        # JSON null -- never the literal string "unsorted" posing as a project.
        assert "\nsource_project: null\n" in md_text
        assert meta["source_project"] is None, "unsorted meetings carry a null project"
        assert meta["source_meeting"] == MEETING_NAME
        assert meta["source_recording"] is None
        # The rest of the contract still holds for an unfiled meeting: the
        # date comes off the folder name, and `archived` is always written.
        assert meta["source_date"] == "2026-01-01"
        assert meta["archived"] is False
        assert set(meta) == ACTION_ITEM_META_KEYS
    finally:
        await manager.aclose()


async def test_a_meeting_without_a_date_prefix_still_succeeds_with_a_null_source_date(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    """FR-3: an unparseable folder name degrades to null, never fails the job."""
    meeting = tmp_app_dir / "vault" / "ELS" / "Planning notes"
    meeting.mkdir(parents=True)
    (meeting / "source.mp3").write_bytes(b"fake-audio")
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")

    response = _items_json(
        [{"type": "task", "title": "A task", "description_md": "d", "timestamps": [10.0]}]
    )
    manager = _manager(config, ledger, FakeLlm(responses=[response]))
    items_dir = meeting / "action items"
    try:
        job_id = await _run_job(
            manager, job_type="action_items", input_path=meeting, output_dir=items_dir
        )
        assert manager.status(job_id).status == "succeeded"

        md_text = (items_dir / "a-task" / "a-task.md").read_text(encoding="utf-8")
        meta, _ = parse_front_matter(md_text)
        assert "\nsource_date: null\n" in md_text
        assert meta["source_date"] is None
        assert meta["source_project"] == "ELS"
    finally:
        await manager.aclose()


async def test_duplicate_titles_get_collision_suffixed_folders(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    response = _items_json(
        [{"type": "task", "title": "Same title", "description_md": "first", "timestamps": []}]
    )
    llm = FakeLlm(responses=[response])
    manager = _manager(config, ledger, llm)
    items_dir = meeting_dir / "action items"
    try:
        await _run_job(
            manager, job_type="action_items", input_path=meeting_dir, output_dir=items_dir
        )
        # Re-run: the same title must land in a new, suffixed folder.
        second = await manager.submit(
            job_type="action_items", input_path=str(meeting_dir), output_dir=str(items_dir)
        )
        await _wait_until_terminal(manager, second)

        names = sorted(p.name for p in items_dir.iterdir() if p.is_dir())
        assert names == ["same-title", "same-title (2)"]
        assert (items_dir / "same-title (2)" / "same-title (2).md").is_file()
    finally:
        await manager.aclose()


# --------------------------------------------------------------------- export


async def test_export_assembles_sections_in_order_and_renders_a_pdf(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    (meeting_dir / "summary.md").write_text("The meeting summary text.", encoding="utf-8")
    # This recording's own item lives inside the meeting folder (FR-4).
    write_item(
        meeting_dir / "action items",
        title="Ours",
        meta={"type": "task", "title": "Ours", "source_meeting": MEETING_NAME},
        body_md="belongs here",
        images=[],
    )
    # A legacy project-level item -- even with a matching `source_meeting` -- is
    # never read; it also stays on disk untouched (FR-6, Q1).
    legacy_md = write_item(
        meeting_dir.parent / "action items",
        title="Legacy leftover",
        meta={"type": "task", "title": "Legacy leftover", "source_meeting": MEETING_NAME},
        body_md="must not appear",
        images=[],
    )

    manager = _manager(config, ledger, FakeLlm())
    export_dir = meeting_dir / "exports" / "260102"
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=export_dir
        )
        job = manager.status(job_id)
        assert job.status == "succeeded"

        export_md = (export_dir / "export.md").read_text(encoding="utf-8")
        assert "The meeting summary text." in export_md
        assert "Ours" in export_md
        assert "belongs here" in export_md
        assert "Legacy leftover" not in export_md, "legacy project-level items are unread"
        assert "must not appear" not in export_md
        assert legacy_md.is_file(), "legacy items are left on disk, never deleted"
        assert "segment 0 discussing the plan" in export_md
        # Fixed section order: Summary -> Action items -> Facts -> Transcript.
        positions = [
            export_md.index("## Summary"),
            export_md.index("## Action items"),
            export_md.index("## Facts"),
            export_md.index("## Transcript"),
        ]
        assert positions == sorted(positions)

        pdf_bytes = (export_dir / "export.pdf").read_bytes()
        assert pdf_bytes.startswith(b"%PDF"), "a real PDF was rendered"
    finally:
        await manager.aclose()


async def test_export_of_an_unsorted_meeting_includes_its_meeting_level_items(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = tmp_app_dir / "vault" / "unsorted" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")
    write_item(
        meeting / "action items",
        title="Unfiled task",
        meta={"type": "task", "title": "Unfiled task", "source_meeting": MEETING_NAME},
        body_md="an unfiled action item",
        images=[],
    )
    write_item(
        meeting / "facts",
        title="Unfiled fact",
        meta={"kind": "answered_question", "title": "Unfiled fact"},
        body_md="an unfiled fact",
        images=[],
    )

    manager = _manager(config, ledger, FakeLlm())
    export_dir = meeting / "exports" / "260102"
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting, output_dir=export_dir
        )
        assert manager.status(job_id).status == "succeeded"

        export_md = (export_dir / "export.md").read_text(encoding="utf-8")
        assert "an unfiled action item" in export_md
        assert "an unfiled fact" in export_md
        assert "_No action items recorded" not in export_md
        assert "_No facts recorded" not in export_md
    finally:
        await manager.aclose()


async def test_export_rewrites_item_screenshot_links_relative_to_the_export_dir(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    write_item(
        meeting_dir / "action items",
        title="With a shot",
        meta={"type": "task", "title": "With a shot", "source_meeting": MEETING_NAME},
        body_md="see the frame",
        images=[("screenshot-0010.png", b"\x89PNG\r\n\x1a\nfake-png-bytes")],
    )

    manager = _manager(config, ledger, FakeLlm())
    export_dir = meeting_dir / "exports" / "260102"
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=export_dir
        )
        assert manager.status(job_id).status == "succeeded"

        export_md = (export_dir / "export.md").read_text(encoding="utf-8")
        # From <meeting>/exports/<YYMMDD>/ back up to <meeting>/action items/<slug>/.
        assert "(../../action items/with-a-shot/screenshot-0010.png)" in export_md
        assert (
            (export_dir / "../../action items/with-a-shot/screenshot-0010.png").resolve().is_file()
        )
    finally:
        await manager.aclose()


async def test_export_warns_on_the_job_when_the_pdf_font_degrades(
    config: Config,
    ledger: Ledger,
    meeting_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FR-3: with no Cyrillic-capable font available the export still succeeds,
    # but the operator must see the degradation on the job -- not only in the
    # service log.
    fontless = tmp_path / "nowindows"
    (fontless / "Fonts").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(fontless))
    (meeting_dir / "summary.md").write_text("The meeting summary text.", encoding="utf-8")

    manager = _manager(config, ledger, FakeLlm())
    export_dir = meeting_dir / "exports" / "260103"
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=export_dir
        )
        job = manager.status(job_id)

        assert job.status == "succeeded"
        assert (export_dir / "export.pdf").read_bytes().startswith(b"%PDF")
        assert [w for w in job.warnings if "latin" in w.lower()], (
            f"no font-degradation warning on the job: {job.warnings}"
        )
    finally:
        await manager.aclose()


def _russian_transcript_doc() -> dict[str, Any]:
    """The `meeting_dir` transcript as a real Russian meeting has it."""
    texts = [
        "Начали встречу с обзора открытых задач.",
        "Проверили статус переводов интерфейса.",
    ]
    doc = _transcript_doc(segment_count=len(texts))
    for segment, text in zip(doc["segments"], texts, strict=True):
        segment["text"] = text
    doc["text"] = " ".join(texts)
    return doc


@requires_arial
async def test_export_renders_cyrillic_with_embedded_fonts(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    # FR-1/FR-5: the regression guard for "every Russian character is a black
    # box". Driven through the real export job (the flow the operator runs),
    # asserted on the produced `export.pdf` the way a reader opens it: which
    # fonts it embeds, and what text can be selected out of it.
    (meeting_dir / "summary.md").write_text(
        "## Итоги\n\nОбсудили план релиза и распределили задачи.", encoding="utf-8"
    )
    (meeting_dir / "transcript.json").write_text(
        json.dumps(_russian_transcript_doc()), encoding="utf-8"
    )
    # Items live in the recording's own folder -- the post-move read path.
    write_item(
        meeting_dir / "action items",
        title="Подготовить сборку",
        meta={"type": "task", "title": "Подготовить сборку", "source_meeting": MEETING_NAME},
        body_md="Нужно собрать инсталлятор до пятницы.",
        images=[],
    )
    write_item(
        meeting_dir / "facts",
        title="Сроки согласованы",
        meta={"kind": "decision", "title": "Сроки согласованы", "source_meeting": MEETING_NAME},
        body_md="Релиз назначен на конец месяца.",
        images=[],
    )

    manager = _manager(config, ledger, FakeLlm())
    export_dir = meeting_dir / "exports" / "260104"
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=export_dir
        )
        job = manager.status(job_id)
        assert job.status == "succeeded", job.error_message

        pdf_path = export_dir / "export.pdf"
        base_fonts = embedded_base_fonts(pdf_path)
        assert any(name.endswith("+ArialMT") for name in base_fonts), (
            f"the export embeds no Cyrillic-capable Arial subset: {sorted(base_fonts)}"
        )

        text = " ".join(extract_text(pdf_path).split())
        for section, needle in (
            ("Summary", "Обсудили план релиза"),
            ("Action items", "Нужно собрать инсталлятор"),
            ("Facts", "Релиз назначен"),
            ("Transcript", "Проверили статус переводов"),
        ):
            assert needle in text, f"{section}: {needle!r} missing from the export text: {text!r}"
        assert "■" not in text, f"replacement boxes in the export text: {text!r}"
    finally:
        await manager.aclose()
