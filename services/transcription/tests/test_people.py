"""The speakers database: the registry (`people.py`), how it changes whose
voice a label is (`speaker_matching`), the derived views (`people_view.py`)
and the HTTP surface (`api/people_routes.py`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ServiceError
from transcription.people import (
    PEOPLE_FILE_NAME,
    PeopleRegistry,
    delete_from_registry,
    edit_registry,
)
from transcription.people_view import MAX_SEGMENTS_PER_MEETING, people_list, person_detail
from transcription.speaker_matching import (
    assess_exemplars,
    auto_assign_speakers,
    canonicalize,
    collect_known_voices,
    people_resolver,
    scan_meeting_full,
    scan_vault,
)
from transcription.voice_index import VOICE_INDEX_FILENAME, VoiceIndex, voice_memory_status

AUTH = {"Authorization": "Bearer test-token"}

ARTUR = [1.0, 0.0, 0.0, 0.0]
BORIS = [0.0, 1.0, 0.0, 0.0]


def _write_meeting(
    meeting_dir: Path,
    *,
    speakers: dict[int, str | None],
    embeddings: dict[str, list[float]] | None = None,
    names: dict[str, str] | None = None,
    auto: dict[str, str] | None = None,
    segment_sec: float = 20.0,
) -> list[dict[str, object]]:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    segments: list[dict[str, object]] = []
    for seg_id, label in speakers.items():
        segment: dict[str, object] = {
            "id": seg_id,
            "start": seg_id * segment_sec,
            "end": seg_id * segment_sec + segment_sec,
            "text": f" line {seg_id} ",
        }
        if label is not None:
            segment["speaker"] = label
        segments.append(segment)
    doc: dict[str, object] = {"schema_version": 1, "text": "hi", "segments": segments}
    if embeddings is not None:
        doc["diarization"] = {"status": "succeeded", "speaker_embeddings": embeddings}
    (meeting_dir / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    if names is not None:
        body: dict[str, object] = {"schema_version": 1, "assignments": names}
        if auto:
            body["auto"] = auto
        (meeting_dir / "speakers.json").write_text(json.dumps(body), encoding="utf-8")
    return segments


def _write_roster(project_dir: Path, mode: str, names: list[str]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "roster.json").write_text(
        json.dumps({"schema_version": 1, "mode": mode, "names": names}), encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Artur is "Артур" in one project and "Artur" in another; Boris is in
    one meeting; an undiarized meeting was labelled by hand."""
    root = tmp_path / "vault"
    _write_meeting(
        root / "TBOT" / "260901 - Sync",
        speakers={0: "Speaker 1", 1: "Speaker 2", 2: "Speaker 1"},
        embeddings={"Speaker 1": ARTUR, "Speaker 2": BORIS},
        names={"0": "Артур", "1": "Boris", "2": "Артур"},
    )
    _write_meeting(
        root / "ELS" / "260724 - KT",
        speakers={0: "Speaker 1", 1: "Speaker 1"},
        embeddings={"Speaker 1": ARTUR},
        names={"0": "Artur", "1": "Artur"},
        auto={"1": "Artur"},
    )
    _write_meeting(
        root / "ELS" / "260601 - Undiarized",
        speakers={0: None, 1: None},
        names={"0": "artur", "1": "Speaker 1"},
    )
    return root


@pytest.fixture
def index(vault: Path) -> Iterator[VoiceIndex]:
    opened = VoiceIndex(vault / ".transcriber" / VOICE_INDEX_FILENAME)
    yield opened
    opened.close()


# -- the registry -----------------------------------------------------------------


def test_an_absent_or_broken_file_is_an_empty_database(tmp_path: Path) -> None:
    assert PeopleRegistry.load(tmp_path).people == []
    (tmp_path / PEOPLE_FILE_NAME).write_text("{not json", encoding="utf-8")
    assert PeopleRegistry.load(tmp_path).people == []


def test_people_are_created_edited_and_stored_in_name_order(tmp_path: Path) -> None:
    edit_registry(tmp_path, "  Boris   Petrov ", bio=" CTO ")
    # A repeated spelling and the person's own name are not aliases.
    edit_registry(tmp_path, "Anna", aliases=["Anya", "anya", "Anna", "Аня"])

    stored = json.loads((tmp_path / PEOPLE_FILE_NAME).read_text(encoding="utf-8"))
    assert stored == {
        "schema_version": 1,
        "people": [
            {"name": "Anna", "aliases": ["Anya", "Аня"], "bio": ""},
            {"name": "Boris Petrov", "aliases": [], "bio": "CTO"},
        ],
    }
    registry = PeopleRegistry.load(tmp_path)
    assert registry.find("ANYA") is registry.find("anna")
    assert registry.find("nobody") is None


def test_a_field_left_out_is_left_alone(tmp_path: Path) -> None:
    edit_registry(tmp_path, "Anna", aliases=["Anya"], bio="PM")

    person = edit_registry(tmp_path, "anya", bio="Product manager")

    assert (person.name, person.aliases, person.bio) == ("Anna", ("Anya",), "Product manager")


def test_a_rename_keeps_the_old_name_as_an_alias(tmp_path: Path) -> None:
    edit_registry(tmp_path, "Danya", aliases=["Даня"])

    person = edit_registry(tmp_path, "Danya", new_name="Danil Savchuk")

    assert (person.name, person.aliases) == ("Danil Savchuk", ("Даня", "Danya"))
    # Labels written under the old name still find the person.
    assert PeopleRegistry.load(tmp_path).find("danya") == person


def test_adding_another_persons_name_as_an_alias_merges_them(tmp_path: Path) -> None:
    edit_registry(tmp_path, "Артур", bio="")
    edit_registry(tmp_path, "Artur", aliases=["Art"], bio="Backend lead")

    person = edit_registry(tmp_path, "Артур", aliases=["Artur"])

    assert (person.name, person.aliases, person.bio) == ("Артур", ("Artur", "Art"), "Backend lead")
    assert [p.name for p in PeopleRegistry.load(tmp_path).people] == ["Артур"]


def test_invalid_edits_are_refused_and_change_nothing(tmp_path: Path) -> None:
    edit_registry(tmp_path, "Anna")
    edit_registry(tmp_path, "Boris")
    before = (tmp_path / PEOPLE_FILE_NAME).read_bytes()

    for kwargs in (
        {"name": "   "},
        {"name": "x" * 81},
        {"name": "Anna", "new_name": "boris"},
        {"name": "Anna", "new_name": " "},
        {"name": "Anna", "aliases": ["ok", ""]},
        {"name": "Anna", "bio": "x" * 8001},
    ):
        with pytest.raises(ServiceError):
            edit_registry(tmp_path, **kwargs)  # type: ignore[arg-type]

    assert (tmp_path / PEOPLE_FILE_NAME).read_bytes() == before


def test_deleting_removes_the_entry_only(tmp_path: Path) -> None:
    edit_registry(tmp_path, "Anna", aliases=["Anya"])

    assert delete_from_registry(tmp_path, "anya") is True
    assert delete_from_registry(tmp_path, "anya") is False
    assert PeopleRegistry.load(tmp_path).people == []


def test_a_name_belongs_to_one_person_even_in_a_hand_edited_file(tmp_path: Path) -> None:
    (tmp_path / PEOPLE_FILE_NAME).write_text(
        json.dumps(
            {
                "people": [
                    {"name": "Anna", "aliases": ["Anya"]},
                    {"name": "Boris", "aliases": ["anya", "Bob"]},
                    {"name": "ANNA"},
                    "junk",
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = PeopleRegistry.load(tmp_path)

    assert [(p.name, p.aliases) for p in registry.people] == [
        ("Anna", ("Anya",)),
        ("Boris", ("Bob",)),
    ]


def test_unregistered_spellings_of_one_name_are_one_person() -> None:
    resolver = PeopleRegistry().resolver({"Иван": 40, "ИВАН": 12, "иван ": 1})

    assert {resolver("ИВАН"), resolver("иван"), resolver(" Иван ")} == {"Иван"}
    assert resolver("Somebody  New") == "Somebody New"
    assert resolver.knows("иван") and not resolver.knows("Somebody New")


# -- whose voice a label is ---------------------------------------------------------


def test_the_scan_reports_names_for_undiarized_meetings_too(vault: Path) -> None:
    scan = scan_meeting_full(vault / "ELS" / "260601 - Undiarized")

    assert scan.state == "no_voices" and scan.exemplars == []
    # The generic label is not a name.
    assert [(n.name, n.segments, n.hand_segments, n.speech_sec) for n in scan.names] == [
        ("artur", 1, 1, 20.0)
    ]

    diarized = scan_meeting_full(vault / "ELS" / "260724 - KT")
    assert [(n.name, n.segments, n.hand_segments) for n in diarized.names] == [("Artur", 2, 1)]


def test_two_spellings_are_two_people_until_registered_as_one(vault: Path) -> None:
    new_meeting = vault / "NEW" / "261001 - First"
    new_meeting.mkdir(parents=True)

    # One voice under two names, one sample each: nothing says which name
    # is right, so both are set aside as a labelling conflict and the voice
    # is not recognized at all.
    before = collect_known_voices(new_meeting)
    assert set(before.voiceprints) == {"Boris"}
    raw = scan_vault(vault)
    assessed = assess_exemplars(canonicalize(raw, people_resolver(vault, raw)))
    assert {(a.exemplar.name, a.quality, a.conflicts_with) for a in assessed} == {
        ("Артур", "conflict", "Artur"),
        ("Artur", "conflict", "Артур"),
        ("Boris", "ok", None),
    }

    edit_registry(vault, "Артур", aliases=["Artur"])

    after = collect_known_voices(new_meeting)
    assert after.voiceprints == {"Артур": [ARTUR, ARTUR], "Boris": [BORIS]}


def test_a_recognized_voice_is_written_under_the_canonical_name(vault: Path) -> None:
    edit_registry(vault, "Artur", new_name="Artur Petrov", aliases=["Артур"])
    new_meeting = vault / "NEW" / "261001 - First"
    segments = _write_meeting(
        new_meeting, speakers={0: "Speaker 1"}, embeddings={"Speaker 1": ARTUR}
    )

    result = auto_assign_speakers(new_meeting, {"Speaker 1": ARTUR}, segments, threshold=0.5)

    assert result.names == ("Artur Petrov",)


def test_a_strict_roster_entry_stands_for_the_person_under_any_name(vault: Path) -> None:
    edit_registry(vault, "Artur Petrov", aliases=["Artur", "Артур"])
    _write_roster(vault / "NEW", "roster", ["Артур"])
    new_meeting = vault / "NEW" / "261001 - First"
    new_meeting.mkdir(parents=True)

    known = collect_known_voices(new_meeting)

    # Both projects' samples, offered under the roster's own spelling; Boris
    # is not on the roster.
    assert known.voiceprints == {"Артур": [ARTUR, ARTUR]}
    assert known.newcomers == frozenset()

    status_index = VoiceIndex(vault / ".transcriber" / "v.sqlite3")
    try:
        status = voice_memory_status(status_index, vault, "NEW")
    finally:
        status_index.close()
    assert [(v["name"], v["samples"]) for v in status["voices"]] == [("Артур", 2)]


# -- the views ------------------------------------------------------------------------


def test_the_table_lists_everybody_labelled_and_registered(vault: Path, index: VoiceIndex) -> None:
    edit_registry(vault, "Artur Petrov", aliases=["Artur", "Артур"], bio="Backend lead")
    edit_registry(vault, "Zoe", bio="Not in any meeting yet")
    _write_roster(vault / "MP", "open", ["Zoe", "artur"])

    people = {row["name"]: row for row in people_list(index, vault)["people"]}

    assert list(people) == ["Artur Petrov", "Boris", "Zoe"]
    artur = people["Artur Petrov"]
    assert artur["registered"] is True and artur["bio"] == "Backend lead"
    # Registered aliases first, then the other spellings labels use.
    assert artur["aliases"] == ["Artur", "Артур", "artur"]
    # Two labelled projects, plus the one whose roster lists him.
    assert artur["projects"] == ["ELS", "MP", "TBOT"]
    assert (artur["meetings"], artur["labelled_segments"], artur["hand_segments"]) == (3, 5, 4)
    assert artur["speech_sec"] == 100.0
    assert (artur["voice_samples"], artur["voice_set_aside"]) == (2, 0)
    assert people["Boris"]["registered"] is False
    assert (people["Boris"]["meetings"], people["Boris"]["voice_samples"]) == (1, 1)
    assert people["Zoe"]["projects"] == ["MP"] and people["Zoe"]["meetings"] == 0


def test_a_persons_page_shows_their_projects_and_hand_labelled_speech(
    vault: Path, index: VoiceIndex
) -> None:
    edit_registry(vault, "Artur Petrov", aliases=["Artur", "Артур"], bio="Backend lead")
    _write_roster(vault / "TBOT", "roster", ["Артур"])
    _write_roster(vault / "MP", "open", ["Artur"])

    detail = person_detail(index, vault, "артур")

    assert (detail["name"], detail["registered"], detail["bio"]) == (
        "Artur Petrov",
        True,
        "Backend lead",
    )
    assert detail["projects"] == [
        {"project": "ELS", "meetings": 2, "speech_sec": 60.0, "in_roster": False},
        {"project": "MP", "meetings": 0, "speech_sec": 0.0, "in_roster": True},
        {"project": "TBOT", "meetings": 1, "speech_sec": 40.0, "in_roster": True},
    ]
    assert detail["voice"] == {"samples": 2, "set_aside": 0, "speech_sec": 80.0}
    # Newest first.
    assert [m["meeting_dir"] for m in detail["meetings"]] == [
        "TBOT/260901 - Sync",
        "ELS/260724 - KT",
        "ELS/260601 - Undiarized",
    ]
    sync, kt, undiarized = detail["meetings"]
    assert sync["segments"] == [
        {"id": 0, "start": 0.0, "end": 20.0, "text": "line 0"},
        {"id": 2, "start": 40.0, "end": 60.0, "text": "line 2"},
    ]
    assert (sync["voice_quality"], sync["segments_truncated"]) == ("ok", False)
    # Segment 1 of the KT meeting was named by the machine: it counts as
    # labelled speech but is not shown as the operator's labelling.
    assert (kt["labelled_segments"], kt["hand_segments"]) == (2, 1)
    assert [segment["id"] for segment in kt["segments"]] == [0]
    assert undiarized["voice_quality"] is None
    assert [segment["id"] for segment in undiarized["segments"]] == [0]


def test_a_long_meeting_shows_its_first_segments_and_says_so(
    vault: Path, index: VoiceIndex
) -> None:
    count = MAX_SEGMENTS_PER_MEETING + 5
    _write_meeting(
        vault / "TBOT" / "260905 - Long",
        speakers=dict.fromkeys(range(count), "Speaker 1"),
        embeddings={"Speaker 1": BORIS},
        names={str(seg_id): "Boris" for seg_id in range(count)},
    )

    detail = person_detail(index, vault, "Boris")

    long_meeting = detail["meetings"][0]
    assert long_meeting["meeting"] == "260905 - Long"
    assert len(long_meeting["segments"]) == MAX_SEGMENTS_PER_MEETING
    assert long_meeting["segments_truncated"] is True
    assert long_meeting["hand_segments"] == count


def test_an_unregistered_person_has_a_page_and_a_stranger_does_not(
    vault: Path, index: VoiceIndex
) -> None:
    from transcription.people import PersonNotFoundError

    detail = person_detail(index, vault, "boris")
    assert (detail["name"], detail["registered"], detail["aliases"]) == ("Boris", False, [])

    with pytest.raises(PersonNotFoundError):
        person_detail(index, vault, "Nobody")


# -- HTTP -------------------------------------------------------------------------------


@pytest.fixture
def config(tmp_app_dir: Path, vault: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir), str(vault)),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        index_db_path=str(vault / ".transcriber" / "index.sqlite3"),
        vault_root=str(vault),
        token="test-token",  # noqa: S106 -- test fixture
    )


def test_the_people_routes_round_trip(config: Config, vault: Path) -> None:
    with TestClient(create_app(config)) as client:
        assert client.get("/v1/people").status_code == 401

        listed = client.get("/v1/people", headers=AUTH)
        assert listed.status_code == 200
        assert {row["name"]: row["registered"] for row in listed.json()["people"]} == {
            "Artur": False,
            "Boris": False,
            "Артур": False,
        }

        saved = client.put(
            "/v1/people",
            json={"name": "Артур", "new_name": "Artur Petrov", "aliases": ["Artur"], "bio": "Lead"},
            headers=AUTH,
        )
        assert saved.status_code == 200
        # The name being replaced stays one of his names even though the
        # alias list sent along did not mention it.
        assert saved.json() == {
            "name": "Artur Petrov",
            "aliases": ["Artur", "Артур"],
            "bio": "Lead",
            "registered": True,
        }
        names = [row["name"] for row in client.get("/v1/people", headers=AUTH).json()["people"]]
        assert names == ["Artur Petrov", "Boris"]

        detail = client.get("/v1/people/detail", params={"name": "artur"}, headers=AUTH)
        assert detail.status_code == 200
        body = detail.json()
        assert body["name"] == "Artur Petrov" and body["bio"] == "Lead"
        assert [project["project"] for project in body["projects"]] == ["ELS", "TBOT"]
        assert body["meetings"][0]["meeting_dir"] == "TBOT/260901 - Sync"

        assert (
            client.get("/v1/people/detail", params={"name": "Nobody"}, headers=AUTH).status_code
            == 404
        )
        bad = client.put("/v1/people", json={"name": "Boris", "new_name": "artur"}, headers=AUTH)
        assert bad.status_code == 400
        assert bad.json()["error_kind"] == "invalid_request"

        deleted = client.delete("/v1/people", params={"name": "artur"}, headers=AUTH)
        assert deleted.json() == {"deleted": True}
        assert client.delete("/v1/people", params={"name": "artur"}, headers=AUTH).json() == {
            "deleted": False
        }
        # The labels are untouched, so the spellings are back as themselves.
        names = [row["name"] for row in client.get("/v1/people", headers=AUTH).json()["people"]]
        assert names == ["Artur", "Boris", "Артур"]
    assert json.loads((vault / "TBOT" / "260901 - Sync" / "speakers.json").read_text("utf-8")) == {
        "schema_version": 1,
        "assignments": {"0": "Артур", "1": "Boris", "2": "Артур"},
    }
