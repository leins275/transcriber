"""Assemble the app folder `tauri dev` runs against.

Under `tauri dev` the executable sits in `target/debug/`, which is not a
sane app folder: it has no `models\\`, no `runtime\\`, no bundled
`ffmpeg.exe`. `TRANSCRIBER_DEV_APP_DIR` exists to point the engine
somewhere else (see `app_paths::DEV_APP_DIR_ENV`); this script builds that
somewhere.

Nothing large is copied. The runtime payload and the whisper weights are
hardlinked from what is already on disk, and the LLM -- 20 GB, usually on
another volume, where a hardlink cannot reach -- stays where it is and is
named by `TRANSCRIBER_LLM_MODEL_PATH` instead.

Idempotent: re-run it after `stage_engine_payload.py` to refresh the DLLs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGED_PAYLOAD = REPO_ROOT / "apps/desktop/src-tauri/resources/engine"
DEFAULT_DEV_DIR = REPO_ROOT / ".devapp"

# `engine::models` derives both from `config.model`, whose default is
# "large-v3"; the VAD name is fixed. Kept as literals rather than parsed out
# of config.rs because a mismatch here fails loudly on the first run, which
# is cheaper than the parsing.
WHISPER_WEIGHTS = "ggml-large-v3.bin"
WHISPER_VAD = "ggml-silero-v5.1.2.bin"
LLM_GGUF = "Qwen3.6-35B-A3B-Q4_K_M.gguf"

# `engine::models::ready_marker` appends this to the payload's own path.
# Note this is NOT the Python service's convention, which wrote a single
# bare `.ready` in the directory -- a Python-era install therefore looks
# un-downloaded to the Rust engine until this marker exists beside the file.
READY_SUFFIX = ".ready"


class DevAppDirError(RuntimeError):
    """Aborts with an operator-facing message."""


def default_llm_dir() -> Path | None:
    """The installed release's LLM folder, if this machine has one."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    candidate = Path(local) / "Transcriber" / "models" / "llm"
    return candidate if (candidate / LLM_GGUF).is_file() else None


WHISPER_SRC_ENV = "TRANSCRIBER_DEV_WHISPER_SRC"


def find_whisper_weights(explicit: Path | None, dev_dir: Path) -> Path | None:
    """Locate a directory holding both whisper payloads.

    `None` means "already in the dev dir, leave it alone" -- which is what
    makes a re-run idempotent once the weights are linked, including on a
    machine whose only copy has since moved.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        from_env = os.environ.get(WHISPER_SRC_ENV)
        if from_env:
            candidates.append(Path(from_env))
        # A Rust-era install already keeps them in exactly this shape.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Transcriber" / "models" / "whisper")
    for candidate in candidates:
        if (candidate / WHISPER_WEIGHTS).is_file():
            return candidate

    already_there = dev_dir / "models" / "whisper"
    if all(
        (already_there / name).is_file() for name in (WHISPER_WEIGHTS, WHISPER_VAD)
    ):
        return None

    raise DevAppDirError(
        f"no directory with {WHISPER_WEIGHTS} found. Pass --whisper-src <dir>, "
        f"set {WHISPER_SRC_ENV}, or let the app download it on first run"
    )


def link_or_copy(src: Path, dst: Path) -> str:
    """Hardlink `src` to `dst`, falling back to a copy across volumes.

    Returns what it did, so the caller can report a copy -- the only case
    that costs real disk.
    """
    if dst.exists():
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        # Different volume, or a filesystem without hardlinks.
        shutil.copy2(src, dst)
        return "copy"


def mark_ready(payload: Path) -> None:
    marker = payload.with_name(payload.name + READY_SUFFIX)
    if not marker.exists():
        marker.write_bytes(b"")


def mirror_payload(dev_dir: Path) -> int:
    """Hardlink the staged engine payload into the dev app folder."""
    if not (STAGED_PAYLOAD / "payload-manifest.json").is_file():
        raise DevAppDirError(
            f"no staged payload in {STAGED_PAYLOAD}. Run "
            "`uv run scripts/stage_engine_payload.py` first"
        )
    linked = 0
    for src in STAGED_PAYLOAD.rglob("*"):
        if not src.is_file() or src.name == ".gitkeep":
            continue
        link_or_copy(src, dev_dir / src.relative_to(STAGED_PAYLOAD))
        linked += 1
    return linked


def build(dev_dir: Path, whisper_src: Path | None, llm_dir: Path | None) -> list[str]:
    """Assemble `dev_dir`. Returns the lines to report to the operator."""
    dev_dir.mkdir(parents=True, exist_ok=True)
    notes = [f"dev app dir: {dev_dir}"]

    notes.append(f"  runtime payload: {mirror_payload(dev_dir)} files from {STAGED_PAYLOAD}")

    weights_src = find_whisper_weights(whisper_src, dev_dir)
    whisper_dst = dev_dir / "models" / "whisper"
    for name in (WHISPER_WEIGHTS, WHISPER_VAD):
        if weights_src is None:
            mark_ready(whisper_dst / name)
            notes.append(f"  {name}: already present")
            continue
        src = weights_src / name
        if not src.is_file():
            raise DevAppDirError(f"{src} is missing -- the VAD model is not optional")
        how = link_or_copy(src, whisper_dst / name)
        mark_ready(whisper_dst / name)
        notes.append(f"  {name}: {how} from {weights_src}")

    if llm_dir is None:
        notes.append("  llm: not linked -- the app will download it on first run")
    else:
        gguf = llm_dir / LLM_GGUF
        if not gguf.is_file():
            raise DevAppDirError(f"{gguf} is missing")
        # The marker has to sit beside the GGUF, and the GGUF is 20 GB that
        # we are deliberately not moving -- so the marker is written into
        # the existing folder rather than into the dev dir.
        mark_ready(gguf)
        notes.append(f"  llm: reused in place from {llm_dir}")

    return notes


def env_exports(dev_dir: Path, llm_dir: Path | None) -> dict[str, str]:
    env = {"TRANSCRIBER_DEV_APP_DIR": str(dev_dir), "TRANSCRIBER_APP_DIR": str(dev_dir)}
    if llm_dir is not None:
        env["TRANSCRIBER_LLM_MODEL_PATH"] = str(llm_dir)
    return env


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-dir", type=Path, default=DEFAULT_DEV_DIR)
    parser.add_argument(
        "--whisper-src",
        type=Path,
        default=None,
        help=f"directory holding {WHISPER_WEIGHTS} and {WHISPER_VAD}",
    )
    parser.add_argument(
        "--llm-dir",
        type=Path,
        default=None,
        help="directory holding the GGUF; defaults to the installed release's copy",
    )
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="emit only KEY=VALUE lines, for a wrapper shell to consume",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="do not wire up an LLM, even if an installed copy is found",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    llm_dir = None if args.no_llm else (args.llm_dir or default_llm_dir())
    try:
        notes = build(args.dev_dir.resolve(), args.whisper_src, llm_dir)
    except DevAppDirError as exc:
        print(f"dev_app_dir: {exc}", file=sys.stderr)
        return 1

    env = env_exports(args.dev_dir.resolve(), llm_dir)
    if args.print_env:
        for key, value in env.items():
            print(f"{key}={value}")
        return 0

    for line in notes:
        print(line)
    print("\nRun the dev app with:")
    for key, value in env.items():
        print(f'  $env:{key} = "{value}"')
    print("  npm --prefix apps/desktop run tauri dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
