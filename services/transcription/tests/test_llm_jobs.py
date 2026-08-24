"""Tests for the derived (LLM) job types inside the job manager.

Drives `JobManager` with `FakeLlm` + `FakeFrameExtractor` (tests/fakes.py):
no HTTP, no model, no llama.cpp, no network (FR-15). The vault tree is a
plain tempdir shaped like `<root>/<PROJECT>/<meeting>/`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeFrameExtractor, FakeLlm

from transcription.artifacts import parse_front_matter
from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger

MEETING_NAME = "260101 - Planning"


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
    items_dir = meeting_dir.parent / "action items"
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
    items_dir = meeting_dir.parent / "action items"
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
        assert meta["screenshots"] == "succeeded"
        assert meta["timestamps"] == [10.0, 20.0]
        assert "# Fix the login flow" in body
        assert "login flow breaks" in body

        screenshots = sorted(p.name for p in task_dir.glob("*.png"))
        assert screenshots == ["screenshot-0010.png", "screenshot-0020.png"]
        assert "![screenshot-0010.png](screenshot-0010.png)" in body

        assert (items_dir / "investigate-caching" / "investigate-caching.md").is_file()

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
    items_dir = meeting_dir.parent / "action items"
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


async def test_persistently_invalid_json_fails_with_llm_output(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(responses=["nope"])  # the single response repeats: repair also fails
    manager = _manager(config, ledger, llm)
    items_dir = meeting_dir.parent / "action items"
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
    items_dir = meeting_dir.parent / "action items"
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
    facts_dir = meeting.parent / "facts"
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
    items_dir = meeting_dir.parent / "action items"
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
    # One action item from THIS meeting, one from another (must be filtered out).
    from transcription.artifacts import write_item

    items_dir = meeting_dir.parent / "action items"
    write_item(
        items_dir,
        title="Ours",
        meta={"type": "task", "title": "Ours", "source_meeting": MEETING_NAME},
        body_md="belongs here",
        images=[],
    )
    write_item(
        items_dir,
        title="Not ours",
        meta={"type": "task", "title": "Not ours", "source_meeting": "260202 - Other"},
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
        assert "must not appear" not in export_md
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
