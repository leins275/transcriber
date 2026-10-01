"""What the voice memory accepts as a reference, and per-segment naming.

Companion to `test_speaker_matching.py` (which covers the per-cluster
matching): these tests pin the rules that decide which named voices are
evidence at all, the `auto` provenance map in `speakers.json`, and
`identify_segments`.

Synthetic voice embeddings: orthogonal unit vectors are distinct voices.
"""

from __future__ import annotations

import json
from pathlib import Path

from transcription.speaker_matching import (
    SEGMENT_MIN_SEC,
    SEGMENT_MIN_VOTES,
    assess_exemplars,
    auto_assign_speakers,
    collect_project_voiceprints,
    identify_segments,
    scan_meeting,
    scan_project,
    segment_spans,
)

ALICE = [1.0, 0.0, 0.0, 0.0]
BOB = [0.0, 1.0, 0.0, 0.0]
# Closer to Alice than to Bob, the way one noisy segment of hers would be.
ALICE_ISH = [0.9, 0.1, 0.0, 0.2]
BOB_ISH = [0.1, 0.9, 0.0, 0.2]
# Equally like both: says nothing about who is speaking.
EITHER = [0.7, 0.7, 0.0, 0.0]


def _write_meeting(
    meeting_dir: Path,
    *,
    embeddings: dict[str, list[float]],
    speakers: dict[int, str],
    names: dict[str, str] | None = None,
    auto: dict[str, str] | None = None,
    segment_sec: float = 20.0,
) -> list[dict[str, object]]:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    segments: list[dict[str, object]] = [
        {
            "id": seg_id,
            "start": seg_id * segment_sec,
            "end": seg_id * segment_sec + segment_sec,
            "text": "hi",
            "speaker": label,
        }
        for seg_id, label in speakers.items()
    ]
    doc = {
        "schema_version": 1,
        "text": "hi",
        "segments": segments,
        "diarization": {"status": "succeeded", "speaker_embeddings": embeddings},
    }
    (meeting_dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    if names is not None:
        body: dict[str, object] = {"schema_version": 1, "assignments": names}
        if auto:
            body["auto"] = auto
        (meeting_dir / "speakers.json").write_text(json.dumps(body), encoding="utf-8")
    return segments


def _speakers_file(meeting_dir: Path) -> dict[str, dict[str, str]]:
    data = json.loads((meeting_dir / "speakers.json").read_text(encoding="utf-8"))
    return {"assignments": data["assignments"], "auto": data.get("auto", {})}


def _qualities(project: Path) -> dict[tuple[str, str], str]:
    return {
        (item.exemplar.meeting, item.exemplar.name): item.quality
        for item in assess_exemplars(scan_project(project))
    }


# -- which named voices are evidence -------------------------------------------


def test_a_meeting_reports_why_it_contributes_nothing(tmp_path: Path) -> None:
    named = tmp_path / "named"
    _write_meeting(
        named, embeddings={"Speaker 1": ALICE}, speakers={0: "Speaker 1"}, names={"0": "A"}
    )
    unnamed = tmp_path / "unnamed"
    _write_meeting(unnamed, embeddings={"Speaker 1": ALICE}, speakers={0: "Speaker 1"})
    seeded = tmp_path / "seeded"
    _write_meeting(
        seeded,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Speaker 1"},
    )
    undiarized = tmp_path / "undiarized"
    _write_meeting(undiarized, embeddings={}, speakers={0: "Speaker 1"})

    assert scan_meeting(named)[0] == "named"
    assert scan_meeting(unnamed)[0] == "unnamed"
    assert scan_meeting(seeded) == ("unnamed", [])
    assert scan_meeting(undiarized)[0] == "no_voices"
    assert scan_meeting(tmp_path / "missing")[0] == "no_voices"


def test_a_voice_heard_for_a_few_seconds_is_not_a_reference(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Brief",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
        segment_sec=4.0,
    )
    new_meeting = project / "260803 - New"
    new_meeting.mkdir()

    assert _qualities(project) == {("260801 - Brief", "Alice"): "short"}
    assert collect_project_voiceprints(new_meeting) == {}


def test_a_few_corrected_lines_do_not_name_a_whole_voice(tmp_path: Path) -> None:
    """Two lines fixed by hand inside a cluster nobody named say who spoke
    those lines, not whose voice the cluster is."""
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Long",
        embeddings={"Speaker 1": ALICE},
        speakers={seg_id: "Speaker 1" for seg_id in range(10)},
        names={"3": "Bob", "7": "Bob"},
    )

    assert _qualities(project) == {("260801 - Long", "Bob"): "partial"}


def test_the_machine_does_not_learn_from_its_own_guesses(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    meeting = project / "260801 - Auto"
    _write_meeting(
        meeting,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1", 1: "Speaker 1"},
        names={"0": "Alice", "1": "Alice"},
        auto={"0": "Alice", "1": "Alice"},
    )

    assert _qualities(project) == {("260801 - Auto", "Alice"): "unconfirmed"}


def test_a_name_the_operator_changed_is_theirs_again(tmp_path: Path) -> None:
    """The app carries the `auto` map over untouched when it saves; a
    segment whose name no longer matches it is a hand edit."""
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Corrected",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1", 1: "Speaker 1"},
        names={"0": "Alicia", "1": "Alicia"},
        auto={"0": "Alice", "1": "Alice"},
    )

    assert _qualities(project) == {("260801 - Corrected", "Alicia"): "ok"}


def test_a_file_without_provenance_is_all_the_operators(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Legacy",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )

    assert _qualities(project) == {("260801 - Legacy", "Alice"): "ok"}


def test_one_mislabelled_meeting_does_not_poison_a_known_voice(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    for day in ("260801", "260802", "260803"):
        _write_meeting(
            project / f"{day} - Sync",
            embeddings={"Speaker 1": ALICE, "Speaker 2": BOB},
            speakers={0: "Speaker 1", 1: "Speaker 2"},
            names={"0": "Alice", "1": "Bob"},
        )
    # Alice's voice, labelled as Bob.
    _write_meeting(
        project / "260804 - Oops",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )
    new_meeting = project / "260805 - New"
    new_meeting.mkdir()

    assessed = {
        (item.exemplar.meeting, item.exemplar.name): item
        for item in assess_exemplars(scan_project(project))
    }

    oops = assessed[("260804 - Oops", "Bob")]
    assert (oops.quality, oops.conflicts_with) == ("conflict", "Alice")
    assert all(item.quality == "ok" for key, item in assessed.items() if key[0] != "260804 - Oops")
    prints = collect_project_voiceprints(new_meeting)
    assert prints["Bob"] == [BOB, BOB, BOB]
    assert prints["Alice"] == [ALICE, ALICE, ALICE]


def test_similar_voices_under_their_own_names_are_not_a_conflict(tmp_path: Path) -> None:
    """Two people who merely sound alike each resemble their own name's
    samples best, so neither is set aside."""
    project = tmp_path / "ACME"
    twin_a = [1.0, 0.0, 0.0, 0.0]
    twin_b = [0.9, 0.3, 0.0, 0.0]
    for day in ("260801", "260802"):
        _write_meeting(
            project / f"{day} - Sync",
            embeddings={"Speaker 1": twin_a, "Speaker 2": twin_b},
            speakers={0: "Speaker 1", 1: "Speaker 2"},
            names={"0": "Anna", "1": "Hanna"},
        )

    assert set(_qualities(project).values()) == {"ok"}


# -- provenance of the names this module writes ---------------------------------


def test_auto_naming_records_what_it_wrote(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )
    new_meeting = project / "260803 - New"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1", 1: "Speaker 1"},
        names={"1": "Somebody"},
    )

    result = auto_assign_speakers(new_meeting, {"Speaker 1": ALICE}, segments, threshold=0.5)

    assert (result.named, result.by_segment_voice, result.names) == (1, 0, ("Alice",))
    assert _speakers_file(new_meeting) == {
        "assignments": {"0": "Alice", "1": "Somebody"},
        "auto": {"0": "Alice"},
    }


def test_a_re_run_may_revise_its_own_guess_but_never_the_operators(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )
    new_meeting = project / "260803 - New"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1", 1: "Speaker 1", 2: "Speaker 1"},
        # 0: an earlier guess left alone; 1: an earlier guess the operator
        # replaced; 2: typed by the operator from the start.
        names={"0": "Alice", "1": "Carol", "2": "Dave"},
        auto={"0": "Alice", "1": "Alice"},
    )

    result = auto_assign_speakers(new_meeting, {"Speaker 1": BOB}, segments, threshold=0.5)

    assert result.named == 1
    assert _speakers_file(new_meeting) == {
        "assignments": {"0": "Bob", "1": "Carol", "2": "Dave"},
        # The dead entry for segment 1 is dropped on the way through.
        "auto": {"0": "Bob"},
    }


def test_a_pass_that_changes_nothing_does_not_rewrite_the_file(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )
    new_meeting = project / "260803 - New"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
        auto={"0": "Alice"},
    )
    before = (new_meeting / "speakers.json").read_bytes()

    result = auto_assign_speakers(new_meeting, {"Speaker 1": ALICE}, segments, threshold=0.5)

    assert result.named == 0
    assert (new_meeting / "speakers.json").read_bytes() == before


# -- per-segment identification -------------------------------------------------


def _segments(labels: dict[int, str]) -> list[dict[str, object]]:
    return [
        {"id": seg_id, "start": seg_id * 5.0, "end": seg_id * 5.0 + 5.0, "speaker": label}
        for seg_id, label in labels.items()
    ]


VOICEPRINTS = {"Alice": [ALICE], "Bob": [BOB]}


def test_several_agreeing_segments_reveal_a_second_person_in_a_cluster() -> None:
    segments = _segments({seg_id: "Speaker 1" for seg_id in range(6)})
    embeddings = {
        "0": ALICE_ISH,
        "1": ALICE_ISH,
        "2": ALICE_ISH,
        "3": BOB_ISH,
        "4": BOB_ISH,
        "5": BOB_ISH,
    }

    named = identify_segments(segments, embeddings, VOICEPRINTS, threshold=0.5)

    assert named == {"0": "Alice", "1": "Alice", "2": "Alice", "3": "Bob", "4": "Bob", "5": "Bob"}


def test_a_lone_dissenting_segment_is_noise() -> None:
    segments = _segments({seg_id: "Speaker 1" for seg_id in range(5)})
    embeddings = {"0": ALICE_ISH, "1": ALICE_ISH, "2": ALICE_ISH, "3": BOB_ISH, "4": BOB_ISH}
    assert SEGMENT_MIN_VOTES == 3

    named = identify_segments(segments, embeddings, VOICEPRINTS, threshold=0.5)

    assert named == {"0": "Alice", "1": "Alice", "2": "Alice"}


def test_votes_are_counted_per_cluster() -> None:
    """Three Bob-like segments spread over three clusters are three lone
    dissenters, not a second person."""
    segments = _segments({0: "Speaker 1", 1: "Speaker 2", 2: "Speaker 3"})
    embeddings = {"0": BOB_ISH, "1": BOB_ISH, "2": BOB_ISH}

    assert identify_segments(segments, embeddings, VOICEPRINTS, threshold=0.5) == {}


def test_an_ambiguous_or_unknown_voice_names_nothing() -> None:
    segments = _segments({seg_id: "Speaker 1" for seg_id in range(3)})
    stranger = [0.0, 0.0, 0.0, 1.0]

    assert (
        identify_segments(
            segments, {"0": EITHER, "1": EITHER, "2": EITHER}, VOICEPRINTS, threshold=0.5
        )
        == {}
    )
    assert (
        identify_segments(
            segments, {"0": stranger, "1": stranger, "2": stranger}, VOICEPRINTS, threshold=0.5
        )
        == {}
    )
    assert identify_segments(segments, {}, VOICEPRINTS, threshold=0.5) == {}
    assert identify_segments(segments, {"0": ALICE}, {}, threshold=0.5) == {}


def test_only_nameable_long_enough_segments_are_worth_embedding(tmp_path: Path) -> None:
    meeting = tmp_path / "ACME" / "260803 - New"
    meeting.mkdir(parents=True)
    (meeting / "speakers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assignments": {"1": "Typed by hand", "2": "Speaker 1", "3": "Guess"},
                "auto": {"3": "Guess"},
            }
        ),
        encoding="utf-8",
    )
    segments = [
        {"id": 0, "start": 0.0, "end": 5.0},
        {"id": 1, "start": 5.0, "end": 10.0},
        {"id": 2, "start": 10.0, "end": 15.0},
        {"id": 3, "start": 15.0, "end": 20.0},
        {"id": 4, "start": 20.0, "end": 20.0 + SEGMENT_MIN_SEC / 2},
    ]

    assert segment_spans(segments, meeting) == [
        ("0", 0.0, 5.0),
        ("2", 10.0, 15.0),
        ("3", 15.0, 20.0),
    ]


def test_a_merged_cluster_is_split_between_the_two_people_it_holds(tmp_path: Path) -> None:
    """End to end over one meeting: the diarizer put Alice and Bob into one
    cluster whose averaged voice matches Alice. Bob's segments are named for
    Bob from their own voice; the short one inherits the cluster's name."""
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE, "Speaker 2": BOB},
        speakers={0: "Speaker 1", 1: "Speaker 2"},
        names={"0": "Alice", "1": "Bob"},
    )
    new_meeting = project / "260803 - New"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE_ISH},
        speakers={seg_id: "Speaker 1" for seg_id in range(7)},
    )
    segment_embeddings = {
        "0": ALICE_ISH,
        "1": ALICE_ISH,
        "2": ALICE_ISH,
        "3": BOB_ISH,
        "4": BOB_ISH,
        "5": BOB_ISH,
        # Segment 6 was too short to embed.
    }

    result = auto_assign_speakers(
        new_meeting,
        {"Speaker 1": ALICE_ISH},
        segments,
        threshold=0.5,
        segment_embeddings=segment_embeddings,
    )

    assert (result.named, result.by_segment_voice, result.names) == (7, 3, ("Alice", "Bob"))
    assert _speakers_file(new_meeting)["assignments"] == {
        "0": "Alice",
        "1": "Alice",
        "2": "Alice",
        "3": "Bob",
        "4": "Bob",
        "5": "Bob",
        "6": "Alice",
    }


def test_segments_of_an_unrecognized_cluster_can_still_be_named(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    _write_meeting(
        project / "260801 - Kickoff",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )
    new_meeting = project / "260803 - New"
    # The cluster's averaged voice matches nobody...
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": [0.0, 0.0, 1.0, 0.0]},
        speakers={seg_id: "Speaker 1" for seg_id in range(4)},
    )
    stranger = [0.0, 0.0, 1.0, 0.0]

    result = auto_assign_speakers(
        new_meeting,
        {"Speaker 1": stranger},
        segments,
        threshold=0.5,
        # ...but three of its segments are plainly Bob.
        segment_embeddings={"0": BOB_ISH, "1": BOB_ISH, "2": BOB_ISH, "3": stranger},
    )

    assert (result.named, result.by_segment_voice) == (3, 3)
    assert _speakers_file(new_meeting)["assignments"] == {"0": "Bob", "1": "Bob", "2": "Bob"}
