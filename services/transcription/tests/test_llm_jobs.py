"""Tests for the derived (LLM) job types inside the job manager.

Drives `JobManager` with `FakeLlm` (tests/fakes.py): no HTTP, no model, no
llama.cpp, no network (FR-15). The vault tree is a plain tempdir shaped like
`<root>/<PROJECT>/<meeting>/`.
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
from fakes import FakeLlm
from pdf_asserts import embedded_base_fonts, extract_text

from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger

MEETING_NAME = "260101 - Planning"
# The share-ready export PDF name for `meeting_dir` (project `ELS`, the
# meeting above): `<project> - <date> - <title>.pdf`.
EXPORT_PDF_NAME = "ELS - 2026-01-01 - Planning.pdf"

RUSSIAN_DIRECTIVE = "Write your entire answer in Russian."
SOFT_RULE = "same language the transcript is written in"

_FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

requires_arial = pytest.mark.skipif(
    not (_FONTS_DIR / "arial.ttf").is_file(),
    reason=r"needs the stock Arial family in %WINDIR%\Fonts",
)


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


def _manager(config: Config, ledger: Ledger, llm: FakeLlm) -> JobManager:
    return JobManager(config, ledger, llm_factory=lambda _cfg: llm)


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

    llm = FakeLlm(responses=["part summary", "part summary", "part summary", "merged summary"])
    manager = _manager(_small_ctx(config), ledger, llm)
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
        second = await manager.submit(
            job_type="export", input_path=str(meeting_dir), output_dir=str(meeting_dir)
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


@pytest.mark.parametrize("retired", ["facts", "action_items"])
async def test_a_retired_job_type_is_rejected_at_submission(
    config: Config, ledger: Ledger, meeting_dir: Path, retired: str
) -> None:
    """`facts` and `action_items` jobs were retired (the summary carries the
    notable facts and the action items); submitting one must answer
    `invalid_request` and leave no ledger row."""
    from transcription.errors import ServiceError

    manager = _manager(config, ledger, FakeLlm())
    try:
        with pytest.raises(ServiceError) as excinfo:
            await manager.submit(
                job_type=retired,
                input_path=str(meeting_dir),
                output_dir=str(meeting_dir),
            )
        assert excinfo.value.kind is ErrorKind.INVALID_REQUEST
        assert ledger.list_jobs(limit=10) == []
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


# ------------------------------------------------------ truncation recovery


async def test_a_truncated_summary_is_split_and_retried(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    # ~3000 chars in one (llm_ctx=2048 -> floor 1024-token) chunk: big
    # enough that the halved budget splits it into two pieces.
    meeting = _meeting_with_language(tmp_app_dir, "en", segment_count=30, repeat_text=3)
    truncated_thought = "Okay, let me think about this meeting at great len"
    llm = FakeLlm(
        responses=[(truncated_thought, "length"), "half one", "half two", "merged summary"]
    )
    manager = _manager(_small_ctx(config), ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 4, "one truncated call, two map retries, one reduce"

        summary = (meeting / "summary.md").read_text(encoding="utf-8")
        assert summary.strip() == "merged summary"
        # The cut-off text (raw chain-of-thought whose </think> never came)
        # must not reach any artifact.
        assert truncated_thought not in summary
        reasoning_path = meeting / "summary.reasoning.md"
        if reasoning_path.exists():
            assert truncated_thought not in reasoning_path.read_text(encoding="utf-8")
    finally:
        await manager.aclose()


async def test_a_truncated_summary_far_below_the_budget_is_still_split(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    # The 260825 field report: with the default 32k context the whole
    # transcript sits in one chunk far below the input budget. Splitting
    # used to halve the *budget* only, which handed back the same single
    # chunk and turned the first truncated call into a hard failure.
    meeting = _meeting_with_language(tmp_app_dir, "en", segment_count=30, repeat_text=3)
    llm = FakeLlm(responses=[("Okay, let me think", "length"), "half one", "half two", "merged"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 4, "one truncated call, two map retries, one reduce"
        assert (meeting / "summary.md").read_text(encoding="utf-8").strip() == "merged"
    finally:
        await manager.aclose()


async def test_summarize_truncated_at_the_floor_fails_with_an_honest_error(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(responses=[("Okay, let me think", "length")])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting_dir, output_dir=meeting_dir
        )
        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.LLM_OUTPUT
        assert job.error_message is not None
        assert "token limit" in job.error_message
        assert not (meeting_dir / "summary.md").exists()
    finally:
        await manager.aclose()


# ------------------------------------------------------- tokenizer-led budgets


async def test_chunking_follows_the_provider_tokenizer_not_the_heuristic(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    # ~7400 chars: the provider tokenizer (len // 4 -> ~1850 tokens) packs
    # two chunks; the fallback heuristic (len // 2) would have made four.
    meeting = _meeting_with_language(tmp_app_dir, "en", segment_count=200)
    llm = FakeLlm(responses=["part summary", "part summary", "merged"])
    manager = _manager(_small_ctx(config), ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"
        map_calls = [call for call in llm.calls if "summarizing one part" in call[0]["content"]]
        assert len(map_calls) == 2
    finally:
        await manager.aclose()


async def test_a_very_long_transcript_reduces_in_budget_fitted_rounds(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    # ~22000 chars -> six map chunks; every response is long enough that
    # only two partials fit one merge group, forcing a multi-round reduce.
    meeting = _meeting_with_language(tmp_app_dir, "en", segment_count=200, repeat_text=3)
    long_partial = "A thorough part summary. " * 64  # ~1600 chars -> ~412 tokens
    llm = FakeLlm(responses=[long_partial])
    manager = _manager(_small_ctx(config), ledger, llm)
    try:
        job_id = await _run_job(
            manager, job_type="summarize", input_path=meeting, output_dir=meeting
        )
        assert manager.status(job_id).status == "succeeded"

        reduce_calls = [
            call for call in llm.calls if "merge partial summaries" in call[0]["content"]
        ]
        assert len(reduce_calls) >= 2, "expected the reduce to need more than one round"
        summary = (meeting / "summary.md").read_text(encoding="utf-8")
        assert summary.strip() == long_partial.strip()
    finally:
        await manager.aclose()


# --------------------------------------------------------------------- export


async def test_export_assembles_sections_in_order_and_renders_a_pdf(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    (meeting_dir / "summary.md").write_text("The meeting summary text.", encoding="utf-8")
    # A legacy per-meeting action-items tree from before extraction was
    # retired: left on disk untouched, never read into an export.
    legacy_dir = meeting_dir / "action items" / "legacy-leftover"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "legacy-leftover.md").write_text(
        '---\ntitle: "Legacy leftover"\n---\n\n# Legacy leftover\n\nmust not appear\n',
        encoding="utf-8",
    )

    manager = _manager(config, ledger, FakeLlm())
    try:
        # The export lands in the meeting folder itself (no dated subfolder),
        # under stable names, so a re-export overwrites in place.
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=meeting_dir
        )
        job = manager.status(job_id)
        assert job.status == "succeeded"

        export_md = (meeting_dir / "export.md").read_text(encoding="utf-8")
        assert "The meeting summary text." in export_md
        assert "must not appear" not in export_md, "legacy action items are unread"
        assert (legacy_dir / "legacy-leftover.md").is_file(), "legacy items stay on disk"
        assert "segment 0 discussing the plan" in export_md
        # Fixed section order: Summary -> Transcript. The retired facts and
        # action-items jobs left no section behind.
        assert export_md.index("## Summary") < export_md.index("## Transcript")
        assert "## Action items" not in export_md
        assert "## Facts" not in export_md

        pdf_bytes = (meeting_dir / EXPORT_PDF_NAME).read_bytes()
        assert pdf_bytes.startswith(b"%PDF"), "a real PDF was rendered"
    finally:
        await manager.aclose()


async def test_reexporting_overwrites_the_same_files_in_place(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    (meeting_dir / "summary.md").write_text("First summary.", encoding="utf-8")
    manager = _manager(config, ledger, FakeLlm())
    try:
        await _run_job(manager, job_type="export", input_path=meeting_dir, output_dir=meeting_dir)
        (meeting_dir / "summary.md").write_text("Second summary.", encoding="utf-8")
        second = await manager.submit(
            job_type="export", input_path=str(meeting_dir), output_dir=str(meeting_dir)
        )
        await _wait_until_terminal(manager, second)
        assert manager.status(second).status == "succeeded"

        assert "Second summary." in (meeting_dir / "export.md").read_text(encoding="utf-8")
        # Still exactly one export pair -- no dated copies accumulate.
        assert len(list(meeting_dir.glob("*.pdf"))) == 1
        assert not (meeting_dir / "exports").exists()
    finally:
        await manager.aclose()


async def test_export_of_an_unsorted_meeting_drops_the_project_from_the_pdf_name(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    meeting = tmp_app_dir / "vault" / "unsorted" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "transcript.json").write_text(json.dumps(_transcript_doc()), encoding="utf-8")

    manager = _manager(config, ledger, FakeLlm())
    try:
        job_id = await _run_job(manager, job_type="export", input_path=meeting, output_dir=meeting)
        assert manager.status(job_id).status == "succeeded"
        assert (meeting / "2026-01-01 - Planning.pdf").is_file()
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
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=meeting_dir
        )
        job = manager.status(job_id)

        assert job.status == "succeeded"
        assert (meeting_dir / EXPORT_PDF_NAME).read_bytes().startswith(b"%PDF")
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
    # asserted on the produced PDF the way a reader opens it: which fonts it
    # embeds, and what text can be selected out of it.
    (meeting_dir / "summary.md").write_text(
        "## Итоги\n\nОбсудили план релиза и распределили задачи.", encoding="utf-8"
    )
    (meeting_dir / "transcript.json").write_text(
        json.dumps(_russian_transcript_doc()), encoding="utf-8"
    )
    manager = _manager(config, ledger, FakeLlm())
    try:
        job_id = await _run_job(
            manager, job_type="export", input_path=meeting_dir, output_dir=meeting_dir
        )
        job = manager.status(job_id)
        assert job.status == "succeeded", job.error_message

        pdf_path = meeting_dir / EXPORT_PDF_NAME
        base_fonts = embedded_base_fonts(pdf_path)
        assert any(name.endswith("+ArialMT") for name in base_fonts), (
            f"the export embeds no Cyrillic-capable Arial subset: {sorted(base_fonts)}"
        )

        text = " ".join(extract_text(pdf_path).split())
        for section, needle in (
            ("Summary", "Обсудили план релиза"),
            ("Transcript", "Проверили статус переводов"),
        ):
            assert needle in text, f"{section}: {needle!r} missing from the export text: {text!r}"
        assert "■" not in text, f"replacement boxes in the export text: {text!r}"
    finally:
        await manager.aclose()
