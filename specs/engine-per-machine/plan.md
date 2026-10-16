# Plan: engine-per-machine (as implemented, 2026-10-16)

## Service (`services/transcription`)

- `hardware.py` — `Accelerators`, `detect(nvidia=...)`; probes are
  module functions tests pin (`conftest._plain_machine` pins the whole
  suite to a machine with no accelerator). Intel NPU/GPU from
  `HKLM\SYSTEM\CurrentControlSet\Enum\PCI` + `Control\Class`, cached.
- `engines.py` — `Presence`, `EngineRow`, `plan_engines`,
  `resolve_whisper_backend`, `openvino_device_order`, weights dirs.
- `config.py` — `whisper_backend`, `whisper_openvino_device`,
  `whisper_openvino_repo/_revision`, `whisper_mlx_repo/_revision`,
  `cpu_threads`, `whisper_keep_loaded` (+ validation).
- `providers/__init__.py` — `local` resolves through the plan to
  `_ENGINE_PROVIDERS` with fallback; `known_provider_names` unchanged.
- `providers/whisper_common.py` — language rule, VAD batches,
  `restore_times`, `map_segment`, `finish`.
- `providers/mlx_engine.py`, `providers/openvino_engine.py` — the two
  accelerated providers; `unload()` on every provider, called by
  `jobs.py` after a transcribe job unless `whisper_keep_loaded`.
- `openvino_runtime.py` + generated `openvino_runtime_packages.py`
  (`scripts/gen_openvino_runtime.py`, reusing the diarization generator's
  lock walker; `make lint` checks both).
- `api/model_routes.py` — `Phase`, `SetupDownload(phases=...)` (legacy
  two-slot constructor kept), `build_setup_download` by profile,
  `build_mlx_weights_download`; manager status gains `phase`/`warnings`.
- `app.py` `/health` — `accelerators`, `engines`.
- Quick wins: `llm/llama_cpp_local.py` Metal branch (`_gpu_kind`),
  `local_whisper.resolve_cpu_threads`, `compress.encoders_for_platform`
  + `EncoderSpec.pix_fmt`, `diarizer._auto_torch_device` + MPS fallback
  env, `diarization_runtime.reported_runtime_variant` (`mps`).
- `pyproject.toml` — extras `whisper-mlx`, `whisper-openvino`; mypy
  overrides; ruff exclude for the generated manifest. `uv.lock` re-locked.

## Build

- `scripts/build_pyenv.py` — `PLATFORM_EXTRAS = {"darwin": ("whisper-mlx",
  "diarization")}`, merged after `BASE_EXTRAS` in `bake()`.
- `Makefile` lint — `gen_openvino_runtime.py --check`.

## Shell (`apps/desktop`)

- Rust: `EngineRow`, `ServiceHealth.engines`, `ModelDownloadStatus.phase`
  / `.warnings`; `commands/model.rs` view carries `phase`, `warnings`,
  `engines`; fake service plays an NVIDIA machine.
- TS: `lib/modelDownload.ts` (`EngineRow`, `PHASE_WORDS`,
  `engineSetupNeeded`, `engineLabel`, `STAGE_LABELS`),
  `ModelDownloadStep` (phase label, generalized setup notice),
  `SettingsPage` (Engines block, `mps` label), `serviceLabel`
  (device suffix from the whisper row), `types.ts` variant union.

## Tests added

`test_hardware.py`, `test_engines.py`, `test_openvino_runtime.py`,
`scripts/tests/test_platform_extras.py`, plus cases in `test_config.py`,
`test_compress.py`, `test_diarizer.py`, `test_diarization_runtime.py`,
`test_provider_local.py`, `test_llm_units.py`, `test_api_contract.py`,
`modelDownload.test.ts`, `serviceLabel.test.ts`.
