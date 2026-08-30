"""Contract tests for per-recording export assembly (``exporting.py``).

The export document is Summary then Transcript (action items live inside
the summary since extraction was retired), and every missing ingredient
degrades to a placeholder plus a warning instead of failing the assembly.
"""

from __future__ import annotations

import json
from pathlib import Path

from transcription.exporting import build_export_md

MEETING_NAME = "260824 - standup"


def _meeting_dir(tmp_path: Path) -> Path:
    meeting_dir = tmp_path / "vault" / "ELS" / MEETING_NAME
    meeting_dir.mkdir(parents=True)
    return meeting_dir


def _write_transcript(meeting_dir: Path) -> None:
    (meeting_dir / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"id": 0, "start": 0.0, "end": 2.0, "text": "hello", "speaker": "S1"},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_export_is_summary_then_transcript(tmp_path: Path) -> None:
    meeting_dir = _meeting_dir(tmp_path)
    (meeting_dir / "summary.md").write_text("The meeting summary.", encoding="utf-8")
    _write_transcript(meeting_dir)

    md, warnings = build_export_md(meeting_dir=meeting_dir, meeting_name=MEETING_NAME)

    assert warnings == []
    assert md.index(f"# {MEETING_NAME}") < md.index("## Summary") < md.index("## Transcript")
    assert "The meeting summary." in md
    assert "[0:00] S1: hello" in md
    # Action items are the summary's job now; the export has no section of its own.
    assert "## Action items" not in md


def test_missing_summary_degrades_to_placeholder_and_warning(tmp_path: Path) -> None:
    meeting_dir = _meeting_dir(tmp_path)
    _write_transcript(meeting_dir)

    md, warnings = build_export_md(meeting_dir=meeting_dir, meeting_name=MEETING_NAME)

    assert "_No summary has been generated for this recording yet._" in md
    assert any("no summary.md" in warning for warning in warnings)


def test_missing_transcript_degrades_to_placeholder_and_warning(tmp_path: Path) -> None:
    meeting_dir = _meeting_dir(tmp_path)
    (meeting_dir / "summary.md").write_text("The meeting summary.", encoding="utf-8")

    md, warnings = build_export_md(meeting_dir=meeting_dir, meeting_name=MEETING_NAME)

    assert "_The transcript could not be read._" in md
    assert any("transcript.json" in warning for warning in warnings)
