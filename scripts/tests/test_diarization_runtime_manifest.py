"""The committed diarization-runtime manifest is derived from `uv.lock` by
`scripts/gen_diarization_runtime.py`; these pin the derivation itself so a
lock update (or a hand edit of the generated module) fails here, not on an
operator's first-run download."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_diarization_runtime as gen  # noqa: E402


def test_the_committed_manifest_matches_the_lock() -> None:
    rendered = gen.render(gen.compute_wheels())
    assert gen.OUTPUT_FILE.read_text(encoding="utf-8") == rendered, (
        "run: uv run scripts/gen_diarization_runtime.py"
    )


def test_the_cuda_overrides_track_the_locked_torch_versions() -> None:
    wheels = {wheel.name: wheel for wheel in gen.compute_wheels()}
    lock = gen.Lock(gen.LOCK_FILE)
    for name, override in gen.CUDA_OVERRIDES.items():
        locked = lock.entry(name, None, frozenset())["version"]
        assert override.version.split("+", 1)[0] == locked
        assert wheels[name] is override
        assert override.version.endswith(f"+{gen.TORCH_CUDA_VARIANT}")


def test_conflict_extra_markers_evaluate_against_the_active_extras() -> None:
    active = frozenset({"extra-13-transcription-llm-cpu"})
    assert gen._evaluate_marker("extra == 'extra-13-transcription-llm-cpu'", active)
    assert not gen._evaluate_marker("extra == 'extra-13-transcription-llm-cuda'", active)
    # uv's "never" spelling: both conflicting extras at once.
    never = (
        "sys_platform == 'linux' or (extra == 'extra-13-transcription-llm-cpu' "
        "and extra == 'extra-13-transcription-llm-cuda')"
    )
    assert not gen._evaluate_marker(never, active)
    assert gen._evaluate_marker("sys_platform == 'win32'", active)
    assert gen._evaluate_marker(None, active)


def test_only_the_extra_s_own_packages_are_fetched() -> None:
    """Nothing the baked environment already carries may be re-fetched:
    the runtime goes to the front of `sys.path`, so a duplicate would
    shadow the installed copy."""
    lock = gen.Lock(gen.LOCK_FILE)
    baked = gen._closure(lock, frozenset(gen.BASE_EXTRAS))
    fetched = {gen.canonicalize_name(wheel.name) for wheel in gen.compute_wheels()}
    assert not fetched & set(baked)
    assert {"pyannote-audio", "torch", "torchaudio", "speechbrain"} <= fetched
