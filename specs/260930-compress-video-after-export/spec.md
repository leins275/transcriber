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
- **Encoder: `h264_nvenc` first, `libx264` CRF 23 second.** H.264 yuv420p,
  same size and frame rate, pts copied in the source time base. Audio is
  copied when an mp4 can hold it (aac/mp3/opus/ac3/eac3/alac), else AAC
  160k. Subtitles, chapters, extra streams and rotation metadata are
  dropped (accepted).
- **Settings toggle `compress_video`, on by default.** App-only key; the
  service never reads it; no sidecar restart.

## Requirements

- FR-1 A `compress` job (`POST /v1/jobs`, `input_path` = meeting dir) runs
  the re-encode on the serial worker and reports `compressing video` with
  the decoded fraction as its phase.
- FR-2 Skip with a warning, no encoding: audio-only extension, no video
  stream, average bitrate at or under 2000 kbps.
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

- `services/transcription/tests/test_compress.py` (17 cases, synthetic
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
