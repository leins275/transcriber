"""Tests for the layered configuration loader (FR-16, FR-9)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from transcription.config import Config, ConfigError, load_config


def _write_config(app_dir: Path, data: dict[str, object]) -> Path:
    config_path = app_dir / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def test_precedence_override_wins(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"model_path": "A"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "TRANSCRIBER_MODEL_PATH": "B"}

    cfg = load_config(env=env, overrides={"model_path": "C"})

    assert cfg.model_path == "C"


def test_precedence_env_wins_without_override(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"model_path": "A"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "TRANSCRIBER_MODEL_PATH": "B"}

    cfg = load_config(env=env)

    assert cfg.model_path == "B"


def test_precedence_config_file_wins_without_env_or_override(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"model_path": "A"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.model_path == "A"


def test_missing_config_file_is_not_an_error(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.model_path == str(tmp_app_dir / "models")


def test_unknown_key_in_config_file_is_ignored(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"some_unknown_future_key": "whatever"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert not hasattr(cfg, "some_unknown_future_key")


def test_vault_root_key_seeds_allowed_roots(tmp_app_dir: Path) -> None:
    vault = tmp_app_dir / "vault"
    vault.mkdir()
    _write_config(tmp_app_dir, {"vault_root": str(vault)})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert Path(vault).resolve() in [Path(r).resolve() for r in cfg.allowed_roots]


def test_app_dir_selects_default_db_and_model_paths(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.db_path == str(tmp_app_dir / "data" / "jobs.sqlite3")
    assert cfg.model_path == str(tmp_app_dir / "models")


def test_token_is_auto_generated_and_differs_between_loads(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg1 = load_config(env=env)
    cfg2 = load_config(env=env)

    assert len(cfg1.token) >= 32
    assert cfg1.token != cfg2.token


def test_allowed_roots_splits_on_pathsep(tmp_app_dir: Path) -> None:
    root_a = tmp_app_dir / "a"
    root_b = tmp_app_dir / "b"
    root_a.mkdir()
    root_b.mkdir()
    env = {
        "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
        "TRANSCRIBER_ALLOWED_ROOTS": os.pathsep.join([str(root_a), str(root_b)]),
    }

    cfg = load_config(env=env)

    assert str(root_a) in cfg.allowed_roots
    assert str(root_b) in cfg.allowed_roots


def test_malformed_json_raises_config_error_naming_file(tmp_app_dir: Path) -> None:
    config_path = tmp_app_dir / "config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=env)

    assert str(config_path) in str(exc_info.value)


def test_public_contains_no_secrets(tmp_app_dir: Path) -> None:
    env = {
        "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
        "TRANSCRIBER_PROVIDER_API_KEY": "sk-super-secret-key",
    }

    cfg = load_config(env=env)
    public = cfg.public()

    assert "provider" in public
    assert "model" in public
    assert "device" in public
    assert "token" not in public
    assert cfg.token not in json.dumps(public)
    assert "sk-super-secret-key" not in json.dumps(public)


def test_api_key_in_overrides_raises_config_error(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError):
        load_config(env=env, overrides={"provider_api_key": "sk-from-argv"})


def test_real_desktop_config_json_model_shape_unpacks_into_flat_fields(
    tmp_app_dir: Path,
) -> None:
    """Field report / Bug 3 regression: F3's real config.json schema
    (docs/config-contract.md) nests the model choice as
    ``"model": {"id": ..., "path": ...}``. Before the fix, the generic
    known-field passthrough copied this dict verbatim onto `Config.model`
    (a field that must be a plain string), which surfaced many calls later
    as an unhandled `sqlite3.ProgrammingError` on every job submission
    (HTTP 500 `internal`) -- this is the exact config.json shape the
    installed app writes.
    """
    _write_config(
        tmp_app_dir,
        {
            "schema_version": 1,
            "meetings_root": str(tmp_app_dir),
            "service": {"base_url": None},
            "model": {
                "id": "faster-whisper-large-v3",
                "path": r"C:\Apps\Transcriber\models\faster-whisper-large-v3",
            },
        },
    )
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.model == "faster-whisper-large-v3"
    assert cfg.model_path == r"C:\Apps\Transcriber\models\faster-whisper-large-v3"


def test_real_desktop_config_json_with_null_model_id_and_path_keeps_defaults(
    tmp_app_dir: Path,
) -> None:
    """The first-run/no-override shape (`model.id`/`model.path` both
    `null`, exactly what the installer and the app's first-run wizard
    write) must leave `Config.model` at its string default -- never a
    `dict` -- and `Config.model_path` at its own computed default."""
    _write_config(
        tmp_app_dir,
        {
            "schema_version": 1,
            "meetings_root": str(tmp_app_dir),
            "service": {"base_url": None},
            "model": {"id": None, "path": None},
        },
    )
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert isinstance(cfg.model, str)
    assert cfg.model == "large-v3"
    assert cfg.model_path == str(tmp_app_dir / "models")


def test_default_model_is_large_v3(tmp_app_dir: Path) -> None:
    """E5: the default local model must be large-v3, never a smaller size
    that silently downloads over the network."""
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.model == "large-v3"


def test_host_ignores_env_override(tmp_app_dir: Path) -> None:
    """E4: the bind host is hard-pinned; `TRANSCRIBER_HOST` must be ignored."""
    env = {
        "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
        "TRANSCRIBER_HOST": "0.0.0.0",  # noqa: S104 -- proving this value is *ignored*
    }

    cfg = load_config(env=env)

    assert cfg.host == "127.0.0.1"


def test_host_ignores_config_file_value(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"host": "0.0.0.0"})  # noqa: S104 -- proving this is ignored
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.host == "127.0.0.1"


def test_host_ignores_explicit_override(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env, overrides={"host": "0.0.0.0"})  # noqa: S104 -- proving this is ignored

    assert cfg.host == "127.0.0.1"


def test_config_is_frozen_dataclass(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert isinstance(cfg, Config)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        cfg.model_path = "elsewhere"  # type: ignore[misc]


# -- diarization -------------------------------------------------------------


def test_diarization_defaults_are_off_and_pinned_to_pyannote(tmp_app_dir: Path) -> None:
    cfg = load_config(env={"TRANSCRIBER_APP_DIR": str(tmp_app_dir)})

    assert cfg.diarize is False
    assert cfg.diarization_model == "pyannote/speaker-diarization-3.1"
    assert cfg.diarization_model_path == ""
    assert cfg.diarization_min_speakers is None
    assert cfg.diarization_max_speakers is None
    assert cfg.hf_token is None


def test_diarize_parses_from_the_config_file_and_env(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"diarize": True, "diarization_max_speakers": 4})

    cfg = load_config(env={"TRANSCRIBER_APP_DIR": str(tmp_app_dir)})
    assert cfg.diarize is True
    assert cfg.diarization_max_speakers == 4

    # Env is string-shaped and overrides the file.
    cfg = load_config(
        env={
            "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
            "TRANSCRIBER_DIARIZE": "false",
            "TRANSCRIBER_DIARIZATION_MIN_SPEAKERS": "2",
        }
    )
    assert cfg.diarize is False
    assert cfg.diarization_min_speakers == 2


def test_hf_token_comes_from_the_environment_chain(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "HF_TOKEN": "hf_from_env"}
    assert load_config(env=env).hf_token == "hf_from_env"  # noqa: S105 -- test fixture

    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "HUGGING_FACE_HUB_TOKEN": "hf_hub"}
    assert load_config(env=env).hf_token == "hf_hub"  # noqa: S105 -- test fixture

    # The TRANSCRIBER_-prefixed form wins over the generic ones.
    env = {
        "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
        "TRANSCRIBER_HF_TOKEN": "hf_specific",
        "HF_TOKEN": "hf_generic",
    }
    assert load_config(env=env).hf_token == "hf_specific"  # noqa: S105 -- test fixture


def test_hf_token_cannot_be_supplied_via_overrides(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError):
        load_config(env=env, overrides={"hf_token": "hf_from_argv"})


def test_public_reports_diarization_but_never_the_token(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "HF_TOKEN": "hf_secret"}

    public = load_config(env=env).public()

    assert public["diarize"] is False
    assert public["diarization_model"] == "pyannote/speaker-diarization-3.1"
    assert "hf_secret" not in json.dumps(public)
