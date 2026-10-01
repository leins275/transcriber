"""The speakers database as the app shows it: the table and one person's page.

The registry (``people.py``) only knows names, aliases and bios. Everything
else on these views is derived, on every request, from the voice index's
snapshot of the vault (``voice_index.py``): which meetings carry a person's
name, how much of that was labelled by hand, and what the voice memory
makes of their samples. Nothing here is stored.

A person exists on these views when they are registered *or* labelled
anywhere; the latter are listed as ``registered: false`` so the operator
can see who the vault already talks about before curating anybody.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from transcription.people import PeopleRegistry, PersonNotFoundError, name_key
from transcription.speaker_matching import (
    _is_generic,
    _load_speakers,
    _read_json_capped,
    _segment_duration,
    project_dirs,
    roster_names,
)
from transcription.voice_index import Snapshot, VoiceIndex

# How many hand-labelled segments of one meeting the person page carries;
# the recording itself has the rest.
MAX_SEGMENTS_PER_MEETING = 20

_TRANSCRIPT_FILE_NAME = "transcript.json"
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
# Shown for a meeting's sample in order of how much the operator can act on
# it: a sample in use outranks any set-aside one.
_QUALITY_RANK = {"ok": 0, "conflict": 1, "unconfirmed": 2, "partial": 3, "short": 4}


@dataclass
class _Tally:
    """One person's totals, accumulated over the vault."""

    spellings: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    meetings: int = 0
    labelled_segments: int = 0
    hand_segments: int = 0
    speech_sec: float = 0.0
    voice_samples: int = 0
    voice_set_aside: int = 0
    voice_speech_sec: float = 0.0


def _tallies(snapshot: Snapshot) -> dict[str, _Tally]:
    """Canonical name -> totals, over every labelled meeting."""
    tallies: dict[str, _Tally] = {}
    for row in snapshot.rows:
        seen_here: set[str] = set()
        for stat in row.names:
            person = snapshot.resolver(stat.name)
            tally = tallies.setdefault(person, _Tally())
            tally.spellings.add(" ".join(stat.name.split()))
            tally.projects.add(row.project)
            tally.labelled_segments += stat.segments
            tally.hand_segments += stat.hand_segments
            tally.speech_sec += stat.speech_sec
            if person not in seen_here:
                seen_here.add(person)
                tally.meetings += 1
        for item in row.exemplars:
            tally = tallies.setdefault(item.exemplar.name, _Tally())
            if item.quality == "ok":
                tally.voice_samples += 1
                tally.voice_speech_sec += item.exemplar.speech_sec
            else:
                tally.voice_set_aside += 1
    return tallies


def _aliases(name: str, registered: tuple[str, ...], spellings: set[str]) -> list[str]:
    """The person's other names: the registered aliases first, then any
    other spelling the labels use that folds to a different string."""
    out: list[str] = []
    seen = {name}
    for alias in (*registered, *sorted(spellings)):
        if alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


def people_list(index: VoiceIndex, vault_root: Path) -> dict[str, Any]:
    """``GET /v1/people``: everybody registered or labelled, by name."""
    snapshot = index.snapshot(vault_root)
    registry = PeopleRegistry.load(vault_root)
    tallies = _tallies(snapshot)
    for person in registry.people:
        tallies.setdefault(person.name, _Tally())
    # Rosters assign people to projects before their first meeting there.
    rosters = {
        project_dir.name: roster_names(project_dir, snapshot.resolver)
        for project_dir in project_dirs(vault_root)
    }
    for name, tally in tallies.items():
        key = name_key(name)
        tally.projects.update(project for project, keys in rosters.items() if key in keys)
    people: list[dict[str, Any]] = []
    for person in registry.people:
        tally = tallies.pop(person.name, _Tally())
        people.append(_row(person.name, person.aliases, person.bio, registered=True, tally=tally))
    for name, tally in tallies.items():
        people.append(_row(name, (), "", registered=False, tally=tally))
    people.sort(key=lambda row: name_key(str(row["name"])))
    return {"people": people}


def _row(
    name: str, aliases: tuple[str, ...], bio: str, *, registered: bool, tally: _Tally
) -> dict[str, Any]:
    return {
        "name": name,
        "aliases": _aliases(name, aliases, tally.spellings),
        "bio": bio,
        "registered": registered,
        "projects": sorted(tally.projects),
        "meetings": tally.meetings,
        "labelled_segments": tally.labelled_segments,
        "hand_segments": tally.hand_segments,
        "speech_sec": tally.speech_sec,
        "voice_samples": tally.voice_samples,
        "voice_set_aside": tally.voice_set_aside,
    }


def _hand_segments(
    meeting_dir: Path, person: str, snapshot: Snapshot
) -> tuple[list[dict[str, Any]], bool]:
    """The segments of one meeting the operator labelled as `person`, in
    time order and capped; the flag says more exist."""
    doc = _read_json_capped(meeting_dir / _TRANSCRIPT_FILE_NAME, _MAX_TRANSCRIPT_BYTES)
    segments = doc.get("segments") if doc else None
    if not isinstance(segments, list):
        return [], False
    assignments, auto = _load_speakers(meeting_dir)
    out: list[dict[str, Any]] = []
    truncated = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("id"))
        name = assignments.get(segment_id)
        if not name or _is_generic(name) or auto.get(segment_id) == name:
            continue
        if snapshot.resolver(name) != person:
            continue
        if len(out) >= MAX_SEGMENTS_PER_MEETING:
            truncated = True
            break
        try:
            start = float(segment.get("start", 0.0))
        except (TypeError, ValueError):
            start = 0.0
        out.append(
            {
                "id": segment.get("id") if isinstance(segment.get("id"), int) else 0,
                "start": start,
                "end": start + _segment_duration(segment),
                "text": str(segment.get("text", "")).strip(),
            }
        )
    return out, truncated


def person_detail(index: VoiceIndex, vault_root: Path, name: str) -> dict[str, Any]:
    """``GET /v1/people/detail``: one person, addressed by any of their
    names. Raises ``PersonNotFoundError`` for a name that is neither
    registered nor used in any label."""
    snapshot = index.snapshot(vault_root)
    registry = PeopleRegistry.load(vault_root)
    registered = registry.find(name)
    if registered is None and not snapshot.resolver.knows(name):
        raise PersonNotFoundError(f"no speaker is called {name.strip()!r}")
    person = registered.name if registered is not None else snapshot.resolver(name)
    tally = _tallies(snapshot).get(person, _Tally())

    projects: dict[str, dict[str, Any]] = {}
    meetings: list[dict[str, Any]] = []
    for row in snapshot.rows:
        stats = [stat for stat in row.names if snapshot.resolver(stat.name) == person]
        if not stats:
            continue
        speech = sum(stat.speech_sec for stat in stats)
        project = projects.setdefault(
            row.project, {"project": row.project, "meetings": 0, "speech_sec": 0.0}
        )
        project["meetings"] += 1
        project["speech_sec"] += speech
        samples = [item for item in row.exemplars if item.exemplar.name == person]
        sample = min(samples, key=lambda item: _QUALITY_RANK[item.quality]) if samples else None
        segments, truncated = _hand_segments(
            vault_root / row.project / row.meeting, person, snapshot
        )
        meetings.append(
            {
                "project": row.project,
                "meeting": row.meeting,
                "meeting_dir": f"{row.project}/{row.meeting}",
                "labelled_segments": sum(stat.segments for stat in stats),
                "hand_segments": sum(stat.hand_segments for stat in stats),
                "speech_sec": speech,
                "voice_quality": sample.quality if sample is not None else None,
                "voice_conflicts_with": sample.conflicts_with if sample is not None else None,
                "segments": segments,
                "segments_truncated": truncated,
            }
        )
    # Newest first: meeting folders start with their date.
    meetings.sort(key=lambda meeting: (meeting["meeting"], meeting["project"]), reverse=True)
    # A project also counts when its roster lists the person, meetings or
    # not: being put on a roster is how somebody is assigned to a project.
    key = name_key(person)
    for project_dir in project_dirs(vault_root):
        on_roster = key in roster_names(project_dir, snapshot.resolver)
        if on_roster:
            projects.setdefault(
                project_dir.name,
                {"project": project_dir.name, "meetings": 0, "speech_sec": 0.0},
            )
        if project_dir.name in projects:
            projects[project_dir.name]["in_roster"] = on_roster
    for project in projects.values():
        project.setdefault("in_roster", False)

    return {
        "name": person,
        "aliases": _aliases(
            person, registered.aliases if registered is not None else (), tally.spellings
        ),
        "bio": registered.bio if registered is not None else "",
        "registered": registered is not None,
        "projects": sorted(projects.values(), key=lambda p: str(p["project"])),
        "voice": {
            "samples": tally.voice_samples,
            "set_aside": tally.voice_set_aside,
            "speech_sec": tally.voice_speech_sec,
        },
        "meetings": meetings,
    }
