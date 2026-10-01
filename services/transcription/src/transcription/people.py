"""The speakers database: who the people in the vault are.

``<vault_root>/people.json`` is a small registry the operator edits from the
app's Speakers pages: one entry per person -- a canonical ``name``, the
``aliases`` the same person carries in meeting labels (other spellings,
short forms), and a free-text ``bio``. It is the one thing about speakers
that is *not* derived: everything else (which projects a person appears in,
their labelled speech, their voice samples) is read from the meetings.

A person has no id. They are addressed by any of their names, compared
folded (``name_key``); renaming keeps the old name as an alias, and adding
another person's name as an alias merges that person in. This is what lets
the labels already written in ``speakers.json`` files stay exactly as they
are: the registry is a layer of *resolution* over those strings, never a
reason to rewrite them.

People who appear in labels but were never edited are not in the file.
They still resolve to one person per folded name (``PeopleResolver``), so
"Anna" and "ANNA" are one voice even before anybody registers her.

The file is a file at vault root on purpose: every walker of the vault
looks at directories only, so it cannot surface as a project.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from transcription.errors import ErrorKind, ServiceError

logger = logging.getLogger("transcription")

PEOPLE_FILE_NAME = "people.json"
PEOPLE_SCHEMA_VERSION = 1

MAX_NAME_CHARS = 80
MAX_BIO_CHARS = 8000
MAX_ALIASES = 50
_MAX_PEOPLE_BYTES = 4 * 1024 * 1024

# One writer at a time: an edit is read-modify-write of the whole file.
_write_lock = threading.Lock()


class PersonNotFoundError(Exception):
    """Raised for a name that is neither registered nor labelled anywhere."""


def name_key(name: str) -> str:
    """How two spellings of a name are compared: trimmed and lower-cased,
    the same folding the project roster deduplicates by
    (`commands/roster.rs`, `lib/roster.ts`)."""
    return name.strip().lower()


@dataclass(frozen=True, kw_only=True)
class Person:
    name: str
    aliases: tuple[str, ...] = ()
    bio: str = ""

    def keys(self) -> set[str]:
        return {name_key(self.name), *(name_key(alias) for alias in self.aliases)}

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "aliases": list(self.aliases), "bio": self.bio}


def _clean_name(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(ErrorKind.INVALID_REQUEST, f"{what} must not be empty")
    name = " ".join(value.split())
    if len(name) > MAX_NAME_CHARS:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST, f"{what} is longer than {MAX_NAME_CHARS} characters"
        )
    return name


def _dedupe(names: Iterable[str], *, excluding: str) -> tuple[str, ...]:
    """`names` without blanks, without `excluding` and without folded
    duplicates, first spelling kept, order kept."""
    seen = {name_key(excluding)}
    out: list[str] = []
    for name in names:
        key = name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(" ".join(name.split()))
    return tuple(out)


class PeopleRegistry:
    """The registered people, as read from ``people.json``."""

    def __init__(self, people: Iterable[Person] = ()) -> None:
        self.people: list[Person] = list(people)

    # -- reading ---------------------------------------------------------

    @classmethod
    def load(cls, vault_root: Path) -> PeopleRegistry:
        """The registry, or an empty one when the file is absent, oversized
        or malformed: resolution then degrades to "every folded name is its
        own person", which is what the vault meant before the file existed."""
        path = vault_root / PEOPLE_FILE_NAME
        try:
            if not path.is_file() or path.stat().st_size > _MAX_PEOPLE_BYTES:
                return cls()
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "people.json is unreadable; treating the speakers database as empty",
                extra={"event": "people_unreadable"},
            )
            return cls()
        entries = data.get("people") if isinstance(data, dict) else None
        people: list[Person] = []
        taken: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip() or name_key(name) in taken:
                continue
            raw_aliases = entry.get("aliases")
            aliases = tuple(
                alias
                for alias in _dedupe(
                    (a for a in raw_aliases if isinstance(a, str))
                    if isinstance(raw_aliases, list)
                    else (),
                    excluding=name,
                )
                # A name can belong to one person only; the first entry wins.
                if name_key(alias) not in taken
            )
            bio = entry.get("bio")
            person = Person(
                name=" ".join(name.split()),
                aliases=aliases,
                bio=bio if isinstance(bio, str) else "",
            )
            taken |= person.keys()
            people.append(person)
        return cls(people)

    def find(self, name: str) -> Person | None:
        key = name_key(name)
        for person in self.people:
            if key in person.keys():
                return person
        return None

    def resolver(self, observed: Mapping[str, float] | None = None) -> PeopleResolver:
        return PeopleResolver(self, observed or {})

    # -- editing ---------------------------------------------------------

    def upsert(
        self,
        name: str,
        *,
        new_name: str | None = None,
        aliases: Iterable[str] | None = None,
        bio: str | None = None,
    ) -> Person:
        """Create or update the person `name` refers to; returns the result.

        ``new_name`` renames (the old name stays as an alias, so labels
        written under it keep resolving -- also when the same call replaces
        the alias list). ``aliases`` replaces the alias list; an alias that
        is another registered person's name merges that person in -- their
        aliases come along and their bio is kept when this one has none.
        Fields left ``None`` are untouched.
        """
        current = self.find(name)
        if current is None:
            current = Person(name=_clean_name(name, what="the name"))
            self.people.append(current)
        position = self.people.index(current)
        updated = current

        if bio is not None:
            if len(bio) > MAX_BIO_CHARS:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST, f"the bio is longer than {MAX_BIO_CHARS} characters"
                )
            updated = replace(updated, bio=bio.strip())

        if aliases is not None:
            wanted = [_clean_name(alias, what="an alias") for alias in aliases]
            merged_aliases: list[str] = []
            merged_bio = updated.bio
            for alias in wanted:
                merged_aliases.append(alias)
                other = self.find(alias)
                if other is not None and other is not current:
                    # Merge: the other person's every name becomes ours.
                    merged_aliases.extend((other.name, *other.aliases))
                    if not merged_bio:
                        merged_bio = other.bio
                    self.people.remove(other)
                    position = self.people.index(current)
            final = _dedupe(merged_aliases, excluding=updated.name)
            if len(final) > MAX_ALIASES:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST, f"more than {MAX_ALIASES} aliases for one speaker"
                )
            updated = replace(updated, aliases=final, bio=merged_bio)

        if new_name is not None:
            cleaned = _clean_name(new_name, what="the new name")
            owner = self.find(cleaned)
            if owner is not None and owner is not current:
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"{cleaned!r} is already another speaker; add it under "
                    "'also known as' to merge the two",
                )
            if cleaned != updated.name:
                # Applied after the aliases on purpose: whatever alias list
                # came with the rename, the name being replaced stays one of
                # the person's names, so labels written under it keep
                # resolving. Dropping it is a separate, explicit edit.
                kept = _dedupe((*updated.aliases, updated.name), excluding=cleaned)
                updated = replace(updated, name=cleaned, aliases=kept)

        self.people[position] = updated
        return updated

    def delete(self, name: str) -> bool:
        person = self.find(name)
        if person is None:
            return False
        self.people.remove(person)
        return True

    def save(self, vault_root: Path) -> None:
        """Write the registry atomically (temp file + rename), people in
        name order so the file diffs cleanly under a sync client."""
        payload = json.dumps(
            {
                "schema_version": PEOPLE_SCHEMA_VERSION,
                "people": [
                    person.as_dict()
                    for person in sorted(self.people, key=lambda p: name_key(p.name))
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_name = tempfile.mkstemp(dir=vault_root, prefix=".people-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, vault_root / PEOPLE_FILE_NAME)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


class PeopleResolver:
    """Any spelling found in a label -> the person's canonical name.

    A registered person answers with their registry name for their name and
    every alias. An unregistered spelling answers with the most used
    spelling among those that fold to the same key (``observed`` maps the
    spellings seen in the vault to how much they are used), so case and
    spacing variants of one name are one person without anybody having to
    register them.
    """

    def __init__(self, registry: PeopleRegistry, observed: Mapping[str, float]) -> None:
        self._by_key: dict[str, str] = {}
        best: dict[str, tuple[float, str]] = {}
        for spelling, weight in observed.items():
            key = name_key(spelling)
            if not key:
                continue
            candidate = (weight, " ".join(spelling.split()))
            # Most used wins; ties go to the spelling that sorts first, so
            # the answer does not depend on iteration order.
            if key not in best or (candidate[0], best[key][1]) > (best[key][0], candidate[1]):
                best[key] = candidate
        for key, (_weight, spelling) in best.items():
            self._by_key[key] = spelling
        # The registry is applied last: it outranks whatever is observed.
        for person in registry.people:
            for key in person.keys():
                self._by_key[key] = person.name

    def __call__(self, name: str) -> str:
        return self._by_key.get(name_key(name), " ".join(name.split()))

    def knows(self, name: str) -> bool:
        return name_key(name) in self._by_key


def edit_registry(
    vault_root: Path,
    name: str,
    *,
    new_name: str | None = None,
    aliases: Iterable[str] | None = None,
    bio: str | None = None,
) -> Person:
    """Read-modify-write one person under the writer lock."""
    with _write_lock:
        registry = PeopleRegistry.load(vault_root)
        person = registry.upsert(name, new_name=new_name, aliases=aliases, bio=bio)
        registry.save(vault_root)
    return person


def delete_from_registry(vault_root: Path, name: str) -> bool:
    with _write_lock:
        registry = PeopleRegistry.load(vault_root)
        deleted = registry.delete(name)
        if deleted:
            registry.save(vault_root)
    return deleted
