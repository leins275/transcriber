"""Stage everything the installer ships beside the executable.

Replaces `build_pyenv.py`. That script baked a relocatable Python runtime
(~420 MB) because the product was half a Python service; this one collects a
much smaller set of native pieces, because the product is one binary plus the
libraries it loads:

- the engine libraries cargo just built (`whisper.dll`, `llama.dll`, the two
  ggml libraries) and the CPU backend modules,
- `ffmpeg.exe`, which decodes every recording the vault accepts,
- ONNX Runtime and the two diarization models.

The first group comes out of the build tree. The rest are third-party binaries
pinned by URL, size and SHA-256 in `crates/fetcher/src/manifest.rs`; they are
downloaded once into a cache directory and reused, so a rebuild is not a
re-download. The digests are what make that cache safe to trust.

Everything lands under `apps/desktop/src-tauri/resources/engine/`, which
`tauri.conf.json` ships into the application folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = REPO_ROOT / "apps/desktop/src-tauri/resources/engine"
MANIFEST = REPO_ROOT / "crates/fetcher/src/manifest.rs"
DEFAULT_CACHE = REPO_ROOT / ".cache/engine-payload"

# The libraries the app links against, which sit beside the executable.
ENGINE_LIBRARIES = (
    "whisper.dll",
    "llama.dll",
    "llama-common.dll",
    "ggml.dll",
    "ggml-base.dll",
)

# The CPU backend modules ggml loads at runtime. They are *not* beside the
# executable: `GGML_BACKEND_DL` means ggml scans a directory for them, and
# keeping them in their own one is what lets the optional GPU payload be a
# separate directory that survives an app update.
# Must match `engine::backends::ENGINE_BACKENDS_DIR`, which is where the
# app looks for them at startup.
BACKENDS_SUBDIR = "runtime/engine/backends"

# Must match `engine::models::onnx_runtime_library` and
# `Config::diarization_model_path`, for the same reason.
ONNX_SUBDIR = "runtime/onnx"
MODELS_SUBDIR = "models/diarization"

# The tracked placeholder that keeps the resources directory present on a
# clean checkout. Staging replaces the directory wholesale, which is right for
# a build artifact -- but this one file is *tracked*, and removing it leaves
# `git status` reporting a deletion nobody made and breaks the next
# `cargo build` (tauri-build resolves bundle.resources on every build, and a
# missing source path fails the crate compile).
GITKEEP = ".gitkeep"


class StageError(RuntimeError):
    """Aborts the stage with an operator-facing message."""


@dataclass(frozen=True)
class Pin:
    """One third-party payload, as `manifest.rs` pins it."""

    name: str
    file_name: str
    url: str
    size: int
    sha256: str


def read_pins(manifest: Path = MANIFEST) -> dict[str, Pin]:
    """Parse the pin table out of `manifest.rs`.

    Read rather than duplicated: the engine and the installer must agree on
    exactly which artifact is meant, and two hand-maintained copies of a digest
    are two chances to be wrong.
    """
    text = manifest.read_text(encoding="utf-8")
    pins: dict[str, Pin] = {}
    for block in re.finditer(r"PinnedPayload\s*\{(.*?)\n    \}", text, re.S):
        body = block.group(1)

        def field(key: str) -> str | None:
            found = re.search(rf'{key}:\s*"((?:[^"\\]|\\.)*)"', body, re.S)
            if not found:
                return None
            # A Rust line continuation: backslash, newline, and the
            # indentation that follows it.
            return re.sub(r"\\\s*\n\s*", "", found.group(1))

        name = field("name")
        size = re.search(r"size:\s*([0-9_]+)", body)
        if not (name and size):
            continue
        pins[name] = Pin(
            name=name,
            file_name=field("file_name") or "",
            url=field("url") or "",
            size=int(size.group(1).replace("_", "")),
            sha256=field("sha256") or "",
        )
    if not pins:
        raise StageError(f"no pins found in {manifest}")
    return pins


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(pin: Pin, cache: Path) -> Path:
    """The pinned file, downloaded once and verified every time.

    Verifying on a cache hit as well as after a download is deliberate: a
    truncated or tampered cache entry is exactly the case a digest is for, and
    it costs a read of a file that is already on disk.
    """
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / pin.file_name

    if target.is_file() and target.stat().st_size == pin.size:
        if sha256_of(target) == pin.sha256:
            return target
        target.unlink()

    print(f"  fetching {pin.name} ({pin.size / 1e6:.0f} MB)")
    partial = target.with_suffix(target.suffix + ".partial")
    with urllib.request.urlopen(pin.url) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out)

    actual_size = partial.stat().st_size
    if actual_size != pin.size:
        partial.unlink()
        raise StageError(f"{pin.name}: expected {pin.size} bytes, got {actual_size}")
    actual = sha256_of(partial)
    if actual != pin.sha256:
        partial.unlink()
        raise StageError(f"{pin.name}: digest mismatch\n  expected {pin.sha256}\n  got      {actual}")

    partial.replace(target)
    return target


def find_in_zip(archive: Path, member_name: str) -> bytes:
    """One named member out of an archive, wherever it sits in the tree."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.rsplit("/", 1)[-1] == member_name:
                return zf.read(member)
    raise StageError(f"{archive.name} has no member named {member_name}")


def find_build_outputs(profile: str) -> tuple[Path, Path]:
    """The directory holding the built engine libraries, and the backends.

    Both are produced by the sys crates' build scripts, which stage the
    libraries next to the profile's binaries and leave the backend modules in
    their own output directory.
    """
    profile_dir = REPO_ROOT / "target" / profile
    if not profile_dir.is_dir():
        raise StageError(f"{profile_dir} does not exist -- build the workspace first")

    missing = [name for name in ENGINE_LIBRARIES if not (profile_dir / name).is_file()]
    if missing:
        raise StageError(
            f"{profile_dir} is missing {', '.join(missing)} -- "
            "run `cargo build` for this profile first"
        )

    candidates = sorted(
        (profile_dir / "build").glob("llama-cpp-sys-2-*/out/backends"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise StageError("no ggml backend modules found -- was the workspace built?")
    return profile_dir, candidates[0]


def stage(profile: str, cache: Path, resources: Path = RESOURCES) -> dict[str, object]:
    """Assemble the payload; returns a manifest of what was staged."""
    pins = read_pins()
    profile_dir, backends_src = find_build_outputs(profile)

    placeholder = None
    if (resources / GITKEEP).is_file():
        placeholder = (resources / GITKEEP).read_bytes()
    if resources.exists():
        shutil.rmtree(resources)
    (resources / BACKENDS_SUBDIR).mkdir(parents=True)
    (resources / ONNX_SUBDIR).mkdir(parents=True)
    (resources / MODELS_SUBDIR).mkdir(parents=True)

    staged: list[str] = []

    for name in ENGINE_LIBRARIES:
        shutil.copy2(profile_dir / name, resources / name)
        staged.append(name)

    backends = sorted(backends_src.glob("ggml-*.dll"))
    if not backends:
        raise StageError(f"{backends_src} holds no backend modules")
    for backend in backends:
        shutil.copy2(backend, resources / BACKENDS_SUBDIR / backend.name)
        staged.append(f"{BACKENDS_SUBDIR}/{backend.name}")

    # ffmpeg ships as one executable out of a zip full of tools we never call.
    ffmpeg = resources / "ffmpeg.exe"
    ffmpeg.write_bytes(find_in_zip(fetch(pins["ffmpeg"], cache), "ffmpeg.exe"))
    staged.append("ffmpeg.exe")

    onnx = resources / ONNX_SUBDIR / "onnxruntime.dll"
    onnx.write_bytes(find_in_zip(fetch(pins["onnxruntime"], cache), "onnxruntime.dll"))
    staged.append(f"{ONNX_SUBDIR}/onnxruntime.dll")

    for pin_name in ("diarization-segmentation", "diarization-embedding"):
        pin = pins[pin_name]
        target = resources / MODELS_SUBDIR / pin.file_name
        shutil.copy2(fetch(pin, cache), target)
        staged.append(f"{MODELS_SUBDIR}/{pin.file_name}")

    if placeholder is not None:
        (resources / GITKEEP).write_bytes(placeholder)

    total = sum(path.stat().st_size for path in resources.rglob("*") if path.is_file())
    manifest = {
        "profile": profile,
        "files": staged,
        "total_bytes": total,
    }
    (resources / "payload-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="release", help="cargo profile to stage from")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)

    try:
        manifest = stage(args.profile, args.cache)
    except StageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"staged {len(manifest['files'])} files, {manifest['total_bytes'] / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
