"""Attribution audit and provider-isolation guard (FR-4, FR-13).

Static checks over the tree: NOTICE names Vexa and every adapted origin path,
the four adapted files carry both the Apache-2.0 header and a comment naming
their vexa origin, no module outside `providers/`/`config.py` references a
concrete provider library, and no source file shells out.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "transcription"
PACKAGE_ROOT = SRC_ROOT.parent.parent
NOTICE_PATH = SRC_ROOT.parent.parent / "NOTICE"

ADAPTED_FILES = [
    SRC_ROOT / "errors.py",
    SRC_ROOT / "filters.py",
    SRC_ROOT / "providers" / "local_whisper.py",
    PACKAGE_ROOT / "tests" / "test_api_contract.py",
]

# Strings that would tie a non-provider module to a concrete provider library
# (FR-4): only `providers/`, `llm/` and `config.py` may reference these.
# (`openai` also catches the `openai_compat` engine name; modules outside
# `llm/` reach engine names through `transcription.llm`'s constants instead.)
_PROVIDER_LIBRARY_STRINGS = ("litellm", "faster_whisper", "openai", "groq", "llama_cpp")

# Real subprocess/shell usage, not incidental prose mentioning the word.
_SHELL_TRUE = re.compile(r"shell\s*=\s*True")
_OS_SYSTEM = re.compile(r"os\.system\s*\(")
_SUBPROCESS_CALL = re.compile(r"subprocess\.\w")


def _all_source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _isolation_scope_files() -> list[Path]:
    providers_dir = SRC_ROOT / "providers"
    llm_dir = SRC_ROOT / "llm"
    config_py = SRC_ROOT / "config.py"
    return [
        path
        for path in _all_source_files()
        if providers_dir not in path.parents and llm_dir not in path.parents and path != config_py
    ]


def test_notice_names_vexa_and_license() -> None:
    assert NOTICE_PATH.is_file(), "NOTICE must exist at the package root"
    text = NOTICE_PATH.read_text(encoding="utf-8")
    assert "Vexa" in text
    assert "Vexa-ai/vexa" in text
    assert "Apache License, Version 2.0" in text


def test_notice_lists_every_adapted_origin_path() -> None:
    text = NOTICE_PATH.read_text(encoding="utf-8")
    for adapted in ADAPTED_FILES:
        rel = adapted.relative_to(PACKAGE_ROOT).as_posix()
        assert rel in text, f"NOTICE is missing an entry for {rel}"


def test_adapted_files_carry_apache_header_and_origin_comment() -> None:
    # Source files carry a full copyright/license header block; the test file
    # (T13) carries the lighter "Test pattern adapted from Vexa" comment --
    # both forms name the Apache-2.0 license and the vexa origin, which is
    # what FR-13 actually requires.
    for path in ADAPTED_FILES:
        text = path.read_text(encoding="utf-8")
        head = "\n".join(text.splitlines()[:15])
        assert re.search(r"Apache", head) and "2.0" in head, (
            f"{path} is missing an Apache-2.0 reference in its header"
        )
        assert "Vexa" in head, f"{path} is missing a Vexa attribution comment"
        assert re.search(r"origin:", head), (
            f"{path} is missing a comment naming its vexa origin path"
        )


def test_provider_libraries_are_confined_to_the_provider_seam() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _isolation_scope_files():
        text = path.read_text(encoding="utf-8")
        hits = [needle for needle in _PROVIDER_LIBRARY_STRINGS if needle in text]
        if hits:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = hits

    assert not offenders, (
        f"provider library names leaked outside providers/ and config.py (FR-4): {offenders}"
    )


def test_no_shell_escapes_anywhere_in_src() -> None:
    offenders: list[str] = []
    for path in _all_source_files():
        text = path.read_text(encoding="utf-8")
        if _SHELL_TRUE.search(text) or _OS_SYSTEM.search(text) or _SUBPROCESS_CALL.search(text):
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))

    assert not offenders, f"shell escape found in: {offenders}"
