"""The hybrid-search HTTP surface: `POST /v1/search` and the SSE chat
(`POST /v1/chat`).

Follows `model_routes.py`'s pattern: a `build_*_router(require_token)`
factory whose handlers pull their collaborators off `app.state`. Anything
that is really model inference -- the query embedding, the chat completion
-- runs on the job manager's single serial executor, so a request issued
mid-transcription honestly waits its turn.

The chat stream deliberately does NOT unload the LLM afterwards, whatever
`llm_keep_loaded` says: a chat session is interactive, and paying a full
GGUF reload per message would be brutal. The next summarize job's own
unload still applies.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import JobManager
from transcription.llm.base import LlmProvider, Message
from transcription.llm.reasoning import ThinkStreamFilter, split_reasoning
from transcription.providers.base import CancelToken
from transcription.schema import ChatRequest, SearchRequest, SearchResponse, SearchResultModel
from transcription.search.chat import RetrievedChunk, build_chat_messages
from transcription.search.service import SearchService

_logger = logging.getLogger("transcription")


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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

    @router.post("/v1/chat", dependencies=deps)
    async def chat(request: Request, payload: ChatRequest) -> StreamingResponse:
        manager = _job_manager(request)
        service = _search_service(request)
        config = request.app.state.config

        question = payload.messages[-1].content
        history: list[Message] = [
            {"role": message.role, "content": message.content} for message in payload.messages[:-1]
        ]

        # Failures before the stream starts are ordinary JSON errors; the
        # provider resolves here (and reports a missing GGUF as model_load)
        # rather than mid-stream.
        provider: LlmProvider = await manager.resolve_llm_for_chat()
        pairs = await manager.run_serial(
            functools.partial(
                service.retrieve, question, project=payload.project, top_k=config.search_top_k
            )
        )
        chunks = [RetrievedChunk(result=result, text=text) for result, text in pairs]
        messages, sources = build_chat_messages(
            history=history,
            question=question,
            chunks=chunks,
            budget_tokens=manager.llm_budget_tokens(),
            count_tokens=provider.count_tokens,
        )

        loop = asyncio.get_running_loop()
        out: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        cancel = CancelToken()
        filt = ThinkStreamFilter()
        max_tokens = config.llm_max_output_tokens + config.llm_think_headroom_tokens

        def on_token(piece: str) -> None:
            # Runs on the serial executor thread; the filter is only ever
            # touched there, so it needs no lock.
            visible = filt.feed(piece)
            if visible:
                loop.call_soon_threadsafe(out.put_nowait, ("delta", visible))

        def body() -> None:
            # The `error`/`done` sentinel is load-bearing: without one the
            # SSE generator below would wait forever.
            try:
                completion = provider.complete(
                    messages,
                    json_schema=None,
                    max_tokens=max_tokens,
                    temperature=config.llm_temperature,
                    on_progress=lambda _fraction: None,
                    cancel=cancel,
                    on_token=on_token,
                )
                tail = filt.flush()
                # Belt and braces: whatever reached the client plus this
                # tail must never include a think block.
                tail, _reasoning = split_reasoning(tail) if tail else (tail, None)
                if tail:
                    loop.call_soon_threadsafe(out.put_nowait, ("delta", tail))
                loop.call_soon_threadsafe(out.put_nowait, ("done", completion))
            except BaseException as exc:  # noqa: BLE001 - forwarded as the sentinel
                loop.call_soon_threadsafe(out.put_nowait, ("error", exc))

        completion_task = asyncio.create_task(manager.run_serial(body))

        async def stream() -> AsyncIterator[str]:
            try:
                yield _sse("sources", {"sources": [source.as_dict() for source in sources]})
                while True:
                    kind, value = await out.get()
                    if kind == "delta":
                        yield _sse("delta", {"text": value})
                    elif kind == "done":
                        yield _sse(
                            "done",
                            {
                                "finish_reason": value.finish_reason or "stop",
                                "completion_tokens": value.completion_tokens,
                            },
                        )
                        return
                    else:
                        exc = value
                        if isinstance(exc, ServiceError):
                            kind_value, message = exc.kind.value, exc.message
                        else:
                            kind_value, message = ErrorKind.INTERNAL.value, str(exc)
                            _logger.error("chat completion failed", exc_info=exc)
                        yield _sse("error", {"error_kind": kind_value, "error_message": message})
                        return
            finally:
                # Client gone or stream over: stop generation within one
                # token, and never leave the executor task dangling.
                cancel.set()
                completion_task.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
