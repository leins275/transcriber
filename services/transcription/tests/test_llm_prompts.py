"""Prompt-builder unit tests for language pinning.

The per-meeting builders take an explicit target language and turn it
into a hard directive naming the output language; anything outside the
supported {ru, en} set falls back to today's soft mirror rule. Pure string
assertions -- no model, no filesystem (NFR-1).
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from typing import Any

import pytest

from transcription.llm import prompts
from transcription.llm.prompts import (
    Message,
    chunk_summary_messages,
    merge_summaries_messages,
    summary_messages,
)

SOFT_RULE = "same language the transcript is written in"
TERMS_CLAUSE = "Keep technical terms, product names and code identifiers as they appear."

TRANSCRIPT = "[0:01] A: hello"

# The three per-meeting builders paired with their non-language arguments; the
# language is threaded in as a keyword by the tests.
BUILDERS: dict[str, tuple[Callable[..., list[Message]], tuple[Any, ...]]] = {
    "summary_messages": (summary_messages, (TRANSCRIPT,)),
    "chunk_summary_messages": (chunk_summary_messages, (TRANSCRIPT, 0, 3)),
    "merge_summaries_messages": (merge_summaries_messages, (["part one"],)),
}

BUILDER_NAMES = sorted(BUILDERS)


def build(name: str, **kwargs: Any) -> list[Message]:
    """Call one builder positionally, exactly as production code does."""
    builder, args = BUILDERS[name]
    return builder(*args, **kwargs)


def system_content(messages: list[Message]) -> str:
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# ------------------------------------------------------------------- FR-1


@pytest.mark.parametrize("name", BUILDER_NAMES)
@pytest.mark.parametrize(
    ("language", "directive"),
    [
        ("ru", "Write your entire answer in Russian."),
        ("en", "Write your entire answer in English."),
    ],
)
def test_supported_language_pins_the_output_language(
    name: str, language: str, directive: str
) -> None:
    content = system_content(build(name, language=language))
    assert directive in content
    assert SOFT_RULE not in content


@pytest.mark.parametrize("name", BUILDER_NAMES)
@pytest.mark.parametrize(
    ("variant", "canonical"), [("RU", "ru"), ("En", "en"), ("  ru ", "ru"), ("EN", "en")]
)
def test_language_code_is_normalized(name: str, variant: str, canonical: str) -> None:
    assert system_content(build(name, language=variant)) == system_content(
        build(name, language=canonical)
    )


@pytest.mark.parametrize("name", BUILDER_NAMES)
@pytest.mark.parametrize("language", ["ru", "en", None, "de"])
def test_technical_terms_clause_survives_in_both_modes(name: str, language: str | None) -> None:
    assert TERMS_CLAUSE in system_content(build(name, language=language))


# ------------------------------------------------------------------- FR-3


@pytest.mark.parametrize("name", BUILDER_NAMES)
@pytest.mark.parametrize("language", [None, "de", "", "ru-RU", 42, ["ru"], {"code": "ru"}])
def test_unsupported_language_falls_back_to_the_soft_rule(name: str, language: Any) -> None:
    content = system_content(build(name, language=language))
    assert prompts._LANGUAGE_RULE in content
    assert "Write your entire answer in" not in content


@pytest.mark.parametrize("name", BUILDER_NAMES)
def test_omitting_the_language_matches_an_explicit_none(name: str) -> None:
    assert system_content(build(name)) == system_content(build(name, language=None))


@pytest.mark.parametrize("name", BUILDER_NAMES)
def test_language_is_a_keyword_parameter_with_a_default(name: str) -> None:
    """``llm/summarize.py`` calls ``chunk_summary_messages(chunk, i, len(chunks))``
    with its non-language arguments positionally; the language parameter must
    not break that call shape."""
    parameter = inspect.signature(BUILDERS[name][0]).parameters["language"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ------------------------------------------------------------------- FR-4


@pytest.mark.parametrize("name", BUILDER_NAMES)
def test_every_summary_prompt_asks_for_action_items(name: str) -> None:
    """Action items are a summary section now (extraction was retired), and
    the map-reduce path must carry them too or a long transcript's reduce
    would drop them."""
    messages = build(name)
    assert messages[1]["role"] == "user"
    assert "action items" in messages[1]["content"].casefold()


# ------------------------------------------------------------------- FR-2


def test_prompts_module_imports_nothing_from_the_package_and_does_no_io() -> None:
    """The module's documented contract: pure string assembly."""
    tree = ast.parse(inspect.getsource(prompts))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    forbidden = {
        "os",
        "io",
        "json",
        "pathlib",
        "shutil",
        "socket",
        "subprocess",
        "sqlite3",
        "tempfile",
        "urllib",
        "requests",
        "httpx",
    }
    for name in imported:
        root = name.split(".")[0]
        assert root != "transcription", f"prompts.py imports from the package: {name}"
        assert root not in forbidden, f"prompts.py imports an I/O module: {name}"

    source = inspect.getsource(prompts)
    assert "open(" not in source
