"""Layered configuration: defaults < config.json < TRANSCRIBER_* env < explicit overrides.

FR-16 acceptance: ``TRANSCRIBER_MODEL_PATH`` overrides the config file value, and an
explicit override (CLI flag, in ``cli.py``) overrides both.

No module-level filesystem or network access happens at import time (NFR-1); everything
below runs only when :func:`load_config` is called.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# Keys that must never be settable from an argv-shaped override (FR-9): credentials
# come from the environment or the config file only, never from a CLI flag.
_SECRET_KEYS = frozenset({"token", "hf_token"})

_TRUE_STRINGS = frozenset({"1", "true", "yes"})

# Config-file keys this service does not know about get silently ignored (F4 owns the
# file's overall schema); this key is the one exception it reads on its own behalf.
_VAULT_ROOT_KEY = "vault_root"

# F3's config.json schema (docs/config-contract.md) nests the model choice as
# ``"model": {"id": ..., "path": ...}`` -- but this dataclass's own field is
# named `model` too (a flat string, the model id). Bug (field report, 2026-08-22):
# the generic known-field passthrough below used to copy this key's value
# verbatim, so a real installed config.json (which always has this nested
# shape) silently set `Config.model` to a `dict` instead of a string --
# surfacing many calls later as `sqlite3.ProgrammingError: Error binding
# parameter 4: type 'dict' is not supported` in `ledger.insert_job`, reported
# to the operator as an unhelpful HTTP 500 `internal` on every job
# submission. `model` is special-cased here the same way `vault_root` is,
# unpacking `id`/`path` into the flat `model`/`model_path` fields instead of
# being copied through the generic loop.
_MODEL_KEY = "model"

# The operator's language universe is exactly these two (F2 FR-3); anything
# else -- from the config file, `TRANSCRIBER_LANGUAGE`, or `--language` -- is
# a configuration error rather than a value handed to the decoder.
_ALLOWED_LANGUAGES = ("ru", "en")


class ConfigError(Exception):
    """Raised when configuration cannot be loaded (e.g. malformed config file)."""


@dataclass(frozen=True, kw_only=True)
class Config:
    """Fully resolved, immutable configuration for one process run."""

    app_dir: Path
    config_path: Path
    model: str = "large-v3"
    model_path: str = ""
    device: str = "auto"
    compute_type: str | None = None
    provider: str = "local"
    db_path: str = ""
    allowed_roots: tuple[str, ...] = field(default_factory=tuple)
    # Hard-pinned (FR-9): `load_config` excludes this key from the config
    # file / env / overrides layers, so this default is the only value it
    # can ever have.
    host: str = "127.0.0.1"
    port: int = 0
    token: str = ""
    language: str | None = None
    filter_hallucinations: bool = True
    # Word-level timestamps feed the utterance re-segmentation pass (and are
    # kept in transcript.json for future diarization); on by default because
    # segmentation quality depends on them.
    word_timestamps: bool = True
    # Batched decoding on CUDA (faster_whisper.BatchedInferencePipeline).
    # <= 1 disables batching; ignored on CPU, where memory is tighter and
    # the sequential path is the tested one.
    batch_size: int = 8
    # VAD: how much silence ends a speech chunk. Lower = segments break at
    # conversational pauses instead of bridging them.
    vad_min_silence_ms: int = 500
    # Re-segmentation: a pause between words at least this long starts a new
    # segment (utterance), in addition to sentence-ending punctuation.
    resegment_gap_sec: float = 0.6
    # Speaker diarization (pyannote): off by default -- it needs the optional
    # `diarization` extra installed and, for the hub-hosted gated model, a
    # Hugging Face token. A per-job `diarize` flag overrides this default.
    diarize: bool = False
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    # A local snapshot directory (containing config.yaml) for offline loads;
    # empty means load `diarization_model` from the Hugging Face hub/cache.
    diarization_model_path: str = ""
    # Optional speaker-count bounds passed through to the pipeline; `None`
    # lets pyannote estimate the count itself.
    diarization_min_speakers: int | None = None
    diarization_max_speakers: int | None = None
    # Hugging Face access token for the gated pyannote models. Environment
    # only (TRANSCRIBER_HF_TOKEN, else HF_TOKEN/HUGGING_FACE_HUB_TOKEN),
    # never argv (FR-9).
    hf_token: str | None = None
    job_timeout_sec: int | None = None
    log_level: str = "INFO"
    # --- Local LLM (summaries / action items / facts) ---
    # Display/model id: it names the local GGUF snapshot the built-in
    # llama.cpp engine loads.
    llm_model: str = "qwen3.6-35b-a3b"
    # Where GGUF snapshots live; empty means `<app_dir>/models/llm`.
    llm_model_path: str = ""
    # Hugging Face repo + revision the in-app GGUF download fetches, and the
    # one file it selects out of that repo (GGUF repos carry many quants;
    # downloading all of them would be hundreds of GB). The Qwen org itself
    # publishes no GGUF; ggml-org (the llama.cpp maintainers) is the
    # canonical conversion. Revision pinned so verification always has a
    # concrete digest set to compare against, like the whisper snapshot.
    llm_model_repo: str = "ggml-org/Qwen3.6-35B-A3B-GGUF"
    llm_model_revision: str = "baec3ebee244827cda0f4557eafa8b28f7545fa6"
    llm_model_file: str = "Qwen3.6-35B-A3B-Q4_K_M.gguf"
    # Context window the chunker budgets against and llama.cpp allocates.
    llm_ctx: int = 16384
    # -1 = auto-fit: put as many whole layers on the GPU as the free VRAM
    # holds (measured via NVML, layer count read from the GGUF header) and
    # leave the rest on CPU. 0 disables offload; a positive number pins the
    # layer count. On a CPU-only llama.cpp build the knob is inert either
    # way, so the auto default is safe everywhere.
    llm_gpu_layers: int = -1
    # None lets llama.cpp pick (physical cores).
    llm_threads: int | None = None
    llm_temperature: float = 0.3
    llm_max_output_tokens: int = 4096
    # Extra output budget for the reasoning model's <think> block on
    # free-text calls (summaries), on top of llm_max_output_tokens, so the
    # thinking cannot eat the whole answer budget. Grammar-constrained JSON
    # calls suppress thinking and keep the plain cap.
    llm_think_headroom_tokens: int = 2048
    # Keep the GGUF model resident between LLM jobs. Off by default so the
    # ~20 GB working set is released and never sits next to a loaded whisper
    # model; reloading is mmap-fast.
    llm_keep_loaded: bool = False

    def public(self) -> dict[str, Any]:
        """What ``/health`` and logs may show: no token (FR-9)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
            "filter_hallucinations": self.filter_hallucinations,
            "word_timestamps": self.word_timestamps,
            "diarize": self.diarize,
            "diarization_model": self.diarization_model,
            "batch_size": self.batch_size,
            "log_level": self.log_level,
            "llm_model": self.llm_model,
            "llm_ctx": self.llm_ctx,
            "llm_gpu_layers": self.llm_gpu_layers,
        }


def _resolve_app_dir(env: Mapping[str, str]) -> Path:
    raw = env.get("TRANSCRIBER_APP_DIR")
    if raw:
        return Path(raw)
    return Path(sys.executable).resolve().parent.parent


def _read_config_file(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"failed to read config file {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {config_path} must contain a JSON object")
    return data


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_STRINGS


def _normalize_language(value: object) -> str | None:
    """Normalize a layered ``language`` value, rejecting anything but ru/en.

    Unset and empty (``None``, ``""``, whitespace) mean "no explicit language"
    -- constrained auto-detection, not an error (FR-3).
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in _ALLOWED_LANGUAGES:
        allowed = ", ".join(repr(code) for code in _ALLOWED_LANGUAGES)
        raise ConfigError(
            f"invalid language {str(value)!r}: allowed values are {allowed}, or unset for "
            f"automatic detection"
        )
    return normalized


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    return env.get(f"TRANSCRIBER_{key.upper()}")


def load_config(
    *,
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load configuration: defaults < config file < ``TRANSCRIBER_*`` env < overrides."""
    if env is None:
        env = os.environ
    if overrides is None:
        overrides = {}

    for key in overrides:
        if key in _SECRET_KEYS:
            raise ConfigError(
                f"credentials cannot be supplied via overrides (argv-shaped sources); "
                f"got {key!r} — use an environment variable or the config file instead"
            )

    app_dir = _resolve_app_dir(env)

    if config_path is not None:
        resolved_config_path = Path(config_path)
    else:
        env_config_path = env.get("TRANSCRIBER_CONFIG_PATH")
        resolved_config_path = Path(env_config_path) if env_config_path else app_dir / "config.json"

    file_data = _read_config_file(resolved_config_path)

    # `host` is deliberately excluded from every layer below: the loopback
    # bind is a hard security guarantee (FR-9), not a configurable value --
    # neither the config file, `TRANSCRIBER_HOST`, nor a CLI override may
    # change it, so `Config.host` always keeps its `"127.0.0.1"` default.
    known_fields = {f.name for f in fields(Config)} - {"app_dir", "config_path", "host"}
    values: dict[str, Any] = {}

    # 1. defaults come from the dataclass field defaults themselves — nothing to do
    #    here until a layer actually supplies a value.

    # 2. config file (unknown keys ignored; vault_root and model are
    #    special-cased below -- neither is a plain scalar the generic loop
    #    can copy verbatim).
    for key, value in file_data.items():
        if key in (_VAULT_ROOT_KEY, _MODEL_KEY):
            continue
        if key in known_fields:
            values[key] = value

    allowed_roots: list[str] = list(values.get("allowed_roots") or [])
    vault_root = file_data.get(_VAULT_ROOT_KEY)
    if vault_root:
        allowed_roots.append(str(vault_root))

    # F3's nested `"model": {"id": ..., "path": ...}` (see _MODEL_KEY above):
    # unpack onto the flat `model`/`model_path` fields instead of assigning
    # the dict itself. A `null`/absent `id` or `path` leaves the
    # corresponding field at whatever an earlier layer (never happens here --
    # this is layer 2) or the dataclass default already supplied.
    model_field = file_data.get(_MODEL_KEY)
    if isinstance(model_field, dict):
        model_id = model_field.get("id")
        if model_id:
            values["model"] = str(model_id)
        model_path_value = model_field.get("path")
        if model_path_value:
            values["model_path"] = str(model_path_value)
    elif isinstance(model_field, str) and model_field:
        # Tolerate a plain string too, in case some other writer ever uses a
        # flatter shape -- never silently accept anything else (a list, a
        # number, ...), which is exactly the shape of bug this guards
        # against.
        values["model"] = model_field

    # 3. TRANSCRIBER_* env overrides
    for key in known_fields:
        env_raw = _env_value(env, key)
        if env_raw is None:
            continue
        if key == "allowed_roots":
            allowed_roots = env_raw.split(os.pathsep)
            continue
        values[key] = env_raw

    if "TRANSCRIBER_ALLOWED_ROOTS" in env:
        allowed_roots = env["TRANSCRIBER_ALLOWED_ROOTS"].split(os.pathsep)

    # hf_token: env only, never argv (FR-9) -- the generic TRANSCRIBER_HF_TOKEN
    # pickup above already applied; this adds the huggingface_hub-conventional
    # variables as fallbacks.
    if not values.get("hf_token"):
        hf_token = env.get("HF_TOKEN") or env.get("HUGGING_FACE_HUB_TOKEN")
        if hf_token:
            values["hf_token"] = hf_token

    # 4. explicit overrides (CLI flags) win over everything above
    for key, value in overrides.items():
        if key in known_fields:
            values[key] = value
    if "allowed_roots" in overrides:
        allowed_roots = list(overrides["allowed_roots"])

    values["allowed_roots"] = tuple(allowed_roots)

    if "db_path" not in values or not values["db_path"]:
        values["db_path"] = str(app_dir / "data" / "jobs.sqlite3")
    if "model_path" not in values or not values["model_path"]:
        values["model_path"] = str(app_dir / "models")
    if "llm_model_path" not in values or not values["llm_model_path"]:
        values["llm_model_path"] = str(app_dir / "models" / "llm")

    if "compute_type" in values and values["compute_type"] in (None, ""):
        values["compute_type"] = None

    # Validated after every layer has merged, so a valid override can replace
    # an invalid config-file/env value (and an invalid override always loses).
    if "language" in values:
        values["language"] = _normalize_language(values["language"])

    if "port" in values:
        values["port"] = int(values["port"])
    if "job_timeout_sec" in values and values["job_timeout_sec"] is not None:
        values["job_timeout_sec"] = int(values["job_timeout_sec"])
    if "filter_hallucinations" in values:
        values["filter_hallucinations"] = _parse_bool(values["filter_hallucinations"])
    if "word_timestamps" in values:
        values["word_timestamps"] = _parse_bool(values["word_timestamps"])
    if "batch_size" in values:
        values["batch_size"] = int(values["batch_size"])
    if "vad_min_silence_ms" in values:
        values["vad_min_silence_ms"] = int(values["vad_min_silence_ms"])
    if "resegment_gap_sec" in values:
        values["resegment_gap_sec"] = float(values["resegment_gap_sec"])
    if "diarize" in values:
        values["diarize"] = _parse_bool(values["diarize"])
    for speakers_key in ("diarization_min_speakers", "diarization_max_speakers"):
        if speakers_key in values and values[speakers_key] not in (None, ""):
            values[speakers_key] = int(values[speakers_key])
        elif speakers_key in values:
            values[speakers_key] = None
    for llm_int_key in (
        "llm_ctx",
        "llm_gpu_layers",
        "llm_max_output_tokens",
        "llm_think_headroom_tokens",
    ):
        if llm_int_key in values:
            values[llm_int_key] = int(values[llm_int_key])
    if "llm_threads" in values and values["llm_threads"] not in (None, ""):
        values["llm_threads"] = int(values["llm_threads"])
    elif "llm_threads" in values:
        values["llm_threads"] = None
    if "llm_temperature" in values:
        values["llm_temperature"] = float(values["llm_temperature"])
    if "llm_keep_loaded" in values:
        values["llm_keep_loaded"] = _parse_bool(values["llm_keep_loaded"])

    token = values.get("token") or secrets.token_hex(32)
    values["token"] = token

    return Config(app_dir=app_dir, config_path=resolved_config_path, **values)
