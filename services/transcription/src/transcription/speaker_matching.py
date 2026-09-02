"""Cross-meeting speaker naming from diarization voice embeddings.

After a diarized transcription lands, its per-speaker voice embeddings are
compared against the project's speaker memory: voices the operator has
already named in *sibling* meetings (each sibling's ``speakers.json``
assignments joined to its ``transcript.json``'s stored
``diarization.speaker_embeddings``). A close-enough cosine match pre-fills
``speakers.json`` for the new meeting, so a returning voice opens already
named.

Additive only, by contract: ``speakers.json`` is the operator's file (see
the vault-side comment on ``SPEAKERS_FILE_NAME`` in the app), and this
module never overwrites an existing assignment -- it fills segments that
have none. Everything here degrades rather than fails: an unreadable
sibling contributes nothing, and the caller treats any raised error as a
job warning.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("transcription")

_SPEAKERS_FILE_NAME = "speakers.json"
_TRANSCRIPT_FILE_NAME = "transcript.json"
_SPEAKERS_SCHEMA_VERSION = 1

# Caps mirror the app's readers: a bigger file is something other than what
# it claims to be.
_MAX_SPEAKERS_BYTES = 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0 or not math.isfinite(norm_a * norm_b):
        return 0.0
    return dot / (norm_a * norm_b)


def _read_json_capped(path: Path, cap: int) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_assignments(meeting_dir: Path) -> dict[str, str]:
    """``speakers.json``'s segment-id -> name map (empty when absent)."""
    data = _read_json_capped(meeting_dir / _SPEAKERS_FILE_NAME, _MAX_SPEAKERS_BYTES)
    assignments = data.get("assignments") if data else None
    if not isinstance(assignments, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in assignments.items()
        if isinstance(value, str) and value.strip()
    }


def _label_embeddings(doc: dict[str, Any]) -> dict[str, list[float]]:
    """``diarization.speaker_embeddings`` as clean label -> vector."""
    diarization = doc.get("diarization")
    embeddings = diarization.get("speaker_embeddings") if isinstance(diarization, dict) else None
    if not isinstance(embeddings, dict):
        return {}
    out: dict[str, list[float]] = {}
    for label, vector in embeddings.items():
        if isinstance(vector, list) and vector and all(isinstance(v, (int, float)) for v in vector):
            out[str(label)] = [float(v) for v in vector]
    return out


def collect_project_voiceprints(meeting_dir: Path) -> dict[str, list[list[float]]]:
    """The project's speaker memory: operator-given name -> known voice
    embeddings, gathered from every *other* meeting in the same project.

    A sibling contributes one vector per diarized label whose segments the
    operator has (majority-)named. Meetings without embeddings or without
    assignments contribute nothing.
    """
    voiceprints: dict[str, list[list[float]]] = defaultdict(list)
    project_dir = meeting_dir.parent
    try:
        siblings = [
            entry
            for entry in project_dir.iterdir()
            if entry.is_dir() and entry.name != meeting_dir.name
        ]
    except OSError:
        return {}

    for sibling in siblings:
        doc = _read_json_capped(sibling / _TRANSCRIPT_FILE_NAME, _MAX_TRANSCRIPT_BYTES)
        if doc is None:
            continue
        embeddings = _label_embeddings(doc)
        if not embeddings:
            continue
        assignments = _load_assignments(sibling)
        if not assignments:
            continue

        # Majority vote: which name did the operator give this label's
        # segments? (A stray mis-assigned segment must not rename a voice.)
        votes: dict[str, Counter[str]] = defaultdict(Counter)
        segments = doc.get("segments")
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            label = segment.get("speaker")
            name = assignments.get(str(segment.get("id")))
            if isinstance(label, str) and label in embeddings and name:
                votes[label][name] += 1
        for label, counter in votes.items():
            name = counter.most_common(1)[0][0]
            voiceprints[name].append(embeddings[label])

    return dict(voiceprints)


def match_speakers(
    embeddings: dict[str, list[float]],
    voiceprints: dict[str, list[list[float]]],
    *,
    threshold: float,
) -> dict[str, str]:
    """New-meeting label -> recognized name, greedy best-match-first.

    Each name is used at most once (two speakers in one meeting are two
    voices), and nothing below ``threshold`` matches at all.
    """
    scored: list[tuple[float, str, str]] = []
    for label, vector in embeddings.items():
        for name, known in voiceprints.items():
            similarity = max((_cosine(vector, ref) for ref in known), default=0.0)
            if similarity >= threshold:
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


def _write_speakers_atomic(meeting_dir: Path, assignments: dict[str, str]) -> None:
    payload = json.dumps(
        {"schema_version": _SPEAKERS_SCHEMA_VERSION, "assignments": assignments},
        ensure_ascii=False,
        indent=2,
    )
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
) -> int:
    """Pre-fill ``speakers.json`` from the project's speaker memory.

    Returns how many segments gained a name. Existing assignments are never
    touched; with none added, the file is not rewritten at all.
    """
    if not embeddings or threshold > 1.0:
        return 0
    voiceprints = collect_project_voiceprints(meeting_dir)
    if not voiceprints:
        return 0
    matches = match_speakers(embeddings, voiceprints, threshold=threshold)
    if not matches:
        return 0

    assignments = _load_assignments(meeting_dir)
    added = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        label = segment.get("speaker")
        if not isinstance(label, str):
            continue
        name = matches.get(label)
        segment_id = str(segment.get("id"))
        if name and segment_id not in assignments:
            assignments[segment_id] = name
            added += 1
    if added:
        _write_speakers_atomic(meeting_dir, assignments)
        logger.info(
            "recognized returning speakers",
            extra={
                "event": "speakers_auto_assigned",
                "names": sorted(set(matches.values())),
                "segments": added,
            },
        )
    return added
