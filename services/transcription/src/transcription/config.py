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
_SECRET_KEYS = frozenset({"provider_api_key", "token"})

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
    cloud_model: str | None = None
    provider_api_key: str | None = None
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
    max_cloud_upload_mb: int = 25
    job_timeout_sec: int | None = None
    log_level: str = "INFO"

    def public(self) -> dict[str, Any]:
        """What ``/health`` and logs may show: no token, no API key (FR-9)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "cloud_model": self.cloud_model,
            "language": self.language,
            "filter_hallucinations": self.filter_hallucinations,
            "max_cloud_upload_mb": self.max_cloud_upload_mb,
            "log_level": self.log_level,
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

    # provider_api_key: env only (TRANSCRIBER_PROVIDER_API_KEY, else the provider's
    # own conventional env var), never the config file.
    provider_api_key = (
        env.get("TRANSCRIBER_PROVIDER_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("GROQ_API_KEY")
    )
    if provider_api_key is not None:
        values["provider_api_key"] = provider_api_key

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

    if "compute_type" in values and values["compute_type"] in (None, ""):
        values["compute_type"] = None

    if "port" in values:
        values["port"] = int(values["port"])
    if "max_cloud_upload_mb" in values:
        values["max_cloud_upload_mb"] = int(values["max_cloud_upload_mb"])
    if "job_timeout_sec" in values and values["job_timeout_sec"] is not None:
        values["job_timeout_sec"] = int(values["job_timeout_sec"])
    if "filter_hallucinations" in values:
        values["filter_hallucinations"] = _parse_bool(values["filter_hallucinations"])

    token = values.get("token") or secrets.token_hex(32)
    values["token"] = token

    return Config(app_dir=app_dir, config_path=resolved_config_path, **values)
