"""Provider registry: name -> "module:Class" strings, resolved lazily (FR-4).

`_REGISTRY` holds the concrete providers as import-path strings, resolved with
`importlib` only *inside* `get_provider`, so importing this package alone never
pulls in `faster_whisper` (NFR-1, NFR-6). Transcription is local-only: the
built-in `local_whisper.py` is the sole shipping provider; the `register()`
hook adds test doubles without touching this module.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from transcription.errors import ErrorKind, ServiceError

if TYPE_CHECKING:
    from transcription.providers.base import TranscriptionProvider

logger = logging.getLogger("transcription")

_REGISTRY: dict[str, str | type] = {
    "local": "transcription.providers.local_whisper:LocalWhisperProvider",
}

# The provider name jobs are submitted under. `local` is the whisper
# engine of this machine: `get_provider` resolves it through the engine
# plan (`engines.py`) to one of the accelerated engines below when its
# payload is on disk, and to faster-whisper otherwise -- the ledger, the
# job API and `config.provider` keep saying `local` either way, so the
# engines are not registry names a job can be submitted under.
LOCAL_PROVIDER = "local"
_ENGINE_PROVIDERS: dict[str, str] = {
    "mlx": "transcription.providers.mlx_engine:MlxWhisperProvider",
    "openvino": "transcription.providers.openvino_engine:OpenVinoWhisperProvider",
}


def register(name: str, provider_cls: type) -> None:
    """Register a provider class directly under `name` (the test hook)."""
    _REGISTRY[name] = provider_cls


def known_provider_names() -> frozenset[str]:
    """The registered provider names -- a membership check only, no import.

    Cheap enough to call synchronously from an ``async def`` request handler
    (FR-2, NFR-1): unlike :func:`get_provider`, this never touches
    ``importlib`` (E15).
    """
    return frozenset(_REGISTRY)


def validate_provider_name(name: str) -> None:
    """Raise ``ServiceError(invalid_request)`` if `name` isn't registered.

    Checking membership in ``_REGISTRY`` never imports a provider module, so
    a bogus provider name is still rejected before any job/ledger row exists
    (E1) without paying for a `faster_whisper` import to find that out on the
    request path (E15).
    """
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"unknown provider {name!r}; known providers: {known}",
        )


def get_provider(name: str, config: Any) -> TranscriptionProvider:
    """Resolve and instantiate the provider registered under `name`.

    The concrete provider module is imported lazily, here, so that merely
    importing `transcription.providers` never loads a provider library.
    Callers on the event-loop-bound request path should call this off the
    event loop (e.g. via ``asyncio.to_thread``) -- see ``jobs.py`` (E15).
    """
    validate_provider_name(name)
    if name == LOCAL_PROVIDER and isinstance(_REGISTRY[name], str):
        return _local_provider(config)
    return _instantiate(name, config)


def _instantiate(name: str, config: Any) -> TranscriptionProvider:
    target = _REGISTRY.get(name) or _ENGINE_PROVIDERS[name]
    if isinstance(target, str):
        module_name, _, class_name = target.partition(":")
        module = importlib.import_module(module_name)
        provider_cls = getattr(module, class_name)
    else:
        provider_cls = target
    return provider_cls(config)  # type: ignore[no-any-return]


def _local_provider(config: Any) -> TranscriptionProvider:
    """The whisper engine of this machine (see `LOCAL_PROVIDER`).

    Degradation over failure: an accelerated provider whose library does
    not import (a half-fetched runtime, a wheel the OS loader refuses)
    logs why and hands over to faster-whisper instead of failing the job.
    """
    from transcription import engines, hardware  # noqa: PLC0415 - keeps import light

    accel = hardware.detect()
    backend = engines.resolve_whisper_backend(config, accel, engines.presence(config))
    provider_name = engines.PROVIDER_FOR_BACKEND.get(backend, LOCAL_PROVIDER)
    if provider_name == LOCAL_PROVIDER:
        return _instantiate(LOCAL_PROVIDER, config)
    try:
        return _instantiate(provider_name, config)
    except Exception as exc:  # noqa: BLE001 - the fallback engine must still run
        logger.warning(
            "whisper engine %s unavailable (%s); falling back to faster-whisper",
            backend,
            exc,
            extra={"event": "whisper_engine_fallback", "backend": backend},
        )
        return _instantiate(LOCAL_PROVIDER, config)
