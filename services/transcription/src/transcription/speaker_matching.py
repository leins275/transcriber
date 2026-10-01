"""Cross-meeting speaker naming from diarization voice embeddings.

After a diarized transcription lands, its per-speaker voice embeddings are
compared against the voice memory: voices the operator has named *by hand*
in other meetings (each meeting's ``speakers.json`` assignments joined to
its ``transcript.json``'s stored ``diarization.speaker_embeddings``). A
close-enough cosine match pre-fills ``speakers.json`` for the new meeting,
so a returning voice opens already named.

The memory spans the whole vault: a person is the same person in every
project, so a voice named in one project is recognized in all of them, and
a name's samples from every project stand behind it. A project whose
``roster.json`` is in ``roster`` mode is the one exception to "any name":
there, only the names on its roster may be given -- the roster is the
operator's statement of who can be in that project's meetings -- while the
samples behind those names still come from everywhere.

Sharing voices across projects has a price, measured on the operator's
vault: the scraps a diarizer leaves behind (a few seconds of crosstalk
filed as a "speaker") sound alike in every recording, so a name whose
samples are such scraps attracts scraps everywhere. A name is therefore
*at home* in a project when the project's own meetings hold a usable sample
of it or its roster lists it, and is matched as before; any other name is a
*newcomer* there and must clear ``NEWCOMER_MIN_SIMILARITY`` on a voice with
at least ``MIN_EXEMPLAR_SPEECH_SEC`` of speech. A real returning voice does
(the same person scores 0.9 and up across meetings); a scrap does not.

Two layers of recognition, both fed by the same memory:

* **per voice** -- each diarized cluster is matched to at most one known
  name (``match_speakers``);
* **per segment** -- when the caller could embed individual segments, a
  segment whose own voice clearly belongs to somebody else than its cluster
  says is named for that person (``identify_segments``). This is what
  repairs a cluster the diarizer merged two people into.

What counts as memory is deliberately narrow, because a name is only as
good as its source (measured on a real vault -- see ``assess_exemplars``):

* a name this module wrote itself is recorded in ``speakers.json``'s
  ``auto`` map and is *not* evidence: the machine never learns from its own
  guesses, only from what the operator typed or changed;
* a voice whose name covers only a few of its segments was never really
  named -- somebody fixed a couple of lines inside it;
* a voice heard for less than ``MIN_EXEMPLAR_SPEECH_SEC`` is too little
  audio to be a reference;
* a voice that sounds like another person's usual voice is a labelling
  conflict and is set aside until the operator resolves it.

Additive only, by contract: ``speakers.json`` is the operator's file (see
the vault-side comment on ``SPEAKERS_FILE_NAME`` in the app), and this
module never overwrites a name the operator gave -- it fills segments that
have none, segments the app only seeded with a generic ``Speaker N`` label,
and segments it named itself on an earlier pass. Everything here degrades
rather than fails: an unreadable sibling contributes nothing, and the
caller treats any raised error as a job warning.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from transcription.diarization import SPEAKER_LABEL_PREFIX

logger = logging.getLogger("transcription")

_SPEAKERS_FILE_NAME = "speakers.json"
_TRANSCRIPT_FILE_NAME = "transcript.json"
# `<vault root>/<PROJECT>/roster.json`, written by the app
# (`commands/roster.rs`, `vault::ROSTER_FILE_NAME`).
_ROSTER_FILE_NAME = "roster.json"
_ROSTER_STRICT_MODE = "roster"
_MAX_ROSTER_BYTES = 1024 * 1024
# Top-level vault directories that are not projects (mirrors the indexer's
# reserved names; dot-directories are skipped as well).
_NON_PROJECT_DIRS = frozenset({"reports", "action items", "facts", "exports", "chats"})
_SPEAKERS_SCHEMA_VERSION = 1

# Caps mirror the app's readers: a bigger file is something other than what
# it claims to be.
_MAX_SPEAKERS_BYTES = 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024

# What diarization calls a voice it has no name for, derived from the one
# place that mints those labels (``diarization.SPEAKER_LABEL_PREFIX``).
_GENERIC_LABEL = re.compile(rf"^{re.escape(SPEAKER_LABEL_PREFIX)}\d+$")

# A voice heard for less than this is not a reference. Measured on the
# operator's vault (leave-one-out over 29 labelled meetings): a cluster of a
# few seconds carries a near-random embedding that, under the one-name-per-
# meeting rule, can steal a name from the cluster that really owns it -- in
# one project this alone took cluster naming from 18% to 93% of segments.
MIN_EXEMPLAR_SPEECH_SEC = 10.0

# A voice is a labelling conflict when it resembles another person's usual
# voice at least this much...
CONFLICT_MIN_SIMILARITY = 0.8
# ...and by at least this much more than it resembles its own name's.
CONFLICT_MARGIN = 0.1

# What a name new to a project must score to be given there. Between the
# 0.9+ a real returning voice scores across meetings and the 0.5-0.75 that
# unrelated voices and diarization scraps reach against a stranger's samples.
NEWCOMER_MIN_SIMILARITY = 0.8

# Per-segment identification. A segment shorter than this gives an embedding
# too noisy to outvote its cluster (at 1 s the override broke more correct
# names than it repaired; from 2 s it is a net gain).
SEGMENT_MIN_SEC = 2.0
# Longer segments are embedded from their middle: more audio adds cost, not
# certainty.
SEGMENT_MAX_SEC = 10.0
# The best name must beat the runner-up by this much.
SEGMENT_MARGIN = 0.1
# ...and at least this many segments of the same cluster must agree on that
# name: one lone dissenting segment is noise, three are a second person.
SEGMENT_MIN_VOTES = 3

ExemplarQuality = Literal["ok", "unconfirmed", "partial", "short", "conflict"]
MeetingVoiceState = Literal["named", "unnamed", "no_voices"]


@dataclass(frozen=True, kw_only=True)
class Exemplar:
    """One diarized voice of one meeting, as the operator's labels name it.

    Of the cluster's ``total_segments``, ``named_segments`` carry a real
    name and ``votes`` of those carry ``name`` (the majority);
    ``hand_votes`` of the votes were given by the operator rather than by
    this module. ``speech_sec`` is how long the voice speaks in that
    meeting.
    """

    project: str
    meeting: str
    label: str
    name: str
    vector: list[float]
    speech_sec: float
    votes: int
    named_segments: int
    hand_votes: int
    total_segments: int


@dataclass(frozen=True, kw_only=True)
class AssessedExemplar:
    """An exemplar plus whether the memory uses it (``quality == "ok"``)
    and, for a conflict, whose voice it sounds like."""

    exemplar: Exemplar
    quality: ExemplarQuality
    conflicts_with: str | None = None


@dataclass(frozen=True, kw_only=True)
class NamingResult:
    """What one auto-naming pass did to a meeting's ``speakers.json``.

    ``named`` segments gained a name; ``by_segment_voice`` of them were
    named from their own voice against what their cluster said.
    """

    named: int = 0
    by_segment_voice: int = 0
    names: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class KnownVoices:
    """The voices one meeting may be named from.

    ``voiceprints`` maps a name to its sample embeddings; ``newcomers`` are
    the names among them that are not at home in the meeting's project and
    so face the stricter bar.
    """

    voiceprints: dict[str, list[list[float]]]
    newcomers: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.voiceprints)


class VoiceMemory(Protocol):
    """What this module needs from the derived index (``voice_index.py``)."""

    def vault_exemplars(self, vault_root: Path) -> list[Exemplar]: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = math.sumprod(a, b)
    norm_a = math.sqrt(math.sumprod(a, a))
    norm_b = math.sqrt(math.sumprod(b, b))
    if norm_a == 0.0 or norm_b == 0.0 or not math.isfinite(norm_a * norm_b):
        return 0.0
    return dot / (norm_a * norm_b)


def _unit(vector: Sequence[float]) -> list[float] | None:
    """`vector` scaled to length 1, so a cosine is a plain dot product --
    the per-segment pass compares thousands of pairs."""
    norm = math.sqrt(math.sumprod(vector, vector))
    if norm == 0.0 or not math.isfinite(norm):
        return None
    return [x / norm for x in vector]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return math.sumprod(a, b)


def _read_json_capped(path: Path, cap: int) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(name) for key, name in value.items() if isinstance(name, str) and name.strip()
    }


def _load_speakers(meeting_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """``speakers.json`` as ``(assignments, auto)``, both segment-id -> name
    and both empty when absent.

    ``auto`` holds the names this module wrote. A segment is machine-named
    exactly while ``auto[id] == assignments[id]``: the app carries the map
    over untouched when it saves, so a name the operator changes stops
    matching and becomes theirs without anybody having to diff anything.
    """
    data = _read_json_capped(meeting_dir / _SPEAKERS_FILE_NAME, _MAX_SPEAKERS_BYTES)
    if not data:
        return {}, {}
    return _string_map(data.get("assignments")), _string_map(data.get("auto"))


def _load_assignments(meeting_dir: Path) -> dict[str, str]:
    """``speakers.json``'s segment-id -> name map (empty when absent)."""
    return _load_speakers(meeting_dir)[0]


def _label_embeddings(doc: dict[str, Any]) -> dict[str, list[float]]:
    """``diarization.speaker_embeddings`` as clean label -> vector."""
    diarization = doc.get("diarization")
    embeddings = diarization.get("speaker_embeddings") if isinstance(diarization, dict) else None
    if not isinstance(embeddings, dict):
        return {}
    out: dict[str, list[float]] = {}
    for label, vector in embeddings.items():
        if isinstance(vector, list) and vector and all(isinstance(v, int | float) for v in vector):
            out[str(label)] = [float(v) for v in vector]
    return out


def _is_generic(name: str) -> bool:
    """Is this "name" just what diarization calls a voice it cannot name?

    Exactly the form this service writes and nothing else: ``normalize_labels``
    renames every raw pyannote label (``SPEAKER_00``) to
    ``SPEAKER_LABEL_PREFIX`` + a number before a transcript is ever written, so
    ``Speaker 3`` is the only placeholder that can reach a ``speakers.json``.
    Anything else in that file is a string a person typed, and this predicate
    decides which entries ``auto_assign_speakers`` may overwrite -- a looser
    pattern would silently rename someone. ``lib/turns.ts::isGenericSpeakerLabel``
    mirrors it for the UI.
    """
    return _GENERIC_LABEL.match(name.strip()) is not None


def _segment_duration(segment: Mapping[str, Any]) -> float:
    try:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, end - start)


def scan_meeting(meeting_dir: Path) -> tuple[MeetingVoiceState, list[Exemplar]]:
    """One meeting's contribution to the voice memory.

    One exemplar per diarized label whose segments carry a real name, named
    by majority vote (a stray mis-assigned segment must not rename a voice).
    A generic ``Speaker N`` "name" is not a name (the app's transcript
    viewer saves the whole speaker map, seeded labels included), so it casts
    no vote -- otherwise a placeholder would travel across the project as if
    it were a person.

    The state says why a meeting contributes nothing: ``no_voices`` has no
    stored voice embeddings (never diarized), ``unnamed`` has voices but no
    names.
    """
    doc = _read_json_capped(meeting_dir / _TRANSCRIPT_FILE_NAME, _MAX_TRANSCRIPT_BYTES)
    if doc is None:
        return "no_voices", []
    embeddings = _label_embeddings(doc)
    if not embeddings:
        return "no_voices", []
    assignments, auto = _load_speakers(meeting_dir)

    votes: dict[str, Counter[str]] = defaultdict(Counter)
    hand: dict[str, Counter[str]] = defaultdict(Counter)
    speech: dict[str, float] = defaultdict(float)
    totals: Counter[str] = Counter()
    segments = doc.get("segments")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        label = segment.get("speaker")
        if not isinstance(label, str) or label not in embeddings:
            continue
        speech[label] += _segment_duration(segment)
        totals[label] += 1
        segment_id = str(segment.get("id"))
        name = assignments.get(segment_id)
        if not name or _is_generic(name):
            continue
        votes[label][name] += 1
        if auto.get(segment_id) != name:
            hand[label][name] += 1

    exemplars = []
    for label, counter in votes.items():
        name, count = counter.most_common(1)[0]
        exemplars.append(
            Exemplar(
                project=meeting_dir.parent.name,
                meeting=meeting_dir.name,
                label=label,
                name=name,
                vector=embeddings[label],
                speech_sec=speech[label],
                votes=count,
                named_segments=sum(counter.values()),
                hand_votes=hand[label][name],
                total_segments=totals[label],
            )
        )
    return ("named" if exemplars else "unnamed"), exemplars


def assess_exemplars(exemplars: Sequence[Exemplar]) -> list[AssessedExemplar]:
    """Decide which exemplars the memory may use, and say why not.

    In order: a voice the operator did not name themselves (``unconfirmed``
    -- most of its name's votes were written by this module), a voice whose
    name covers no more than half of its segments (``partial`` -- a couple
    of corrected lines inside a cluster nobody named say who spoke *those
    lines*, not whose voice the cluster is) and a voice with too little
    speech (``short``) are set aside. Among the rest, a
    voice that resembles another person's *usual* voice (the median over
    that person's exemplars) more than its own name's is a ``conflict``:
    either the label is wrong or two names mean one person, and matching
    against it would hand that person's voice to the wrong name. One wrong
    label among many right ones is outvoted by the median; the wrong one is
    the exemplar that gets flagged.
    """
    usable: list[Exemplar] = []
    verdicts: dict[int, AssessedExemplar] = {}
    for position, exemplar in enumerate(exemplars):
        if exemplar.hand_votes * 2 < exemplar.votes:
            verdicts[position] = AssessedExemplar(exemplar=exemplar, quality="unconfirmed")
        elif exemplar.votes * 2 <= exemplar.total_segments:
            verdicts[position] = AssessedExemplar(exemplar=exemplar, quality="partial")
        elif exemplar.speech_sec < MIN_EXEMPLAR_SPEECH_SEC:
            verdicts[position] = AssessedExemplar(exemplar=exemplar, quality="short")
        else:
            usable.append(exemplar)

    # Unit vectors once: the comparison below is every usable exemplar
    # against every other, across the whole vault.
    units = {id(exemplar): _unit(exemplar.vector) or [] for exemplar in usable}
    by_name: dict[str, list[Exemplar]] = defaultdict(list)
    for exemplar in usable:
        by_name[exemplar.name].append(exemplar)

    out: list[AssessedExemplar] = []
    for position, exemplar in enumerate(exemplars):
        if position in verdicts:
            out.append(verdicts[position])
            continue
        unit = units[id(exemplar)]
        own = [
            _dot(unit, units[id(other)])
            for other in by_name[exemplar.name]
            if other is not exemplar
        ]
        own_similarity = statistics.median(own) if own else None
        rival_name: str | None = None
        rival_similarity = -1.0
        for name, others in by_name.items():
            if name == exemplar.name:
                continue
            similarity = statistics.median(_dot(unit, units[id(o)]) for o in others)
            if similarity > rival_similarity:
                rival_name, rival_similarity = name, similarity
        if rival_similarity >= CONFLICT_MIN_SIMILARITY and (
            own_similarity is None or rival_similarity - own_similarity >= CONFLICT_MARGIN
        ):
            out.append(
                AssessedExemplar(exemplar=exemplar, quality="conflict", conflicts_with=rival_name)
            )
        else:
            out.append(AssessedExemplar(exemplar=exemplar, quality="ok"))
    return out


def project_dirs(vault_root: Path) -> list[Path]:
    """The vault's project folders (``unsorted`` included: its meetings are
    labelled like any other), in name order."""
    try:
        entries = sorted(entry for entry in vault_root.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(".") and entry.name.lower() not in _NON_PROJECT_DIRS
    ]


def scan_project(project_dir: Path) -> list[Exemplar]:
    """Every meeting's exemplars of one project, read straight from the
    folders."""
    try:
        meetings = sorted(entry for entry in project_dir.iterdir() if entry.is_dir())
    except OSError:
        return []
    exemplars: list[Exemplar] = []
    for meeting in meetings:
        exemplars.extend(scan_meeting(meeting)[1])
    return exemplars


def scan_vault(vault_root: Path) -> list[Exemplar]:
    """Every exemplar of the vault, read straight from the folders -- what
    the derived index caches, and the fallback when it cannot be opened."""
    exemplars: list[Exemplar] = []
    for project_dir in project_dirs(vault_root):
        exemplars.extend(scan_project(project_dir))
    return exemplars


def name_key(name: str) -> str:
    """How two spellings of a name are compared: trimmed and lower-cased,
    the same folding the roster itself deduplicates by
    (`commands/roster.rs`, `lib/roster.ts`)."""
    return name.strip().lower()


def strict_roster_names(project_dir: Path) -> dict[str, str] | None:
    """The names a project in ``roster`` mode allows, as folded name -> the
    roster's own spelling, or ``None`` when any name may be given (no
    roster, ``open`` mode, unreadable file).

    The one thing the service reads from ``roster.json``. The speaker cap
    and the lowered threshold of a strict roster still arrive as numbers on
    the job; the names cannot, because they bound a memory that now spans
    projects -- without them a strict project would be offered every voice
    the vault has ever heard.
    """
    data = _read_json_capped(project_dir / _ROSTER_FILE_NAME, _MAX_ROSTER_BYTES)
    if not data or data.get("mode") != _ROSTER_STRICT_MODE:
        return None
    names = data.get("names")
    if not isinstance(names, list):
        return {}
    return {
        name_key(name): name.strip() for name in names if isinstance(name, str) and name.strip()
    }


def read_vault_exemplars(vault_root: Path, memory: VoiceMemory | None) -> list[Exemplar]:
    """The vault's exemplars through ``memory`` (the derived index, which
    re-reads only the meetings that changed) when one is given, else straight
    from the folders. The two answer identically, and an index that fails
    falls back to the folders."""
    if memory is not None:
        try:
            return memory.vault_exemplars(vault_root)
        except Exception:
            logger.warning(
                "voice index unavailable; reading the vault's meetings directly",
                exc_info=True,
                extra={"event": "voice_index_unavailable"},
            )
    return scan_vault(vault_root)


def roster_names(project_dir: Path) -> frozenset[str]:
    """The folded names on a project's roster, whatever its mode: in
    ``open`` mode the list restricts nothing, but it still says who the
    operator expects in this project."""
    data = _read_json_capped(project_dir / _ROSTER_FILE_NAME, _MAX_ROSTER_BYTES)
    names = data.get("names") if data else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name_key(name) for name in names if isinstance(name, str) and name.strip())


def collect_known_voices(meeting_dir: Path, *, memory: VoiceMemory | None = None) -> KnownVoices:
    """The voices this meeting may be named from, gathered from every
    *other* meeting of the vault and, in a strict-roster project, narrowed
    to the names on its roster.

    The quality rules of ``assess_exemplars`` apply to the vault as a whole
    -- this meeting's own exemplars take part in the judgement even though
    they are not offered as references.
    """
    project_dir = meeting_dir.parent
    exemplars = read_vault_exemplars(project_dir.parent, memory)
    allowed = strict_roster_names(project_dir)
    at_home = set(roster_names(project_dir))

    voiceprints: dict[str, list[list[float]]] = defaultdict(list)
    for assessed in assess_exemplars(exemplars):
        exemplar = assessed.exemplar
        if assessed.quality != "ok":
            continue
        own_project = exemplar.project == project_dir.name
        if own_project and exemplar.meeting == meeting_dir.name:
            continue
        name = exemplar.name
        if allowed is not None:
            # A strict roster also decides the spelling: a voice known
            # elsewhere as "ANNA" is offered here as the roster's "Anna".
            listed = allowed.get(name_key(name))
            if listed is None:
                continue
            name = listed
        if own_project:
            at_home.add(name_key(name))
        voiceprints[name].append(exemplar.vector)
    return KnownVoices(
        voiceprints=dict(voiceprints),
        newcomers=frozenset(name for name in voiceprints if name_key(name) not in at_home),
    )


def collect_project_voiceprints(
    meeting_dir: Path, *, memory: VoiceMemory | None = None
) -> dict[str, list[list[float]]]:
    """``collect_known_voices`` as the bare name -> embeddings map."""
    return collect_known_voices(meeting_dir, memory=memory).voiceprints


def match_speakers(
    embeddings: dict[str, list[float]],
    voiceprints: dict[str, list[list[float]]],
    *,
    threshold: float,
    newcomers: frozenset[str] = frozenset(),
    speech_sec: Mapping[str, float] | None = None,
) -> dict[str, str]:
    """New-meeting label -> recognized name, greedy best-match-first.

    Each name is used at most once (two speakers in one meeting are two
    voices), and nothing below ``threshold`` matches at all. A name in
    ``newcomers`` needs ``NEWCOMER_MIN_SIMILARITY`` instead and, when
    ``speech_sec`` (label -> seconds of speech) is given, a voice with at
    least ``MIN_EXEMPLAR_SPEECH_SEC`` of it.
    """
    newcomer_bar = max(threshold, NEWCOMER_MIN_SIMILARITY)
    scored: list[tuple[float, str, str]] = []
    for label, vector in embeddings.items():
        substantial = speech_sec is None or speech_sec.get(label, 0.0) >= MIN_EXEMPLAR_SPEECH_SEC
        for name, known in voiceprints.items():
            if name in newcomers and not substantial:
                continue
            similarity = max((_cosine(vector, ref) for ref in known), default=0.0)
            if similarity >= (newcomer_bar if name in newcomers else threshold):
                scored.append((similarity, label, name))
    scored.sort(reverse=True)

    matches: dict[str, str] = {}
    used_names: set[str] = set()
    for _similarity, label, name in scored:
        if label in matches or name in used_names:
            continue
        matches[label] = name
        used_names.add(name)
    return matches


def _may_name(segment_id: str, assignments: Mapping[str, str], auto: Mapping[str, str]) -> bool:
    """May this module (re)name the segment? Only when nobody has: it has no
    name, a seeded generic one, or one this module wrote on an earlier pass
    and the operator left alone."""
    current = assignments.get(segment_id)
    return current is None or _is_generic(current) or auto.get(segment_id) == current


def segment_spans(
    segments: Sequence[Mapping[str, Any]], meeting_dir: Path
) -> list[tuple[str, float, float]]:
    """``(segment id, start, end)`` for every segment worth embedding on its
    own: long enough to carry a voice, and not already named by the operator
    (their word is final, so its voice need not be asked)."""
    assignments, auto = _load_speakers(meeting_dir)
    spans: list[tuple[str, float, float]] = []
    for segment in segments:
        segment_id = str(segment.get("id"))
        if not _may_name(segment_id, assignments, auto):
            continue
        if _segment_duration(segment) < SEGMENT_MIN_SEC:
            continue
        spans.append((segment_id, float(segment["start"]), float(segment["end"])))
    return spans


def identify_segments(
    segments: Sequence[Mapping[str, Any]],
    segment_embeddings: Mapping[str, Sequence[float]],
    voiceprints: Mapping[str, Sequence[Sequence[float]]],
    *,
    threshold: float,
    newcomers: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Segment id -> name, for segments whose own voice says who is speaking.

    A segment is confident when its best name clears ``threshold``
    (``NEWCOMER_MIN_SIMILARITY`` for a name in ``newcomers``) and beats
    the runner-up by ``SEGMENT_MARGIN``. A confident name is kept only when
    at least ``SEGMENT_MIN_VOTES`` segments of the same diarized cluster
    agree on it: a single segment's embedding is noisy, but several agreeing
    ones inside one cluster are a person the diarizer folded into somebody
    else's voice. Measured on the operator's vault this never lowered a
    project's accuracy and recovered about half of the segments of a merged
    cluster.
    """
    references: dict[str, list[list[float]]] = {}
    for name, vectors in voiceprints.items():
        units = [unit for unit in (_unit(vector) for vector in vectors) if unit is not None]
        if units:
            references[name] = units
    if not references:
        return {}

    confident: dict[str, str] = {}
    label_of: dict[str, str | None] = {}
    for segment in segments:
        segment_id = str(segment.get("id"))
        vector = segment_embeddings.get(segment_id)
        unit = _unit(vector) if vector else None
        if unit is None:
            continue
        scored = sorted(
            ((max(_dot(unit, ref) for ref in refs), name) for name, refs in references.items()),
            reverse=True,
        )
        best_similarity, best_name = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        bar = max(threshold, NEWCOMER_MIN_SIMILARITY) if best_name in newcomers else threshold
        if best_similarity >= bar and best_similarity - runner_up >= SEGMENT_MARGIN:
            confident[segment_id] = best_name
            label = segment.get("speaker")
            label_of[segment_id] = label if isinstance(label, str) else None

    votes: dict[str | None, Counter[str]] = defaultdict(Counter)
    for segment_id, name in confident.items():
        votes[label_of[segment_id]][name] += 1
    return {
        segment_id: name
        for segment_id, name in confident.items()
        if votes[label_of[segment_id]][name] >= SEGMENT_MIN_VOTES
    }


def _write_speakers_atomic(
    meeting_dir: Path, assignments: dict[str, str], auto: dict[str, str]
) -> None:
    body: dict[str, Any] = {"schema_version": _SPEAKERS_SCHEMA_VERSION, "assignments": assignments}
    if auto:
        body["auto"] = auto
    payload = json.dumps(body, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=meeting_dir, prefix=".speakers-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, meeting_dir / _SPEAKERS_FILE_NAME)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def auto_assign_speakers(
    meeting_dir: Path,
    embeddings: dict[str, list[float]],
    segments: list[dict[str, Any]],
    *,
    threshold: float,
    known: KnownVoices | None = None,
    segment_embeddings: Mapping[str, Sequence[float]] | None = None,
) -> NamingResult:
    """Pre-fill ``speakers.json`` from the voice memory.

    ``known`` is that memory when the caller already read it (it had to, to
    decide whether embedding segments was worth the pass); otherwise it is
    collected here. ``segment_embeddings`` (segment id -> vector)
    switches the per-segment layer on: a segment named by its own voice
    takes that name, every other segment takes its cluster's.

    With nothing added, the file is not rewritten at all.

    Operator assignments are never touched -- but a seeded ``Speaker N`` is
    not one, and neither is a name this module wrote earlier. The app's
    transcript viewer holds the whole speaker map, diarized labels included,
    and saves all of it the moment anything in the meeting is renamed, so an
    edited meeting's ``speakers.json`` names every segment. Treating those
    placeholders as decisions would make a re-run of "Identify speakers"
    (the pass that carries a roster's lowered threshold) inert on exactly
    the meetings it is run on. Every name written here is also recorded in
    the file's ``auto`` map, which is what keeps it from being mistaken for
    the operator's and from feeding the memory it came from.
    """
    if not embeddings or threshold > 1.0:
        return NamingResult()
    if known is None:
        known = collect_known_voices(meeting_dir)
    if not known:
        return NamingResult()
    speech: dict[str, float] = defaultdict(float)
    for segment in segments:
        label = segment.get("speaker") if isinstance(segment, dict) else None
        if isinstance(label, str):
            speech[label] += _segment_duration(segment)
    matches = match_speakers(
        embeddings,
        known.voiceprints,
        threshold=threshold,
        newcomers=known.newcomers,
        speech_sec=speech,
    )
    by_voice = (
        identify_segments(
            segments,
            segment_embeddings,
            known.voiceprints,
            threshold=threshold,
            newcomers=known.newcomers,
        )
        if segment_embeddings
        else {}
    )
    if not matches and not by_voice:
        return NamingResult()

    assignments, auto = _load_speakers(meeting_dir)
    # Dead `auto` entries (the operator renamed the segment since) are
    # dropped on the way through.
    auto = {key: name for key, name in auto.items() if assignments.get(key) == name}
    named = 0
    by_segment_voice = 0
    used: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        label = segment.get("speaker")
        segment_id = str(segment.get("id"))
        cluster_name = matches.get(label) if isinstance(label, str) else None
        name = by_voice.get(segment_id) or cluster_name
        if not name or not _may_name(segment_id, assignments, auto):
            continue
        if assignments.get(segment_id) == name:
            continue
        assignments[segment_id] = name
        auto[segment_id] = name
        named += 1
        used.add(name)
        if name != cluster_name:
            by_segment_voice += 1
    if named:
        _write_speakers_atomic(meeting_dir, assignments, auto)
        logger.info(
            "recognized returning speakers",
            extra={
                "event": "speakers_auto_assigned",
                "names": sorted(used),
                "segments": named,
                "by_segment_voice": by_segment_voice,
            },
        )
    return NamingResult(named=named, by_segment_voice=by_segment_voice, names=tuple(sorted(used)))
