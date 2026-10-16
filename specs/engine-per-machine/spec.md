---
slug: engine-per-machine
created: 2026-10-16
status: implemented (code); on-machine measurements pending
---

# Spec: the best engine for each machine, picked and fetched at first run

## Summary

The operator runs the app on three machines: a desktop with an NVIDIA
GPU, a MacBook Air M4 (16 GB), and a Panther Lake laptop (Intel Core
Ultra 7 366H: NPU 5 at 50 TOPS, 4-core Xe3 iGPU). Until now the only
accelerator the code ever selected was CUDA; on the other two everything
ran on the CPU, and some of those CPU paths were needlessly slow. The ask:
pick the optimal engines at install time, download what they need, and
use the most optimized path out of the box — the Intel NPU where it
helps, the Apple Silicon GPU everywhere it can.

## What was wrong

- The macOS bake compiles llama.cpp with Metal, but the `-1` auto-fit
  asked NVML for free VRAM, got nothing, and offloaded 0 layers.
- Whisper on the CPU was pinned to faster-whisper's default of 4 threads
  on a 10-core M4 and a 16-core Panther Lake alike.
- Compress tried NVENC then x265 although PyAV's wheels bundle
  VideoToolbox (macOS) and Quick Sync (Windows) HEVC encoders.
- Diarization knew only `cuda`/`cpu`; the speaker runtime was offered on
  Windows only.
- Nothing detected Apple Silicon or an Intel NPU.

## What the research settled (2026-10-16)

| Stage | NVIDIA | Apple Silicon | Intel Core Ultra 7 366H |
|---|---|---|---|
| Whisper | CTranslate2 CUDA | **mlx-whisper on the GPU** — CTranslate2's own Metal backend is an open, unmerged PR (#2077), source-build only | **OpenVINO GenAI WhisperPipeline on the NPU**, GPU then CPU as fallback; measured on Lunar Lake: turbo 614 ms/window on the NPU vs 954 ms on the iGPU |
| LLM (Qwen3.5-9B) | llama.cpp CUDA | **llama.cpp Metal** (already compiled in) | CPU — llama.cpp's OpenVINO backend is a preview whose validation table lists Qwen3.5 as *failing on the NPU*; no prebuilt Vulkan/SYCL wheel for the iGPU |
| Diarization | CUDA | **MPS** | CPU torch; no NPU path |
| Compress | hevc_nvenc | **hevc_videotoolbox** | **hevc_qsv** |

NPU caveats that shaped the design: static shapes (one 30 s window, no
`initial_prompt`), a first compile measured at minutes (>10 min for turbo
int8 on a Panther Lake), the driver's compile cache, open long-form bugs.

## Requirements

- FR-1 One hardware profile per process (`hardware.py`): NVIDIA, Apple
  Silicon, Intel NPU, Intel GPU, physical memory. No subprocess (the
  Intel pair comes from the registry's PnP enumeration).
- FR-2 One engine row per stage (`engines.py`), reported by `/health`,
  shown in Settings → Engines, logged at startup.
- FR-3 Whisper backends `ctranslate2` / `mlx` / `openvino` behind the
  unchanged `local` provider name; `auto` picks the accelerated one whose
  payload is on disk and falls back to faster-whisper on any failure to
  import. The faster-whisper snapshot is always downloaded.
- FR-4 First-run download as a chain of named, individually optional
  phases composed per machine; an optional failure is a warning with a
  retry, never a blocked model.
- FR-5 macOS bakes `whisper-mlx` and `diarization`; Windows fetches
  `whisper-openvino` at first run from a lock-generated manifest.
- FR-6 Metal offload, CPU threads = every core, platform encoder order,
  MPS for diarization, whisper weights released after each job on Apple
  Silicon.
- NFR Everything stays local; no new cloud call; the default test suite
  stays model-, GPU- and network-free (fakes for every engine library).

## Decisions

1. The CTranslate2 snapshot stays the universal fallback and is fetched
   everywhere (3 GB on the Mac beside the 3 GB MLX weights). Cheap to
   flip later: skip the phase when the engine's weights are present.
2. The NPU default is `whisper-large-v3-turbo` int8 (OpenVINO's own
   validated NPU workload); the full `large-v3` IR is one config key away.
   Agreement against CUDA transcripts decides the shipped default.
3. The Mac bakes everything (no makensis limit on the dmg); only weights
   download.
4. OpenVINO is a first-run fetch on Windows, gated on an Intel NPU or GPU
   *and no NVIDIA GPU*.
5. Out of scope: LLM on the Intel iGPU/NPU, CoreML/ANE whisper (Swift
   only), pyannote on Intel XPU, CTranslate2 MPS until upstream merges it.
