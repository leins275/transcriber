# Verification: engine-per-machine

## Done on the NVIDIA desktop (2026-10-16)

- `services/transcription`: ruff format/check clean, mypy clean (66
  files), pytest 1055 passed / 2 skipped / 3 deselected (model-, GPU-,
  network-free; the suite pins the hardware probes to a plain machine).
- `scripts/tests`: 171 passed (slow bake tests deselected).
- `apps/desktop`: prettier, eslint, tsc clean; vitest 65 files / 791
  tests passed.
- Rust: `cargo fmt`, `cargo clippy -D warnings` clean; `cargo test -p
  transcriber-desktop` all green (294 unit + integration suites).
- Manifests: `gen_diarization_runtime.py --check` and
  `gen_openvino_runtime.py --check` both match `uv.lock`.
- `hardware._pci_devices()` run on this machine: enumerates 76 PCI
  instances, classifies the RTX 4070 as a non-Intel display device and no
  NPU (`(False, False)`); 64 GB of RAM reported.

## Owed on the other machines (not yet run)

1. **MacBook Air M4** (CI dmg or `make installer` on the Mac): first run
   downloads the CT2 model then the MLX weights; Service log shows
   `whisper=mlx/metal (ready) llm=llama.cpp/metal (ready)
   diarization=pyannote/mps (ready) compress=hevc_videotoolbox/gpu` and
   `llm offload: all layers on the Apple GPU`; Activity Monitor shows GPU
   use during transcription and summary. Measure on one ~30 min meeting:
   wall time CT2-CPU (`whisper_backend: ctranslate2`) vs mlx; LLM tokens/s
   before/after; `hevc_videotoolbox` output size vs the x265 output of the
   same file (tune `VIDEOTOOLBOX_QUALITY` until within ~10 % of x265 CRF
   26); diarization wall time `mps` vs `cpu`; word-level agreement of the
   mlx transcript against the CUDA transcript of the same recording.
   **Open point to confirm on the Mac first**: that PyAV's
   `add_stream(..., options={"flags": "+qscale", "global_quality": ...})`
   reaches the generic codec-context options; if not, set
   `codec_context.flags`/`global_quality` through PyAV's `CodecContext`.
2. **Panther Lake laptop**: first run fetches the OpenVINO runtime
   (~90 MB) and the turbo int8 IR (~830 MB); log shows `openvino:NPU`,
   note the first job's compile time, Task Manager's NPU graph active.
   Measure wall time NPU vs `whisper_openvino_device: GPU` vs CT2-CPU, and
   agreement vs the CUDA transcript for turbo int8 and for
   `OpenVINO/whisper-large-v3-int8-ov`; the better trade decides the
   shipped default (spec decision 2). `hevc_qsv` size vs x265. Second
   service start: confirm the NPU compile is cached by the driver; if
   not, keep the pipeline loaded and document it. Check whether
   `WhisperDecodedResults` exposes word timestamps / the detected language
   on this OpenVINO build (the provider reads both if present).
3. **Wizard failure path**: block the network after the CT2 phase → the
   notice says which engine is not installed, "Retry … setup" re-POSTs
   the slot, transcription still runs on CT2.
4. **NVIDIA regression**: drop a recording → CUDA whisper, CUDA LLM,
   NVENC unchanged; Settings → Engines all green.
