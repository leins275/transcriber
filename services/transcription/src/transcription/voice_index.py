"""The vault's voice memory as a derived, self-invalidating index.

The voice memory is, and stays, the meetings themselves: each meeting's
``speakers.json`` names joined to the voice embeddings in its
``transcript.json`` (``speaker_matching.scan_meeting``). This module only
caches that join in ``<vault_root>/.transcriber/voices.sqlite3`` so that
naming a new meeting -- and showing the operator what the memory holds --
does not re-parse every transcript of the vault each time.

**Invalidation is by construction, not by event.** Every read first
compares each meeting's fingerprint (size and mtime of the two files it was
built from) with the stored one, re-reads exactly the meetings that
changed, and forgets the ones that are gone. Nothing has to notify the
index: an edit made in the app, by hand, by a sync client or by another
machine is picked up by the next read, and a read is never stale.

Like the search index next to it, the file is derived data: a schema
mismatch or a damaged file deletes it and the next read rebuilds it from
the meetings. Deleting it by hand costs one rescan and nothing else.

Quality (which exemplars the memory actually uses) is *not* stored: it is
a property of the vault's exemplars taken together
(``speaker_matching.assess_exemplars``) and is recomputed on every read.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from transcription.people import PeopleRegistry, PeopleResolver, name_key
from transcription.speaker_matching import (
    AssessedExemplar,
    Exemplar,
    NameStat,
    assess_exemplars,
    canonicalize,
    project_dirs,
    scan_meeting_full,
    strict_roster_names,
)

logger = logging.getLogger("transcription")

VOICE_INDEX_SCHEMA_VERSION = 2
VOICE_INDEX_FILENAME = "voices.sqlite3"

_DB_FILE_SUFFIXES = ("", "-wal", "-shm")
_TRANSCRIPT_FILE_NAME = "transcript.json"
_SPEAKERS_FILE_NAME = "speakers.json"
# Folders inside a project that are never meetings (legacy artifact trees
# and the chat store).
_NON_MEETING_DIRS = frozenset({"reports", "action items", "facts", "exports", "chats"})

_SCHEMA = """
CREATE TABLE meetings(
  project TEXT NOT NULL,
  meeting TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL,
  scanned_at INTEGER NOT NULL,
  PRIMARY KEY(project, meeting)
);
CREATE TABLE exemplars(
  project TEXT NOT NULL,
  meeting TEXT NOT NULL,
  label TEXT NOT NULL,
  name TEXT NOT NULL,
  vector BLOB NOT NULL,
  speech_sec REAL NOT NULL,
  votes INTEGER NOT NULL,
  named_segments INTEGER NOT NULL,
  hand_votes INTEGER NOT NULL,
  total_segments INTEGER NOT NULL,
  PRIMARY KEY(project, meeting, label)
);
CREATE TABLE names(
  project TEXT NOT NULL,
  meeting TEXT NOT NULL,
  name TEXT NOT NULL,
  segments INTEGER NOT NULL,
  hand_segments INTEGER NOT NULL,
  speech_sec REAL NOT NULL,
  PRIMARY KEY(project, meeting, name)
);
"""


@dataclass(frozen=True, kw_only=True)
class RefreshStats:
    """What one refresh changed: the ``(project, meeting)`` pairs it re-read
    and how many it forgot."""

    rescanned: tuple[tuple[str, str], ...] = ()
    removed: int = 0


@dataclass(frozen=True, kw_only=True)
class MeetingRow:
    project: str
    meeting: str
    state: str
    scanned_at: int
    exemplars: list[AssessedExemplar] = field(default_factory=list)
    # The names this meeting's labels use, as written (not canonical).
    names: list[NameStat] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """The refreshed vault: what the refresh changed, every transcribed
    meeting with its assessed (canonically named) voice samples and its
    label names, and the resolver that maps any spelling to its person."""

    stats: RefreshStats
    rows: list[MeetingRow]
    resolver: PeopleResolver


def voice_index_path(index_db_path: str | Path) -> Path:
    """The voice index lives next to the search index, wherever that is."""
    return Path(index_db_path).with_name(VOICE_INDEX_FILENAME)


def _pack(vector: list[float]) -> bytes:
    # Doubles, not floats: the index must answer exactly what a direct scan
    # of the JSON answers.
    return struct.pack(f"<{len(vector)}d", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 8}d", blob))


def _stat_part(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "-"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _fingerprint(meeting_dir: Path) -> str:
    """What a meeting's exemplars were built from: both files' size and
    mtime. Either changing -- a rename in the app, a re-diarization, a file
    replaced by a sync client -- makes the stored row stale."""
    return (
        f"{_stat_part(meeting_dir / _TRANSCRIPT_FILE_NAME)}"
        f"|{_stat_part(meeting_dir / _SPEAKERS_FILE_NAME)}"
    )


def _meeting_dirs(project_dir: Path) -> list[Path]:
    try:
        entries = sorted(entry for entry in project_dir.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(".") and entry.name.lower() not in _NON_MEETING_DIRS
    ]


class VoiceIndex:
    """One connection to the voice index. Safe to share between the job
    worker and the status route: every operation holds ``_lock``."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = self._open_or_recreate()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Not a database at all: the handle must be released before the
            # caller can delete the file (Windows refuses otherwise).
            conn.close()
            raise
        conn.row_factory = sqlite3.Row
        return conn

    def _open_or_recreate(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            if int(version) == VOICE_INDEX_SCHEMA_VERSION:
                return conn
            has_tables = conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            if int(version) != 0 or has_tables:
                raise sqlite3.DatabaseError("voice index schema changed")
        except sqlite3.DatabaseError:
            logger.info(
                "voice index is stale or unreadable; rebuilding from the meetings",
                extra={"event": "voice_index_recreated"},
            )
            if conn is not None:
                conn.close()
            for suffix in _DB_FILE_SUFFIXES:
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)
            conn = self._connect()
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {VOICE_INDEX_SCHEMA_VERSION}")
        return conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- refresh ---------------------------------------------------------

    def _forget_locked(self, project: str, meeting: str) -> None:
        self._conn.execute(
            "DELETE FROM exemplars WHERE project = ? AND meeting = ?", (project, meeting)
        )
        self._conn.execute(
            "DELETE FROM names WHERE project = ? AND meeting = ?", (project, meeting)
        )
        self._conn.execute(
            "DELETE FROM meetings WHERE project = ? AND meeting = ?", (project, meeting)
        )

    def _refresh_locked(self, vault_root: Path) -> RefreshStats:
        live: dict[tuple[str, str], Path] = {}
        for project_dir in project_dirs(vault_root):
            for meeting_dir in _meeting_dirs(project_dir):
                if (meeting_dir / _TRANSCRIPT_FILE_NAME).is_file():
                    live[(project_dir.name, meeting_dir.name)] = meeting_dir
        stored = {
            (str(row["project"]), str(row["meeting"])): str(row["fingerprint"])
            for row in self._conn.execute("SELECT project, meeting, fingerprint FROM meetings")
        }

        rescanned: list[tuple[str, str]] = []
        for key, meeting_dir in live.items():
            # Fingerprint first, scan second: a file that changes mid-scan
            # leaves a row whose fingerprint is already out of date, so the
            # next refresh reads it again.
            fingerprint = _fingerprint(meeting_dir)
            if stored.get(key) == fingerprint:
                continue
            project, meeting = key
            scan = scan_meeting_full(meeting_dir)
            state, exemplars = scan.state, scan.exemplars
            with self._conn:
                self._forget_locked(project, meeting)
                self._conn.execute(
                    "INSERT INTO meetings(project, meeting, fingerprint, state, scanned_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (project, meeting, fingerprint, state, int(time.time())),
                )
                self._conn.executemany(
                    "INSERT INTO exemplars(project, meeting, label, name, vector, speech_sec,"
                    " votes, named_segments, hand_votes, total_segments)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            project,
                            meeting,
                            e.label,
                            e.name,
                            _pack(e.vector),
                            e.speech_sec,
                            e.votes,
                            e.named_segments,
                            e.hand_votes,
                            e.total_segments,
                        )
                        for e in exemplars
                    ],
                )
                self._conn.executemany(
                    "INSERT INTO names(project, meeting, name, segments, hand_segments,"
                    " speech_sec) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (project, meeting, n.name, n.segments, n.hand_segments, n.speech_sec)
                        for n in scan.names
                    ],
                )
            rescanned.append(key)

        gone = [key for key in stored if key not in live]
        if gone:
            with self._conn:
                for project, meeting in gone:
                    self._forget_locked(project, meeting)
        if rescanned or gone:
            logger.info(
                "voice index refreshed",
                extra={
                    "event": "voice_index_refreshed",
                    "rescanned": len(rescanned),
                    "removed": len(gone),
                },
            )
        return RefreshStats(rescanned=tuple(rescanned), removed=len(gone))

    def _exemplars_locked(self) -> list[Exemplar]:
        rows = self._conn.execute(
            "SELECT project, meeting, label, name, vector, speech_sec, votes, named_segments,"
            " hand_votes, total_segments FROM exemplars ORDER BY project, meeting, label"
        ).fetchall()
        return [
            Exemplar(
                project=str(row["project"]),
                meeting=str(row["meeting"]),
                label=str(row["label"]),
                name=str(row["name"]),
                vector=_unpack(row["vector"]),
                speech_sec=float(row["speech_sec"]),
                votes=int(row["votes"]),
                named_segments=int(row["named_segments"]),
                hand_votes=int(row["hand_votes"]),
                total_segments=int(row["total_segments"]),
            )
            for row in rows
        ]

    def refresh(self, vault_root: Path) -> RefreshStats:
        """Bring the index in line with the vault's folders."""
        with self._lock:
            return self._refresh_locked(vault_root)

    def vault_exemplars(self, vault_root: Path) -> list[Exemplar]:
        """Every exemplar of the vault, current as of this call."""
        with self._lock:
            self._refresh_locked(vault_root)
            return self._exemplars_locked()

    def snapshot(self, vault_root: Path) -> Snapshot:
        """The refreshed vault, meeting by meeting, with each voice sample's
        verdict and each meeting's label names -- what the status view and
        the speakers database are built from."""
        with self._lock:
            stats = self._refresh_locked(vault_root)
            raw = self._exemplars_locked()
            meeting_rows = self._conn.execute(
                "SELECT project, meeting, state, scanned_at FROM meetings"
            ).fetchall()
            name_rows = self._conn.execute(
                "SELECT project, meeting, name, segments, hand_segments, speech_sec FROM names"
            ).fetchall()

        names: dict[tuple[str, str], list[NameStat]] = {}
        observed: dict[str, float] = {}
        for row in name_rows:
            stat = NameStat(
                name=str(row["name"]),
                segments=int(row["segments"]),
                hand_segments=int(row["hand_segments"]),
                speech_sec=float(row["speech_sec"]),
            )
            names.setdefault((str(row["project"]), str(row["meeting"])), []).append(stat)
            observed[stat.name] = observed.get(stat.name, 0.0) + stat.segments
        resolver = PeopleRegistry.load(vault_root).resolver(observed)

        by_meeting: dict[tuple[str, str], list[AssessedExemplar]] = {}
        for item in assess_exemplars(canonicalize(raw, resolver)):
            key = (item.exemplar.project, item.exemplar.meeting)
            by_meeting.setdefault(key, []).append(item)
        return Snapshot(
            stats=stats,
            rows=[
                MeetingRow(
                    project=str(row["project"]),
                    meeting=str(row["meeting"]),
                    state=str(row["state"]),
                    scanned_at=int(row["scanned_at"]),
                    exemplars=by_meeting.get((str(row["project"]), str(row["meeting"])), []),
                    names=names.get((str(row["project"]), str(row["meeting"])), []),
                )
                for row in meeting_rows
            ],
            resolver=resolver,
        )


def voice_memory_status(index: VoiceIndex, vault_root: Path, project: str) -> dict[str, Any]:
    """The voice memory as one project sees it, shaped for
    ``GET /v1/voices/status``.

    ``voices`` is who a meeting of this project can be named for: one row
    per name, with how many samples recognition uses (``here`` of them from
    this project, the rest from ``other_projects``) and how many are set
    aside. In a strict-roster project that is the roster, name by name --
    including names nobody has a sample for yet. ``meetings`` says, for
    every meeting folder of the project, what it contributes and -- when
    nothing -- why. ``rescanned`` names the project's meetings this very
    call had to re-read, so the view can show the invalidation happening
    instead of hiding it.
    """
    project_dir = vault_root / project
    snapshot = index.snapshot(vault_root)
    stats, rows = snapshot.stats, snapshot.rows
    allowed = strict_roster_names(project_dir, snapshot.resolver)

    voices: dict[str, dict[str, Any]] = {}
    if allowed is not None:
        for key, spelling in allowed.items():
            voices[key] = _empty_voice(spelling)
    for row in rows:
        for item in row.exemplars:
            key = name_key(item.exemplar.name)
            if allowed is not None and key not in allowed:
                continue
            voice = voices.setdefault(key, _empty_voice(item.exemplar.name))
            if item.quality != "ok":
                voice["set_aside"] += 1
                continue
            voice["samples"] += 1
            voice["speech_sec"] += item.exemplar.speech_sec
            if row.project == project:
                voice["here"] += 1
            elif row.project not in voice["other_projects"]:
                voice["other_projects"].append(row.project)
    for voice in voices.values():
        voice["other_projects"].sort()

    own_rows = {row.meeting: row for row in rows if row.project == project}
    meetings: list[dict[str, Any]] = []
    for meeting_dir in sorted(_meeting_dirs(project_dir), reverse=True):
        entry = own_rows.get(meeting_dir.name)
        if entry is None:
            meetings.append({"name": meeting_dir.name, "state": "no_transcript", "voices": []})
            continue
        meetings.append(
            {
                "name": meeting_dir.name,
                "state": entry.state,
                "scanned_at": entry.scanned_at,
                "voices": [
                    {
                        "label": item.exemplar.label,
                        "name": item.exemplar.name,
                        "speech_sec": item.exemplar.speech_sec,
                        "quality": item.quality,
                        "conflicts_with": item.conflicts_with,
                    }
                    for item in sorted(entry.exemplars, key=lambda i: -i.exemplar.speech_sec)
                ],
            }
        )

    return {
        "project": project,
        "roster_only": allowed is not None,
        "updated_at": max((row.scanned_at for row in own_rows.values()), default=None),
        "rescanned": [meeting for owner, meeting in stats.rescanned if owner == project],
        "rescanned_elsewhere": sum(1 for owner, _meeting in stats.rescanned if owner != project),
        "voices": sorted(
            voices.values(),
            # People this project has heard itself first, then people known
            # only from other projects, then names without a usable sample.
            key=lambda v: (-int(v["here"] > 0), -int(v["samples"] > 0), str(v["name"]).casefold()),
        ),
        "meetings": meetings,
    }


def _empty_voice(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "samples": 0,
        "here": 0,
        "other_projects": [],
        "speech_sec": 0.0,
        "set_aside": 0,
    }
