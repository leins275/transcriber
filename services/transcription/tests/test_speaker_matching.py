"""Tests for cross-meeting speaker naming (`speaker_matching.py`).

Synthetic voice embeddings: orthogonal unit vectors are distinct voices,
and a scaled copy of a vector is "the same voice" (cosine 1.0).
"""

from __future__ import annotations

import json
from pathlib import Path

from transcription.speaker_matching import (
    auto_assign_speakers,
    collect_project_voiceprints,
    match_speakers,
)

ALICE = [1.0, 0.0, 0.0, 0.0]
BOB = [0.0, 1.0, 0.0, 0.0]
STRANGER = [0.0, 0.0, 0.0, 1.0]


def _write_meeting(
    meeting_dir: Path,
    *,
    embeddings: dict[str, list[float]] | None,
    speakers: dict[int, str],
    names: dict[str, str] | None = None,
) -> None:
    """A minimal diarized meeting: segments 0..n with `speakers` labels,
    `embeddings` under diarization, and optional speakers.json `names`
    (segment id -> operator name)."""
    meeting_dir.mkdir(parents=True, exist_ok=True)
    segments = [
        {
            "id": seg_id,
            "start": float(seg_id),
            "end": float(seg_id) + 1.0,
            "text": "hi",
            "speaker": label,
        }
        for seg_id, label in speakers.items()
    ]
    doc: dict[str, object] = {"schema_version": 1, "text": "hi", "segments": segments}
    if embeddings is not None:
        doc["diarization"] = {"status": "succeeded", "speaker_embeddings": embeddings}
    (meeting_dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    if names:
        (meeting_dir / "speakers.json").write_text(
            json.dumps({"schema_version": 1, "assignments": names}), encoding="utf-8"
        )


def _assignments(meeting_dir: Path) -> dict[str, str]:
    data = json.loads((meeting_dir / "speakers.json").read_text(encoding="utf-8"))
    return data["assignments"]


def test_voiceprints_join_names_to_embeddings_across_siblings(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE, "Speaker 2": BOB},
        speakers={0: "Speaker 1", 1: "Speaker 2", 2: "Speaker 1"},
        names={"0": "Алиса", "1": "Bob", "2": "Алиса"},
    )
    # A sibling with no assignments contributes nothing.
    _write_meeting(
        project / "260802 - Unnamed",
        embeddings={"Speaker 1": STRANGER},
        speakers={0: "Speaker 1"},
    )
    new_meeting = project / "260803 - New"
    new_meeting.mkdir()

    prints = collect_project_voiceprints(new_meeting)

    assert set(prints) == {"Алиса", "Bob"}
    assert prints["Алиса"] == [ALICE]


def test_matching_is_greedy_thresholded_and_one_name_per_meeting() -> None:
    voiceprints = {"Алиса": [ALICE], "Bob": [BOB]}
    embeddings = {
        "Speaker 1": [2.0, 0.0, 0.0, 0.0],  # Alice's voice, scaled
        "Speaker 2": STRANGER,  # nobody we know
    }

    matches = match_speakers(embeddings, voiceprints, threshold=0.5)

    assert matches == {"Speaker 1": "Алиса"}


def test_auto_assign_fills_only_unnamed_segments(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Алиса"},
    )
    new_meeting = project / "260803 - New"
    segments = [
        {"id": 0, "speaker": "Speaker 1"},
        {"id": 1, "speaker": "Speaker 1"},
        {"id": 2, "speaker": "Speaker 2"},
    ]
    _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE, "Speaker 2": STRANGER},
        speakers={0: "Speaker 1", 1: "Speaker 1", 2: "Speaker 2"},
        # The operator already renamed segment 1 by hand: that entry wins.
        names={"1": "Кто-то другой"},
    )

    added = auto_assign_speakers(
        new_meeting,
        {"Speaker 1": ALICE, "Speaker 2": STRANGER},
        segments,
        threshold=0.5,
    )

    assert added == 1  # segment 0 only: 1 was operator-named, 2 is a stranger
    assert _assignments(new_meeting) == {"0": "Алиса", "1": "Кто-то другой"}


def test_auto_assign_without_speaker_memory_writes_nothing(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    new_meeting = project / "260803 - New"
    new_meeting.mkdir(parents=True)

    added = auto_assign_speakers(
        new_meeting, {"Speaker 1": ALICE}, [{"id": 0, "speaker": "Speaker 1"}], threshold=0.5
    )

    assert added == 0
    assert not (new_meeting / "speakers.json").exists()


def test_a_threshold_above_one_disables_auto_naming(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Алиса"},
    )
    new_meeting = project / "260803 - New"
    new_meeting.mkdir()

    added = auto_assign_speakers(
        new_meeting, {"Speaker 1": ALICE}, [{"id": 0, "speaker": "Speaker 1"}], threshold=1.5
    )

    assert added == 0
    assert not (new_meeting / "speakers.json").exists()


def test_majority_vote_survives_a_stray_mislabeled_segment(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1", 1: "Speaker 1", 2: "Speaker 1"},
        names={"0": "Алиса", "1": "Алиса", "2": "Опечатка"},
    )
    new_meeting = project / "260803 - New"
    new_meeting.mkdir()

    prints = collect_project_voiceprints(new_meeting)

    assert set(prints) == {"Алиса"}
