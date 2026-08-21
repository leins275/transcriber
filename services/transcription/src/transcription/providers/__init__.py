"""Provider registry: name -> "module:Class" strings, resolved lazily (FR-4).

This file is written once (T8) and never edited again: `_REGISTRY` holds the
concrete providers as import-path strings, resolved with `importlib` only
*inside* `get_provider`, so importing this package alone never pulls in
`faster_whisper` or `litellm` (NFR-1, NFR-6). T10 and T11 add
`local_whisper.py` and `litellm_cloud.py` later without touching this module.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from transcription.errors import ErrorKind, ServiceError

if TYPE_CHECKING:
    from transcription.providers.base import TranscriptionProvider

_REGISTRY: dict[str, str | type] = {
    "local": "transcription.providers.local_whisper:LocalWhisperProvider",
    "cloud": "transcription.providers.litellm_cloud:LiteLLMProvider",
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
    (E1) without paying for a `faster_whisper`/`litellm` import to find that
    out on the request path (E15).
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
    target = _REGISTRY[name]

    if isinstance(target, str):
        module_name, _, class_name = target.partition(":")
        module = importlib.import_module(module_name)
        provider_cls = getattr(module, class_name)
    else:
        provider_cls = target

    return provider_cls(config)  # type: ignore[no-any-return]
