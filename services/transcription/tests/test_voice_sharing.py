"""Voices shared across projects (`speaker_matching.collect_known_voices`).

A person is the same person in every project, so the memory spans the vault.
What differs per project is which names are *at home* there (heard in its
own meetings, or listed on its roster) and which are newcomers held to a
stricter bar, and whether a strict roster narrows the names at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from transcription.speaker_matching import (
    MIN_EXEMPLAR_SPEECH_SEC,
    NEWCOMER_MIN_SIMILARITY,
    auto_assign_speakers,
    collect_known_voices,
    identify_segments,
    match_speakers,
    roster_names,
    strict_roster_names,
)

ALICE = [1.0, 0.0, 0.0, 0.0]
BOB = [0.0, 1.0, 0.0, 0.0]
CAROL = [0.0, 0.0, 1.0, 0.0]
# Cosine with ALICE is 0.6: over the ordinary threshold, under the bar a
# name new to the project has to clear.
ALICE_LOOSELY = [0.6, 0.0, 0.0, 0.8]


def _write_meeting(
    meeting_dir: Path,
    *,
    embeddings: dict[str, list[float]],
    speakers: dict[int, str],
    names: dict[str, str] | None = None,
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
        (meeting_dir / "speakers.json").write_text(
            json.dumps({"schema_version": 1, "assignments": names}), encoding="utf-8"
        )
    return segments


def _write_roster(project_dir: Path, mode: str, names: list[str]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "roster.json").write_text(
        json.dumps({"schema_version": 1, "mode": mode, "names": names}), encoding="utf-8"
    )


def _assignments(meeting_dir: Path) -> dict[str, str]:
    path = meeting_dir / "speakers.json"
    if not path.is_file():
        return {}
    data: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    return data["assignments"]


def _vault_with_alice_elsewhere(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    _write_meeting(
        vault / "OTHER" / "260801 - Elsewhere",
        embeddings={"Speaker 1": ALICE, "Speaker 2": CAROL},
        speakers={0: "Speaker 1", 1: "Speaker 2"},
        names={"0": "Alice", "1": "Carol"},
    )
    return vault


# -- reading the roster ----------------------------------------------------------


def test_only_a_strict_roster_narrows_the_names(tmp_path: Path) -> None:
    project = tmp_path / "ACME"
    assert strict_roster_names(project) is None
    assert roster_names(project) == frozenset()

    _write_roster(project, "open", ["Alice", " Bob "])
    assert strict_roster_names(project) is None
    assert roster_names(project) == frozenset({"alice", "bob"})

    _write_roster(project, "roster", ["Alice", " Bob ", "", 7])  # type: ignore[list-item]
    assert strict_roster_names(project) == {"alice": "Alice", "bob": "Bob"}

    (project / "roster.json").write_text("{not json", encoding="utf-8")
    assert strict_roster_names(project) is None


# -- who a meeting may be named from ----------------------------------------------


def test_a_voice_named_in_another_project_is_known_here_as_a_newcomer(tmp_path: Path) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    new_meeting = vault / "ACME" / "260901 - First"
    new_meeting.mkdir(parents=True)

    known = collect_known_voices(new_meeting)

    assert known.voiceprints == {"Alice": [ALICE], "Carol": [CAROL]}
    assert known.newcomers == frozenset({"Alice", "Carol"})


def test_a_name_is_at_home_once_the_project_has_heard_it_or_lists_it(tmp_path: Path) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    acme = vault / "ACME"
    _write_meeting(
        acme / "260830 - Earlier",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )
    _write_roster(acme, "open", ["carol"])
    new_meeting = acme / "260901 - Next"
    new_meeting.mkdir()

    known = collect_known_voices(new_meeting)

    # Both projects' samples stand behind Alice; Carol is expected here by
    # the (open) roster. Nobody is a newcomer.
    assert known.voiceprints == {"Alice": [ALICE, ALICE], "Carol": [CAROL]}
    assert known.newcomers == frozenset()


def test_a_strict_roster_bounds_the_names_and_borrows_the_samples(tmp_path: Path) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    acme = vault / "ACME"
    _write_meeting(
        acme / "260830 - Earlier",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )
    _write_roster(acme, "roster", ["ALICE"])
    new_meeting = acme / "260901 - Next"
    new_meeting.mkdir()

    known = collect_known_voices(new_meeting)

    # Bob was heard in this very project but is not on its roster; Carol is
    # nobody here. Alice's sample comes from the other project, under the
    # roster's own spelling, and she is at home.
    assert known.voiceprints == {"ALICE": [ALICE]}
    assert known.newcomers == frozenset()


def test_this_meetings_own_voices_are_not_its_references(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    meeting = vault / "ACME" / "260901 - Only"
    _write_meeting(
        meeting,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )
    # The same folder name in another project is a different meeting.
    _write_meeting(
        vault / "OTHER" / "260901 - Only",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )

    assert collect_known_voices(meeting).voiceprints == {"Bob": [BOB]}


# -- the newcomer bar ---------------------------------------------------------------


def test_a_newcomer_needs_a_strong_match_on_a_substantial_voice() -> None:
    voiceprints = {"Alice": [ALICE]}
    newcomers = frozenset({"Alice"})
    assert 0.5 < 0.6 < NEWCOMER_MIN_SIMILARITY

    def match(vector: list[float], *, speech: float, newcomer: bool) -> dict[str, str]:
        return match_speakers(
            {"Speaker 1": vector},
            voiceprints,
            threshold=0.5,
            newcomers=newcomers if newcomer else frozenset(),
            speech_sec={"Speaker 1": speech},
        )

    # At home, a loose match on a scrap of speech is enough, as always.
    assert match(ALICE_LOOSELY, speech=2.0, newcomer=False) == {"Speaker 1": "Alice"}
    # A newcomer is not named on a loose match...
    assert match(ALICE_LOOSELY, speech=600.0, newcomer=True) == {}
    # ...nor on a scrap of speech, however well it matches...
    assert match(ALICE, speech=MIN_EXEMPLAR_SPEECH_SEC - 1.0, newcomer=True) == {}
    # ...only when the voice is substantial and plainly theirs.
    assert match(ALICE, speech=MIN_EXEMPLAR_SPEECH_SEC, newcomer=True) == {"Speaker 1": "Alice"}


def test_a_lowered_threshold_never_lowers_the_newcomer_bar() -> None:
    """A strict roster's lower threshold is for the roster's own names."""
    matches = match_speakers(
        {"Speaker 1": ALICE_LOOSELY},
        {"Alice": [ALICE]},
        threshold=0.4,
        newcomers=frozenset({"Alice"}),
        speech_sec={"Speaker 1": 600.0},
    )

    assert matches == {}


def test_segments_face_the_same_bar_for_a_newcomer() -> None:
    segments = [
        {"id": seg_id, "start": seg_id * 5.0, "end": seg_id * 5.0 + 5.0, "speaker": "Speaker 1"}
        for seg_id in range(3)
    ]
    loose = {str(seg_id): ALICE_LOOSELY for seg_id in range(3)}
    exact = {str(seg_id): ALICE for seg_id in range(3)}
    voiceprints = {"Alice": [ALICE]}
    newcomers = frozenset({"Alice"})

    assert identify_segments(segments, loose, voiceprints, threshold=0.5) == {
        "0": "Alice",
        "1": "Alice",
        "2": "Alice",
    }
    assert identify_segments(segments, loose, voiceprints, threshold=0.5, newcomers=newcomers) == {}
    assert identify_segments(segments, exact, voiceprints, threshold=0.5, newcomers=newcomers) == {
        "0": "Alice",
        "1": "Alice",
        "2": "Alice",
    }


# -- end to end ----------------------------------------------------------------------


def test_the_first_meeting_of_a_new_project_recognizes_a_voice_from_another(
    tmp_path: Path,
) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    new_meeting = vault / "ACME" / "260901 - First"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE, "Speaker 2": BOB, "Speaker 3": ALICE_LOOSELY},
        speakers={0: "Speaker 1", 1: "Speaker 2", 2: "Speaker 3"},
    )

    result = auto_assign_speakers(
        new_meeting,
        {"Speaker 1": ALICE, "Speaker 2": BOB, "Speaker 3": ALICE_LOOSELY},
        segments,
        threshold=0.5,
    )

    # Alice is recognized; the stranger and the merely similar voice stay
    # unnamed rather than borrowing a name from another project.
    assert (result.named, result.names) == (1, ("Alice",))
    assert _assignments(new_meeting) == {"0": "Alice"}


def test_a_scrap_of_speech_does_not_borrow_a_name_from_another_project(tmp_path: Path) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    new_meeting = vault / "ACME" / "260901 - First"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        segment_sec=3.0,
    )

    result = auto_assign_speakers(new_meeting, {"Speaker 1": ALICE}, segments, threshold=0.5)

    assert result.named == 0
    assert _assignments(new_meeting) == {}


def test_a_strict_project_is_never_given_a_name_off_its_roster(tmp_path: Path) -> None:
    vault = _vault_with_alice_elsewhere(tmp_path)
    acme = vault / "ACME"
    _write_roster(acme, "roster", ["Carol"])
    new_meeting = acme / "260901 - First"
    segments = _write_meeting(
        new_meeting,
        embeddings={"Speaker 1": ALICE, "Speaker 2": CAROL},
        speakers={0: "Speaker 1", 1: "Speaker 2"},
    )

    result = auto_assign_speakers(
        new_meeting, {"Speaker 1": ALICE, "Speaker 2": CAROL}, segments, threshold=0.5
    )

    assert result.names == ("Carol",)
    assert _assignments(new_meeting) == {"1": "Carol"}
