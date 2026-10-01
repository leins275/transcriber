"""Tests for the derived voice index (`voice_index.py`).

The index is a cache of what `speaker_matching.scan_meeting` reads from the
meeting folders, so every test states its expectation in terms of the
folders: what the index answers must be what a direct scan answers, however
the folders changed in between.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from transcription import voice_index as voice_index_module
from transcription.speaker_matching import collect_known_voices, scan_vault
from transcription.voice_index import (
    VOICE_INDEX_FILENAME,
    VoiceIndex,
    voice_index_path,
    voice_memory_status,
)

ALICE = [1.0, 0.0, 0.0, 0.0]
BOB = [0.0, 1.0, 0.0, 0.0]
CAROL = [0.0, 0.0, 1.0, 0.0]


def _write_meeting(
    meeting_dir: Path,
    *,
    embeddings: dict[str, list[float]] | None,
    speakers: dict[int, str],
    names: dict[str, str] | None = None,
    auto: dict[str, str] | None = None,
    segment_sec: float = 20.0,
) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    segments = [
        {
            "id": seg_id,
            "start": seg_id * segment_sec,
            "end": seg_id * segment_sec + segment_sec,
            "text": "hi",
            "speaker": label,
        }
        for seg_id, label in speakers.items()
    ]
    doc: dict[str, object] = {"schema_version": 1, "text": "hi", "segments": segments}
    if embeddings is not None:
        doc["diarization"] = {"status": "succeeded", "speaker_embeddings": embeddings}
    (meeting_dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    if names is not None:
        _write_names(meeting_dir, names, auto)


def _write_names(meeting_dir: Path, names: dict[str, str], auto: dict[str, str] | None) -> None:
    body: dict[str, object] = {"schema_version": 1, "assignments": names}
    if auto:
        body["auto"] = auto
    path = meeting_dir / "speakers.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    # A rewrite of the same size within the filesystem's timestamp
    # granularity would be invisible to any stat-based check; real edits are
    # seconds apart, so the test says so explicitly.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


def _write_roster(project_dir: Path, mode: str, names: list[str]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "roster.json").write_text(
        json.dumps({"schema_version": 1, "mode": mode, "names": names}), encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    acme = root / "ACME"
    _write_meeting(
        acme / "260801 - Kickoff",
        embeddings={"Speaker 1": ALICE, "Speaker 2": BOB},
        speakers={0: "Speaker 1", 1: "Speaker 2"},
        names={"0": "Alice", "1": "Bob"},
    )
    _write_meeting(
        acme / "260802 - Unnamed",
        embeddings={"Speaker 1": CAROL},
        speakers={0: "Speaker 1"},
    )
    _write_meeting(acme / "260803 - Undiarized", embeddings=None, speakers={0: "Speaker 1"})
    (acme / "260804 - Empty shell").mkdir()
    # Not meetings and not projects: the chat store, a legacy artifact tree,
    # the index's own home.
    (acme / "chats").mkdir()
    (root / "exports").mkdir()
    (root / ".transcriber").mkdir()
    return root


@pytest.fixture
def index(vault: Path) -> Iterator[VoiceIndex]:
    opened = VoiceIndex(vault / ".transcriber" / VOICE_INDEX_FILENAME)
    yield opened
    opened.close()


def _names(index: VoiceIndex, vault: Path) -> set[str]:
    return {exemplar.name for exemplar in index.vault_exemplars(vault)}


def test_the_index_lives_next_to_the_search_index() -> None:
    assert voice_index_path(Path("vault") / ".transcriber" / "index.sqlite3") == (
        Path("vault") / ".transcriber" / VOICE_INDEX_FILENAME
    )


def test_the_index_answers_exactly_what_a_direct_scan_answers(
    vault: Path, index: VoiceIndex
) -> None:
    assert index.vault_exemplars(vault) == scan_vault(vault)
    assert {(e.project, e.meeting, e.name) for e in index.vault_exemplars(vault)} == {
        ("ACME", "260801 - Kickoff", "Alice"),
        ("ACME", "260801 - Kickoff", "Bob"),
    }

    new_meeting = vault / "ACME" / "260805 - New"
    new_meeting.mkdir()
    assert collect_known_voices(new_meeting, memory=index) == collect_known_voices(new_meeting)
    assert set(collect_known_voices(new_meeting, memory=index).voiceprints) == {"Alice", "Bob"}


def test_an_unchanged_vault_is_not_read_again(
    vault: Path, index: VoiceIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(vault)

    def refuse(_meeting_dir: Path) -> None:
        raise AssertionError("an unchanged meeting must not be re-read")

    monkeypatch.setattr(voice_index_module, "scan_meeting", refuse)

    stats = index.refresh(vault)

    assert stats.rescanned == ()
    assert _names(index, vault) == {"Alice", "Bob"}


def test_renaming_a_speaker_re_reads_only_that_meeting(vault: Path, index: VoiceIndex) -> None:
    index.refresh(vault)

    _write_names(vault / "ACME" / "260801 - Kickoff", {"0": "Alicia", "1": "Bob"}, None)
    stats = index.refresh(vault)

    assert stats.rescanned == (("ACME", "260801 - Kickoff"),)
    assert _names(index, vault) == {"Alicia", "Bob"}


def test_naming_a_voice_for_the_first_time_brings_the_meeting_in(
    vault: Path, index: VoiceIndex
) -> None:
    index.refresh(vault)

    _write_names(vault / "ACME" / "260802 - Unnamed", {"0": "Carol"}, None)

    assert _names(index, vault) == {"Alice", "Bob", "Carol"}


def test_a_deleted_meeting_is_forgotten(vault: Path, index: VoiceIndex) -> None:
    index.refresh(vault)
    kickoff = vault / "ACME" / "260801 - Kickoff"
    for child in kickoff.iterdir():
        child.unlink()
    kickoff.rmdir()

    stats = index.refresh(vault)

    assert stats.removed == 1
    assert index.vault_exemplars(vault) == []


def test_every_project_of_the_vault_is_indexed(vault: Path, index: VoiceIndex) -> None:
    _write_meeting(
        vault / "OTHER" / "260801 - Elsewhere",
        embeddings={"Speaker 1": CAROL},
        speakers={0: "Speaker 1"},
        names={"0": "Carol"},
    )
    _write_meeting(
        vault / "unsorted" / "loose recording",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
    )

    assert {(e.project, e.name) for e in index.vault_exemplars(vault)} == {
        ("ACME", "Alice"),
        ("ACME", "Bob"),
        ("OTHER", "Carol"),
        ("unsorted", "Bob"),
    }


def test_the_index_survives_being_reopened(vault: Path, tmp_path: Path) -> None:
    path = tmp_path / "voices.sqlite3"
    first = VoiceIndex(path)
    first.refresh(vault)
    first.close()

    second = VoiceIndex(path)
    try:
        assert second.refresh(vault).rescanned == ()
        assert second.vault_exemplars(vault) == scan_vault(vault)
    finally:
        second.close()


@pytest.mark.parametrize("damage", ["garbage", "foreign-schema"])
def test_a_damaged_or_foreign_file_is_rebuilt_from_the_meetings(
    vault: Path, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / "voices.sqlite3"
    if damage == "garbage":
        path.write_bytes(b"this is not a database" * 64)
    else:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something_else(x)")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()

    index = VoiceIndex(path)
    try:
        assert index.vault_exemplars(vault) == scan_vault(vault)
    finally:
        index.close()


def test_a_failing_index_falls_back_to_the_folders(vault: Path) -> None:
    class Broken:
        def vault_exemplars(self, _vault_root: Path) -> list[object]:
            raise OSError("disk is gone")

    new_meeting = vault / "ACME" / "260805 - New"
    new_meeting.mkdir()

    known = collect_known_voices(new_meeting, memory=Broken())  # type: ignore[arg-type]

    assert set(known.voiceprints) == {"Alice", "Bob"}


def test_status_says_who_is_known_and_why_a_meeting_contributes_nothing(
    vault: Path, index: VoiceIndex
) -> None:
    status = voice_memory_status(index, vault, "ACME")

    assert status["project"] == "ACME"
    assert status["roster_only"] is False
    assert sorted(status["rescanned"]) == [
        "260801 - Kickoff",
        "260802 - Unnamed",
        "260803 - Undiarized",
    ]
    assert [voice["name"] for voice in status["voices"]] == ["Alice", "Bob"]
    assert all(
        (voice["samples"], voice["here"], voice["other_projects"], voice["set_aside"])
        == (1, 1, [], 0)
        for voice in status["voices"]
    )
    states = {meeting["name"]: meeting["state"] for meeting in status["meetings"]}
    # `chats` is not a meeting and is not listed.
    assert states == {
        "260801 - Kickoff": "named",
        "260802 - Unnamed": "unnamed",
        "260803 - Undiarized": "no_voices",
        "260804 - Empty shell": "no_transcript",
    }
    # Newest first, like every other meeting list.
    assert [meeting["name"] for meeting in status["meetings"]][0] == "260804 - Empty shell"

    again = voice_memory_status(index, vault, "ACME")
    assert again["rescanned"] == []
    assert again["updated_at"] == status["updated_at"]


def test_status_shows_voices_known_from_other_projects(vault: Path, index: VoiceIndex) -> None:
    _write_meeting(
        vault / "OTHER" / "260801 - Elsewhere",
        embeddings={"Speaker 1": ALICE, "Speaker 2": CAROL},
        speakers={0: "Speaker 1", 1: "Speaker 2"},
        names={"0": "Alice", "1": "Carol"},
    )

    status = voice_memory_status(index, vault, "ACME")

    voices = {voice["name"]: voice for voice in status["voices"]}
    assert (voices["Alice"]["samples"], voices["Alice"]["here"]) == (2, 1)
    assert voices["Alice"]["other_projects"] == ["OTHER"]
    assert (voices["Carol"]["samples"], voices["Carol"]["here"]) == (1, 0)
    assert voices["Carol"]["other_projects"] == ["OTHER"]
    # People heard in this project come before people known only elsewhere.
    assert [voice["name"] for voice in status["voices"]] == ["Alice", "Bob", "Carol"]
    # Only this project's meetings are listed and counted as re-read here.
    assert all("Elsewhere" not in meeting["name"] for meeting in status["meetings"])
    assert status["rescanned_elsewhere"] == 1


def test_a_strict_roster_is_the_whole_list_of_voices(vault: Path, index: VoiceIndex) -> None:
    _write_meeting(
        vault / "OTHER" / "260801 - Elsewhere",
        embeddings={"Speaker 1": CAROL},
        speakers={0: "Speaker 1"},
        names={"0": "CAROL"},
    )
    _write_roster(vault / "ACME", "roster", ["Alice", "Carol", "Dave"])

    status = voice_memory_status(index, vault, "ACME")

    assert status["roster_only"] is True
    voices = {voice["name"]: voice for voice in status["voices"]}
    # Bob is known in this very project but is not on its roster; Carol is
    # known only elsewhere (under another spelling) and is; Dave has no
    # sample anywhere and is still listed, as somebody to be named.
    assert set(voices) == {"Alice", "Carol", "Dave"}
    assert (voices["Carol"]["samples"], voices["Carol"]["other_projects"]) == (1, ["OTHER"])
    assert voices["Dave"]["samples"] == 0 and voices["Dave"]["set_aside"] == 0
    assert [voice["name"] for voice in status["voices"]] == ["Alice", "Carol", "Dave"]


def test_status_shows_the_samples_the_memory_sets_aside(vault: Path, index: VoiceIndex) -> None:
    acme = vault / "ACME"
    # Too little speech to be a reference.
    _write_meeting(
        acme / "260806 - Brief",
        embeddings={"Speaker 1": CAROL},
        speakers={0: "Speaker 1"},
        names={"0": "Carol"},
        segment_sec=3.0,
    )
    # Named by the machine and never touched by the operator.
    _write_meeting(
        acme / "260807 - Auto",
        embeddings={"Speaker 1": BOB},
        speakers={0: "Speaker 1"},
        names={"0": "Bob"},
        auto={"0": "Bob"},
    )
    # Alice's voice under somebody else's name. Alice is known from two
    # meetings, so the odd one out is the mislabelled sample, not her.
    _write_meeting(
        acme / "260805 - Alice again",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Alice"},
    )
    _write_meeting(
        acme / "260808 - Mislabelled",
        embeddings={"Speaker 1": ALICE},
        speakers={0: "Speaker 1"},
        names={"0": "Dmitry"},
    )

    status = voice_memory_status(index, vault, "ACME")

    by_meeting = {meeting["name"]: meeting["voices"] for meeting in status["meetings"]}
    assert by_meeting["260806 - Brief"][0]["quality"] == "short"
    assert by_meeting["260807 - Auto"][0]["quality"] == "unconfirmed"
    mislabelled = by_meeting["260808 - Mislabelled"][0]
    assert (mislabelled["quality"], mislabelled["conflicts_with"]) == ("conflict", "Alice")
    voices = {voice["name"]: voice for voice in status["voices"]}
    assert (voices["Bob"]["samples"], voices["Bob"]["set_aside"]) == (1, 1)
    assert voices["Bob"]["speech_sec"] == 20.0
    assert (voices["Carol"]["samples"], voices["Carol"]["set_aside"]) == (0, 1)
    assert (voices["Dmitry"]["samples"], voices["Dmitry"]["set_aside"]) == (0, 1)
    # People the memory actually knows come first.
    assert [voice["name"] for voice in status["voices"]][:2] == ["Alice", "Bob"]
