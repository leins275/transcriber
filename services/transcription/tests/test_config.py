"""Tests for the layered configuration loader (FR-16, FR-9)."""

from __future__ import annotations

import json
import os
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from transcription import llm_catalog
from transcription.config import Config, ConfigError, load_config

# The cloud-only config surface removed with the cloud STT provider and the
# external OpenAI-compatible LLM engine (FR-3).
_REMOVED_KEYS = (
    "cloud_model",
    "provider_api_key",
    "max_cloud_upload_mb",
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
)


def _write_config(app_dir: Path, data: dict[str, object]) -> Path:
    config_path = app_dir / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def _snapshot(cfg: Config) -> dict[str, Any]:
    """Every resolved field except the per-run random `token`."""
    return {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name != "token"}


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


def test_meetings_root_is_read_as_the_vault_root_fallback(tmp_app_dir: Path) -> None:
    """The app writes `meetings_root`, never `vault_root`; a standalone
    launch (transcriber-mcp) must still find the vault from the file alone."""
    vault = tmp_app_dir / "vault"
    vault.mkdir()
    _write_config(tmp_app_dir, {"meetings_root": str(vault)})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.vault_root == str(vault)
    assert cfg.index_db_path == str(vault / ".transcriber" / "index.sqlite3")


def test_explicit_vault_root_wins_over_the_meetings_root_fallback(tmp_app_dir: Path) -> None:
    vault = tmp_app_dir / "vault"
    vault.mkdir()
    _write_config(tmp_app_dir, {"meetings_root": "ignored", "vault_root": str(vault)})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.vault_root == str(vault)


def test_index_db_defaults_inside_the_vault(tmp_app_dir: Path) -> None:
    """The index travels with its vault: switching vaults switches indexes."""
    vault = tmp_app_dir / "vault"
    vault.mkdir()
    _write_config(tmp_app_dir, {"vault_root": str(vault)})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.index_db_path == str(vault / ".transcriber" / "index.sqlite3")


def test_index_db_falls_back_to_app_dir_without_a_vault(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.index_db_path == str(tmp_app_dir / "data" / "index.sqlite3")


def test_explicit_index_db_path_wins_over_the_vault_default(tmp_app_dir: Path) -> None:
    vault = tmp_app_dir / "vault"
    vault.mkdir()
    _write_config(
        tmp_app_dir,
        {"vault_root": str(vault), "index_db_path": str(tmp_app_dir / "elsewhere.sqlite3")},
    )
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.index_db_path == str(tmp_app_dir / "elsewhere.sqlite3")


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
        "TRANSCRIBER_HF_TOKEN": "hf-super-secret-key",
    }

    cfg = load_config(env=env)
    public = cfg.public()

    assert "provider" in public
    assert "model" in public
    assert "device" in public
    assert "token" not in public
    assert "hf_token" not in public
    assert cfg.token not in json.dumps(public)
    assert "hf-super-secret-key" not in json.dumps(public)


def test_credentials_in_overrides_raise_config_error(tmp_app_dir: Path) -> None:
    """NFR-3: `_SECRET_KEYS` shrank to the two surviving credentials, and both
    are still refused from argv-shaped overrides (FR-9)."""
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    for secret_key in ("token", "hf_token"):
        with pytest.raises(ConfigError):
            load_config(env=env, overrides={secret_key: "sk-from-argv"})


# -- removed cloud surface (FR-3, FR-4) --------------------------------------


def test_config_has_no_cloud_fields(tmp_app_dir: Path) -> None:
    """FR-3: the cloud STT / external-LLM config keys are gone from `Config`."""
    cfg = load_config(env={"TRANSCRIBER_APP_DIR": str(tmp_app_dir)})
    field_names = {f.name for f in fields(Config)}

    for key in _REMOVED_KEYS:
        assert key not in field_names, f"{key} is still a Config field"
        assert not hasattr(cfg, key)


def test_cloud_env_variables_have_no_effect_on_the_loaded_config(tmp_app_dir: Path) -> None:
    """FR-3 acceptance: the removed `TRANSCRIBER_*` pickups and the
    `OPENAI_API_KEY`/`GROQ_API_KEY` fallbacks no longer reach `Config`."""
    baseline = load_config(env={"TRANSCRIBER_APP_DIR": str(tmp_app_dir)})

    cfg = load_config(
        env={
            "TRANSCRIBER_APP_DIR": str(tmp_app_dir),
            "OPENAI_API_KEY": "sk-openai-secret",
            "GROQ_API_KEY": "gsk-groq-secret",
            "TRANSCRIBER_PROVIDER_API_KEY": "sk-provider-secret",
            "TRANSCRIBER_LLM_API_KEY": "sk-llm-secret",
            "TRANSCRIBER_LLM_BASE_URL": "http://127.0.0.1:1234/v1",
            "TRANSCRIBER_LLM_PROVIDER": "openai_compat",
        }
    )

    assert _snapshot(cfg) == _snapshot(baseline)
    rendered = json.dumps(_snapshot(cfg), default=str)
    for leaked in (
        "sk-openai-secret",
        "gsk-groq-secret",
        "sk-provider-secret",
        "sk-llm-secret",
        "http://127.0.0.1:1234/v1",
        "openai_compat",
    ):
        assert leaked not in rendered


def test_public_reports_no_removed_cloud_keys(tmp_app_dir: Path) -> None:
    """FR-3: `/health` and the startup log lose the cloud rows, keeping the
    surviving local-LLM ones."""
    public = load_config(env={"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}).public()

    for key in _REMOVED_KEYS:
        assert key not in public
    assert {"llm_model", "llm_ctx", "llm_gpu_layers"} <= set(public)


def test_installed_config_file_with_removed_cloud_keys_still_loads(tmp_app_dir: Path) -> None:
    """FR-4: an already-installed `config.json` that still carries every removed
    key loads without error -- the keys fall into the unknown-key-ignored path.
    A leftover `"provider": "cloud"` survives as a value and is rejected later,
    per job, by `validate_provider_name` (FR-1) -- never at load time.
    """
    _write_config(
        tmp_app_dir,
        {
            "provider": "cloud",
            "cloud_model": "whisper-large-v3",
            "provider_api_key": "sk-leftover",
            "max_cloud_upload_mb": 25,
            "llm_provider": "openai_compat",
            "llm_base_url": "http://127.0.0.1:1234/v1",
            "llm_api_key": "sk-leftover-llm",
            "model_path": "A",
        },
    )
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert isinstance(cfg, Config)
    # Surviving keys in the very same file still apply.
    assert cfg.model_path == "A"
    assert cfg.provider == "cloud"
    for key in _REMOVED_KEYS:
        assert not hasattr(cfg, key)
    assert "sk-leftover" not in json.dumps(_snapshot(cfg), default=str)


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


# -- language (FR-3: validation at every entry point) -------------------------


def test_language_from_config_file_outside_ru_en_raises_naming_allowed_values(
    tmp_app_dir: Path,
) -> None:
    _write_config(tmp_app_dir, {"language": "de"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=env)

    message = str(exc_info.value)
    assert "de" in message
    assert "ru" in message
    assert "en" in message


def test_language_from_env_outside_ru_en_raises_naming_allowed_values(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "TRANSCRIBER_LANGUAGE": "de"}

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=env)

    message = str(exc_info.value)
    assert "ru" in message
    assert "en" in message


def test_language_from_overrides_outside_ru_en_raises_naming_allowed_values(
    tmp_app_dir: Path,
) -> None:
    """The CLI's `--language` flag arrives as an override; a bogus value must
    fail config loading (which `cli.main` maps to a nonzero exit)."""
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError) as exc_info:
        load_config(env=env, overrides={"language": "de"})

    message = str(exc_info.value)
    assert "ru" in message
    assert "en" in message


def test_language_override_beats_a_valid_config_file_value(tmp_app_dir: Path) -> None:
    """Validation runs after the layers merge: an invalid file value that a
    valid override replaces must not fail the load, and vice versa."""
    _write_config(tmp_app_dir, {"language": "de"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env, overrides={"language": "en"})

    assert cfg.language == "en"


def test_language_empty_string_normalizes_to_none(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"language": ""})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    assert load_config(env=env).language is None

    env_empty = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "TRANSCRIBER_LANGUAGE": ""}
    assert load_config(env=env_empty).language is None


def test_language_is_normalized_to_lowercase(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir), "TRANSCRIBER_LANGUAGE": "EN"}

    assert load_config(env=env).language == "en"


@pytest.mark.parametrize("language", ["ru", "en", "tr"])
def test_language_accepts_the_whole_universe(tmp_app_dir: Path, language: str) -> None:
    _write_config(tmp_app_dir, {"language": language})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    assert load_config(env=env).language == language


def test_language_unset_stays_none(tmp_app_dir: Path) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    assert load_config(env=env).language is None


def test_llm_model_defaults_to_the_catalog_default_on_a_fresh_install(
    tmp_app_dir: Path,
) -> None:
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    default = llm_catalog.get(llm_catalog.DEFAULT_MODEL_ID)
    assert default is not None
    assert cfg.llm_model == llm_catalog.DEFAULT_MODEL_ID
    assert cfg.llm_model_repo == default.repo
    assert cfg.llm_model_revision == default.revision
    assert cfg.llm_model_file == default.file
    assert cfg.llm_ctx == 32768


def test_llm_model_retired_id_migrates_to_the_default(tmp_app_dir: Path) -> None:
    """A config.json still naming the retired 35B (written by the old
    Settings model switcher) must load on the default, not fail."""
    retired = next(iter(llm_catalog.RETIRED_MODEL_IDS))
    _write_config(tmp_app_dir, {"llm_model": retired})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    default = llm_catalog.DEFAULT_ENTRY
    assert cfg.llm_model == default.id
    assert cfg.llm_model_repo == default.repo
    assert cfg.llm_model_file == default.file


def test_llm_model_retired_id_with_an_explicit_file_stays_on_it(tmp_app_dir: Path) -> None:
    """The escape hatch wins over retirement: an operator who pinned the file
    by hand keeps running it."""
    retired = next(iter(llm_catalog.RETIRED_MODEL_IDS))
    _write_config(tmp_app_dir, {"llm_model": retired, "llm_model_file": "pinned.gguf"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.llm_model == retired
    assert cfg.llm_model_file == "pinned.gguf"


def test_llm_model_explicit_repo_and_file_beat_the_catalog(tmp_app_dir: Path) -> None:
    """The hand-picked-GGUF escape hatch: explicit pins win over the catalog
    entry the id would resolve to."""
    _write_config(
        tmp_app_dir,
        {
            "llm_model": llm_catalog.DEFAULT_MODEL_ID,
            "llm_model_repo": "someone/custom-GGUF",
            "llm_model_revision": "deadbeef",
            "llm_model_file": "custom.gguf",
        },
    )
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.llm_model == llm_catalog.DEFAULT_MODEL_ID
    assert cfg.llm_model_repo == "someone/custom-GGUF"
    assert cfg.llm_model_revision == "deadbeef"
    assert cfg.llm_model_file == "custom.gguf"


def test_llm_model_outside_the_catalog_with_a_file_is_allowed(tmp_app_dir: Path) -> None:
    _write_config(tmp_app_dir, {"llm_model": "my-model", "llm_model_file": "my-model.gguf"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    cfg = load_config(env=env)

    assert cfg.llm_model == "my-model"
    assert cfg.llm_model_file == "my-model.gguf"


def test_llm_model_unknown_id_without_a_file_raises_naming_the_catalog(
    tmp_app_dir: Path,
) -> None:
    _write_config(tmp_app_dir, {"llm_model": "no-such-model"})
    env = {"TRANSCRIBER_APP_DIR": str(tmp_app_dir)}

    with pytest.raises(ConfigError) as excinfo:
        load_config(env=env)
    for model_id in llm_catalog.known_ids():
        assert model_id in str(excinfo.value)
