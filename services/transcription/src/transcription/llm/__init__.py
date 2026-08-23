"""LLM engine registry: name -> "module:Class" strings, resolved lazily.

The exact shape of ``providers/__init__.py`` (FR-4, NFR-1, NFR-6): importing
this package alone never pulls in ``llama_cpp`` or ``litellm`` -- the
concrete engine module is imported only inside :func:`get_llm_provider`.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from transcription.errors import ErrorKind, ServiceError

if TYPE_CHECKING:
    from transcription.llm.base import LlmProvider

# The built-in engine's registry name (also the config default). Modules
# outside this package compare against this constant rather than the literal
# string, keeping the isolation grep test (`test_attribution.py`) meaningful.
BUILTIN_ENGINE = "llama_cpp"

_REGISTRY: dict[str, str | type] = {
    BUILTIN_ENGINE: "transcription.llm.llama_cpp_local:LlamaCppProvider",
    "openai_compat": "transcription.llm.openai_compat:OpenAICompatProvider",
}


def register(name: str, provider_cls: type) -> None:
    """Register an LLM engine class directly under `name` (the test hook)."""
    _REGISTRY[name] = provider_cls


def known_llm_provider_names() -> frozenset[str]:
    """The registered engine names -- a membership check only, no import."""
    return frozenset(_REGISTRY)


def validate_llm_provider_name(name: str) -> None:
    """Raise ``ServiceError(invalid_request)`` if `name` isn't registered.

    Membership in ``_REGISTRY`` never imports an engine module, so a bogus
    name is rejected before any job/ledger row exists without paying for a
    ``llama_cpp``/``litellm`` import on the request path (E1, E15).
    """
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"unknown llm provider {name!r}; known llm providers: {known}",
        )


def get_llm_provider(name: str, config: Any) -> LlmProvider:
    """Resolve and instantiate the engine registered under `name`.

    The concrete module is imported lazily, here, so importing
    ``transcription.llm`` never loads an LLM library. Callers on the
    event-loop-bound request path must run this off the event loop (E15).
    """
    validate_llm_provider_name(name)
    target = _REGISTRY[name]

    if isinstance(target, str):
        module_name, _, class_name = target.partition(":")
        module = importlib.import_module(module_name)
        provider_cls = getattr(module, class_name)
    else:
        provider_cls = target

    return provider_cls(config)  # type: ignore[no-any-return]
