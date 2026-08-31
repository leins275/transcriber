"""The hybrid-search HTTP surface (`POST /v1/search`).

Follows `model_routes.py`'s pattern: a `build_*_router(require_token)`
factory whose handlers pull their collaborators off `app.state`. The query
runs on the job manager's single serial executor (`run_serial`) because the
query embedding is model inference -- a search issued mid-transcription
honestly waits its turn.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request

from transcription.jobs import JobManager
from transcription.schema import SearchRequest, SearchResponse, SearchResultModel
from transcription.search.service import SearchService


def build_search_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter()
    deps = [Depends(require_token)]

    def _search_service(request: Request) -> SearchService:
        service: SearchService = request.app.state.search_service
        return service

    def _job_manager(request: Request) -> JobManager:
        manager: JobManager = request.app.state.job_manager
        return manager

    @router.post("/v1/search", response_model=SearchResponse, dependencies=deps)
    async def search(request: Request, payload: SearchRequest) -> SearchResponse:
        service = _search_service(request)
        results = await _job_manager(request).run_serial(
            lambda: service.search(payload.query, project=payload.project, top_k=payload.top_k)
        )
        return SearchResponse(results=[SearchResultModel(**result.as_dict()) for result in results])

    return router
