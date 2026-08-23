"""FastAPI app: routes, bearer auth, error mapping (FR-2, FR-8, FR-9).

The app object is built by :func:`create_app`, a factory taking a resolved
``Config`` -- there is no module-level app instance, so importing this module
has no side effects (NFR-1). The ``lifespan`` context opens the ledger,
reconciles interrupted rows (NFR-7) and starts/stops the single-worker
``JobManager`` (FR-2). A bearer-token dependency guards every ``/v1/*`` route
while ``/health`` stays reachable without a token so a supervisor can probe
liveness. One exception handler maps ``ServiceError`` to the FR-8 taxonomy
body; anything unclassified becomes a 500 with the traceback logged, never
returned (FR-8: never a generic message, never a leaked traceback).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from transcription import __version__
from transcription.api import model_routes
from transcription.api.model_routes import (
    ModelDownloadManager,
    build_model_router,
    is_model_present,
)
from transcription.config import Config
from transcription.cuda_runtime import is_cuda_runtime_present
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import JobManager, JobNotFoundError
from transcription.ledger import Ledger
from transcription.model_download import ModelDownload
from transcription.schema import JobCreate, JobStatus

_logger = logging.getLogger("transcription")

# Only `invalid_request`/`unsupported_input` are ever raised synchronously
# from a request handler (path validation, in `JobManager.submit`); every
# other taxonomy value is a job-body failure surfaced through
# `GET /v1/jobs/{id}`, never as a synchronous HTTP error response, so it
# falls back to 500 here if it somehow leaked (plan's taxonomy table).
_STATUS_BY_KIND: Mapping[ErrorKind, int] = {
    ErrorKind.INVALID_REQUEST: 400,
    ErrorKind.UNSUPPORTED_INPUT: 400,
}


def _http_status_for(kind: ErrorKind) -> int:
    return _STATUS_BY_KIND.get(kind, 500)


def _error_body(
    kind: ErrorKind, message: str, *, provider_status: int | None = None
) -> dict[str, Any]:
    return {"error_kind": kind.value, "error_message": message, "provider_status": provider_status}


def create_app(
    config: Config,
    *,
    model_download_factory: Callable[[], ModelDownload] | None = None,
) -> FastAPI:
    """Build the FastAPI app for one process run (FR-2); no import-time side effects (NFR-1).

    ``model_download_factory`` is a test-only seam (mirrors T10's
    ``HubClient``/``Transport`` seams): production callers never pass it, so
    :class:`ModelDownloadManager` builds its own real download from
    ``config``.
    """
    ledger = Ledger(config.db_path)
    job_manager = JobManager(config, ledger)
    model_download_manager = ModelDownloadManager(config, factory=model_download_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ledger.reconcile_interrupted()
        await job_manager.start()
        try:
            yield
        finally:
            await job_manager.aclose()
            ledger.close()

    app = FastAPI(lifespan=lifespan)
    app.state.config = config
    app.state.ledger = ledger
    app.state.job_manager = job_manager
    app.state.model_download_manager = model_download_manager

    def require_token(authorization: str | None = Header(default=None)) -> None:
        """Bearer-auth dependency for every `/v1/*` route (FR-9).

        Compares as bytes (encoded permissively): Starlette decodes header
        values as latin-1, so a request whose `Authorization` header
        contains raw non-ASCII bytes reaches here as a non-ASCII `str`, and
        `secrets.compare_digest` raises `TypeError` when given two `str`
        arguments outside ASCII. Any wrong token -- ASCII or not -- must
        answer `401`, never crash to a `500` (E16).
        """
        if not config.token:
            return
        expected = f"Bearer {config.token}".encode()
        if authorization is None or not secrets.compare_digest(
            authorization.encode("utf-8", errors="replace"), expected
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(ServiceError)
    async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=_http_status_for(exc.kind), content=exc.to_dict())

    @app.exception_handler(JobNotFoundError)
    async def _job_not_found_handler(request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_body(ErrorKind.INVALID_REQUEST, str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_body(ErrorKind.INVALID_REQUEST, "request validation failed"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        _logger.error("unhandled exception on %s", request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(ErrorKind.INTERNAL, "internal error"),
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        public = config.public()
        # `provider_info()` never constructs (and so never imports) a
        # provider library on this request path (E15, NFR-1): before
        # anything has resolved the default provider (e.g. a job running
        # it), this reports the *unresolved* config device/model with
        # `model_state: "unloaded"`; once a job has run it, it reports the
        # provider's live, cached `describe()` -- the resolved device and
        # advancing `model_state` (FR-2, FR-3 acceptance).
        info = job_manager.provider_info()
        return {
            "status": "ok",
            "version": __version__,
            "provider": public["provider"],
            "model": info.model,
            "device": info.device,
            "model_state": info.model_state,
            # So the app can detect a missing model without guessing,
            # instead of inferring it from a failed transcription (FR-17).
            "model_present": is_model_present(config),
            # E13: whether the CUDA runtime is on disk, gated by the same
            # `_nvidia_gpu_present()` probe `build_setup_download` decides the
            # download on -- `None` on a GPU-less host (or non-Windows) so
            # the app never prompts about a runtime that machine could never
            # use. Reports `is_cuda_runtime_present()` verbatim otherwise, so
            # the UI can detect "model present, CUDA runtime missing" even
            # outside an active `cuda_warning` (e.g. a fresh process after an
            # earlier run's failed download).
            "cuda_runtime_present": (
                is_cuda_runtime_present(config.app_dir)
                if model_routes._nvidia_gpu_present()
                else None
            ),
        }

    v1_deps = [Depends(require_token)]

    @app.post("/v1/jobs", status_code=202, dependencies=v1_deps)
    async def submit_job(payload: JobCreate) -> dict[str, str]:
        job_id = await job_manager.submit(
            audio_path=payload.audio_path,
            output_dir=payload.output_dir,
            language=payload.language,
            provider=payload.provider,
            model=payload.model,
            meeting=payload.meeting,
            diarize=payload.diarize,
        )
        return {"job_id": job_id}

    @app.get("/v1/jobs", dependencies=v1_deps)
    async def list_jobs_route(
        limit: int = Query(default=50, ge=1, le=500), status: str | None = None
    ) -> list[dict[str, Any]]:
        return ledger.list_jobs(limit=limit, status=status)

    @app.get("/v1/jobs/{job_id}", response_model=JobStatus, dependencies=v1_deps)
    async def get_job(job_id: str) -> JobStatus:
        job = job_manager.status(job_id)
        return JobStatus(
            job_id=job.job_id,
            status=job.status,  # type: ignore[arg-type]
            progress=job.progress,
            elapsed_sec=job.elapsed_sec,
            audio_duration_sec=job.audio_duration_sec,
            provider=job.provider,
            cost_usd=job.cost_usd,
            error_kind=job.error_kind,
            error_message=job.error_message,
        )

    @app.get("/v1/jobs/{job_id}/result", dependencies=v1_deps)
    async def get_job_result(job_id: str) -> Any:
        job = job_manager.status(job_id)
        if job.status != "succeeded":
            return JSONResponse(
                status_code=404,
                content=_error_body(
                    ErrorKind.INVALID_REQUEST, f"result not available for job {job_id}"
                ),
            )
        transcript_path = Path(job.output_path) / "transcript.json"
        if not transcript_path.exists():
            return JSONResponse(
                status_code=404,
                content=_error_body(
                    ErrorKind.INVALID_REQUEST, f"result not available for job {job_id}"
                ),
            )
        return json.loads(transcript_path.read_text(encoding="utf-8"))

    @app.delete("/v1/jobs/{job_id}", dependencies=v1_deps)
    async def cancel_job(job_id: str) -> dict[str, str]:
        await job_manager.cancel(job_id)
        return {"status": "cancelled"}

    app.include_router(build_model_router(require_token))

    return app
