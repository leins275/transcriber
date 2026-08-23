"""Structured-output shapes for the extraction jobs (pydantic, pure data).

``model_json_schema()`` of these models is what the engines constrain
generation against (llama.cpp: compiled to a grammar; OpenAI protocol:
``response_format``), and :func:`parse_llm_json` validates what came back --
one source of truth for both directions.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

ItemType = Literal["requirement", "epic", "task", "spike"]
FactKind = Literal["fact", "answered_question"]


class ActionItemOut(BaseModel):
    """One extracted action item, as the model reports it."""

    type: ItemType
    title: str = Field(min_length=1, max_length=200)
    description_md: str = ""
    # Seconds from the start of the recording, cited from the [m:ss] markers.
    timestamps: list[float] = Field(default_factory=list)


class ActionItemsOut(BaseModel):
    items: list[ActionItemOut] = Field(default_factory=list)


class FactOut(BaseModel):
    """One extracted fact or answered question."""

    kind: FactKind = "fact"
    title: str = Field(min_length=1, max_length=200)
    description_md: str = ""
    timestamps: list[float] = Field(default_factory=list)


class FactsOut(BaseModel):
    items: list[FactOut] = Field(default_factory=list)


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LlmOutputError(ValueError):
    """The model's output was not valid JSON for the requested schema.

    Carries the raw text so the caller's one repair retry can show the model
    its own output; the caller converts a persistent failure into
    ``ServiceError(ErrorKind.LLM_OUTPUT)``.
    """

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def parse_llm_json[ModelT: BaseModel](text: str, model_cls: type[ModelT]) -> ModelT:
    """Parse and validate a model's JSON answer against ``model_cls``.

    Tolerates a Markdown code fence around the JSON (models add one even
    when told not to); anything else invalid raises :class:`LlmOutputError`.
    """
    stripped = _CODE_FENCE.sub("", text.strip())
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LlmOutputError(f"output is not valid JSON: {exc}", raw=text) from exc
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise LlmOutputError(f"output does not match the schema: {exc}", raw=text) from exc
