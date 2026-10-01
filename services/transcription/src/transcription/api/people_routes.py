"""The speakers database's HTTP surface: the table, one person's page, and
the edits (`GET /v1/people`, `GET /v1/people/detail`, `PUT /v1/people`,
`DELETE /v1/people`).

Follows `search_routes.py`'s pattern: a `build_*_router(require_token)`
factory whose handlers pull their collaborators off `app.state`. No model
runs here, so nothing goes through the serial queue: the views read the
voice index (which refreshes itself on every read) and the edits rewrite
one small JSON file, all on a worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from transcription import people, people_view
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import JobManager
from transcription.schema import (
    PeopleListResponse,
    PersonDeleteResponse,
    PersonDetailResponse,
    PersonModel,
    PersonUpdate,
)
from transcription.voice_index import VoiceIndex


def build_people_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter()
    deps = [Depends(require_token)]

    def _vault_root(request: Request) -> Path:
        config = request.app.state.config
        if not config.vault_root:
            raise ServiceError(ErrorKind.INVALID_REQUEST, "no vault_root is configured")
        return Path(config.vault_root)

    def _index(request: Request) -> VoiceIndex:
        manager: JobManager = request.app.state.job_manager
        index = manager.voice_index()
        if index is None:
            raise ServiceError(ErrorKind.INTERNAL, "the voice index could not be opened")
        return index

    @router.get("/v1/people", response_model=PeopleListResponse, dependencies=deps)
    async def list_people(request: Request) -> PeopleListResponse:
        """Everybody the vault knows: the registered people and everybody
        whose name appears in a meeting's labels."""
        vault_root = _vault_root(request)
        return await asyncio.to_thread(
            lambda: PeopleListResponse(**people_view.people_list(_index(request), vault_root))
        )

    @router.get("/v1/people/detail", response_model=PersonDetailResponse, dependencies=deps)
    async def person_detail(request: Request, name: str) -> PersonDetailResponse:
        """One person, addressed by any of their names; 404 for a name that
        is neither registered nor used in a label."""
        vault_root = _vault_root(request)
        return await asyncio.to_thread(
            lambda: PersonDetailResponse(
                **people_view.person_detail(_index(request), vault_root, name)
            )
        )

    @router.put("/v1/people", response_model=PersonModel, dependencies=deps)
    async def save_person(request: Request, payload: PersonUpdate) -> PersonModel:
        """Create or update a person. A rename keeps the old name as an
        alias; an alias that is another registered person's name merges
        that person in. Labels in meetings are never rewritten."""
        vault_root = _vault_root(request)
        person = await asyncio.to_thread(
            lambda: people.edit_registry(
                vault_root,
                payload.name,
                new_name=payload.new_name,
                aliases=payload.aliases,
                bio=payload.bio,
            )
        )
        return PersonModel(**person.as_dict(), registered=True)

    @router.delete("/v1/people", response_model=PersonDeleteResponse, dependencies=deps)
    async def delete_person(request: Request, name: str) -> PersonDeleteResponse:
        """Remove a person's registry entry (name, aliases, bio). Their
        labels stay, so they reappear as unregistered while labelled."""
        vault_root = _vault_root(request)
        deleted = await asyncio.to_thread(lambda: people.delete_from_registry(vault_root, name))
        return PersonDeleteResponse(deleted=deleted)

    return router
