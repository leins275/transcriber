"""Minimal GGUF header reading + GPU-offload fitting (pure logic).

Just enough of the GGUF v2/v3 container format to answer one question --
how many transformer layers does this model have -- so the llama.cpp
engine can auto-fit ``llm_gpu_layers`` to the free VRAM. Reads only the
metadata key-value block at the top of the file; tensor data is never
touched.

Format reference: ggml's gguf.md. Header: magic ``GGUF``, u32 version,
u64 tensor count, u64 metadata KV count, then KV pairs of
``(string key, u32 value type, value)``.
"""

from __future__ import annotations

import struct
from pathlib import Path

_MAGIC = b"GGUF"

# GGUF metadata value types -> (struct format, size) for the fixed-size ones.
_SCALAR_FORMATS: dict[int, str] = {
    0: "<B",  # uint8
    1: "<b",  # int8
    2: "<H",  # uint16
    3: "<h",  # int16
    4: "<I",  # uint32
    5: "<i",  # int32
    6: "<f",  # float32
    7: "<B",  # bool (1 byte)
    10: "<Q",  # uint64
    11: "<q",  # int64
    12: "<d",  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

# Refuse to walk absurd headers -- a corrupt file must fail fast, not spin.
_MAX_KV_COUNT = 4096
_MAX_STRING_BYTES = 1 << 20


class GgufError(ValueError):
    """The file is not a readable GGUF container."""


def _read_exact(handle: object, count: int) -> bytes:
    data = handle.read(count)  # type: ignore[attr-defined]
    if len(data) != count:
        raise GgufError("truncated GGUF header")
    return bytes(data)


def _read_string(handle: object) -> str:
    (length,) = struct.unpack("<Q", _read_exact(handle, 8))
    if length > _MAX_STRING_BYTES:
        raise GgufError(f"unreasonable GGUF string length {length}")
    return _read_exact(handle, int(length)).decode("utf-8", errors="replace")


def _read_value(handle: object, value_type: int) -> object:
    if value_type in _SCALAR_FORMATS:
        fmt = _SCALAR_FORMATS[value_type]
        (value,) = struct.unpack(fmt, _read_exact(handle, struct.calcsize(fmt)))
        return value
    if value_type == _TYPE_STRING:
        return _read_string(handle)
    if value_type == _TYPE_ARRAY:
        (element_type,) = struct.unpack("<I", _read_exact(handle, 4))
        (count,) = struct.unpack("<Q", _read_exact(handle, 8))
        if count > 10_000_000:
            raise GgufError(f"unreasonable GGUF array length {count}")
        # Skipped wholesale for fixed-size elements; walked for strings.
        if element_type in _SCALAR_FORMATS:
            size = struct.calcsize(_SCALAR_FORMATS[element_type])
            handle.seek(size * int(count), 1)  # type: ignore[attr-defined]
            return None
        if element_type == _TYPE_STRING:
            for _ in range(int(count)):
                _read_string(handle)
            return None
        raise GgufError(f"unknown GGUF array element type {element_type}")
    raise GgufError(f"unknown GGUF value type {value_type}")


def read_block_count(model_file: Path) -> int | None:
    """The model's transformer layer count (``<arch>.block_count``), or
    ``None`` when the header cannot be read -- callers degrade to no
    offload rather than failing the job."""
    try:
        with model_file.open("rb") as handle:
            if _read_exact(handle, 4) != _MAGIC:
                return None
            (version,) = struct.unpack("<I", _read_exact(handle, 4))
            if version < 2:
                return None
            _tensor_count = struct.unpack("<Q", _read_exact(handle, 8))
            (kv_count,) = struct.unpack("<Q", _read_exact(handle, 8))
            if kv_count > _MAX_KV_COUNT:
                return None

            values: dict[str, object] = {}
            for _ in range(int(kv_count)):
                key = _read_string(handle)
                (value_type,) = struct.unpack("<I", _read_exact(handle, 4))
                value = _read_value(handle, value_type)
                if key == "general.architecture" or key.endswith(".block_count"):
                    values[key] = value
                arch = values.get("general.architecture")
                if isinstance(arch, str):
                    block_count = values.get(f"{arch}.block_count")
                    if isinstance(block_count, int) and block_count > 0:
                        return block_count
    except (OSError, GgufError, struct.error):
        return None
    return None


# Working memory the offload plan always leaves free on the GPU: KV cache,
# compute buffers and allocator fragmentation, none of which scale with the
# number of offloaded layers in a way this estimate could see.
VRAM_RESERVE_BYTES = 2_000_000_000
# ...plus this fraction of the free VRAM, so a nearly-full card still keeps
# proportional headroom.
VRAM_RESERVE_FRACTION = 0.05


def fit_gpu_layers(free_vram_bytes: int, model_file_bytes: int, block_count: int) -> int:
    """How many whole layers fit on the GPU: ``-1`` for all of them
    (llama.cpp's "everything, output layer included"), else a count.

    The per-layer cost is approximated as an even split of the file over
    ``block_count + 1`` (the +1 standing in for the embedding/output
    tensors) -- coarse, but the reserve absorbs the error, and a layer too
    few merely costs a little speed where a layer too many aborts the load.
    """
    if free_vram_bytes <= 0 or model_file_bytes <= 0 or block_count <= 0:
        return 0
    usable = free_vram_bytes - VRAM_RESERVE_BYTES - int(free_vram_bytes * VRAM_RESERVE_FRACTION)
    if usable <= 0:
        return 0
    per_layer = model_file_bytes / (block_count + 1)
    layers = int(usable // per_layer)
    if layers >= block_count + 1:
        return -1
    return max(0, layers)
