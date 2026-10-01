# Compress the video after the drop-to-insights chain

**Status:** shipped on `main` (2026-09-30). Code wins over this document.

## Problem

A dropped video recording is filed verbatim as `<meeting>/source.<ext>` and
stays at its original size forever, so the vault fills up with multi-GB
originals whose only remaining job is playback.

## Decisions (taken with the operator)

- **Engine: in-process via PyAV.** `av` (FFmpeg's libraries bundled in the
  wheel, already faster-whisper's decoder) is now a direct dependency of the
  service. No `ffmpeg.exe` is shipped or downloaded — the service spec's
  FR-7 ("no external binaries") and `test_attribution.py`'s no-shell guard
  stay intact.
- **The original is replaced.** Output becomes `source.mp4`; the old
  `source.<ext>` is deleted only after the output is verified. The meeting
  never holds two `source.*` files (work files carry a `compress.` prefix,
  whose stem no scanner reads as the recording).
- **Encoder: `libx265` CRF 26, shorter side capped at 1080p.** HEVC
  yuv420p in an `hvc1`-tagged mp4, same frame rate, pts copied in the
  source time base, area resampling for the downscale. Audio is copied when an mp4 can hold it
  (aac/mp3/opus/ac3/eac3/alac), else AAC 160k. Subtitles, chapters, extra
  streams and rotation metadata are dropped (accepted).
  *Revised after measuring on the operator's vault* (55 of 73 recordings
  are 4K screen shares at a fixed 2668 kbps): at the same resolution x264
  CRF 23 came out at the source's bitrate and NVENC CQ 23 at 4.7 Mbps, so
  the original "no visible loss, NVENC first" tuning would have compressed
  nothing. Capping at 1080p gives about 65 % (924 kbps) at 69 fps; the
  alternatives measured were 1440p x264 (44 %), 4K HEVC NVENC CQ 33
  (56 %, HEVC playback caveat) and 4K x264 CRF 28 (38 %). At 1080p output
  NVENC H.264 was 1666 kbps against x264's 924 at the same speed, so the
  GPU path was dropped. *Revised once more* after measuring HEVC on the
  CPU: on this mostly-static screen-share content x265 CRF 26 at 1080p
  came out at 334 kbps (87 % under the source) at x264's speed, against
  852 kbps for `hevc_nvenc` CQ 28 (68 %, 25 % faster) and 806 kbps for
  x265 CRF 28 at full 4K (70 %, 23 fps). The operator chose x265 at 1080p
  everywhere over a GPU-first order, accepting the HEVC playback caveat
  (Windows needs the HEVC extension; VLC and macOS play it natively).
- **Settings toggle `compress_video`, on by default.** App-only key; the
  service never reads it; no sidecar restart.

## Requirements

- FR-1 A `compress` job (`POST /v1/jobs`, `input_path` = meeting dir) runs
  the re-encode on the serial worker and reports `compressing video` with
  the decoded fraction as its phase. The output's shorter side is capped at
  1080 (`target_size`), the aspect kept, both sides even.
- FR-2 Skip with a warning, no encoding: audio-only extension, no video
  stream, average bitrate at or under 700 kbps (lowered from 2000 on 2026-10-01).
- FR-3 Keep the original with a warning: no encoder opens, the output is
  under 15 % smaller, or verification fails (opens, has video, duration
  within 1 s of the source). Only a cancellation propagates.
- FR-4 The app queues the stage after a successful export, or right after
  the transcription when the LLM stages are skipped for want of a model;
  only while `compress_video` is on and the recording's extension is a
  video one; never for a manual re-transcribe, summarize or export.
- FR-5 Settings → Recordings flips the key; the job list narrates
  "Compressing video".

## Verification

- `services/transcription/tests/test_compress.py` (22 cases, synthetic
  recordings, CPU encoder only) and the `compress` cases in
  `tests/test_llm_jobs.py`.
- `jobs.rs` chain tests (`a_drop_chains_summarize_export_then_compress_in_order`,
  `without_an_llm_model_the_chain_skips_straight_to_compress`,
  `compress_is_not_chained_while_the_toggle_is_off`,
  `compress_is_not_chained_for_an_audio_recording`,
  `a_manual_export_never_chains_compress`), `config.rs` and `commands.rs`
  toggle tests, `tests/roster_bounds.rs` (three chained stages).
- `SettingsPage.test.tsx` recordings row, `JobRow` / `activeJob` labels.
- Manual smoke: drop a short `.mkv` → the job list ends with "Compressing
  video", the folder holds a lone smaller `source.mp4`; an `.m4a` ends at
  export; the switch off → no compress job.
