# Plan — compress the video after the chain

Three commits on `main`, each green on its own payload's gate.

1. **Service** (`feat(service): compress job replaces a filed video with an
   h264 mp4`): `av>=18,<19` as a direct dependency; `compress.py`
   (`compress_recording`, `EncoderSpec`, `CompressOutcome`, skip / verify /
   swap rules); `JobType` + `KNOWN_JOB_TYPES` + `_compress_sync`; README
   rows; `tests/test_compress.py` and job-level cases.
2. **Desktop** (`feat(desktop): chain a compress stage after export behind a
   compress_video setting`): `vault::media::is_video_extension` and
   `vault::source_file_in` (the app's `commands/meetings.rs` finder now
   delegates); `LlmJobKind::Compress`; `FollowUp::Compress` with the gate in
   `jobs.rs::queue_follow_up`; `Settings.compress_video` +
   `set_compress_video` command; TS types, API binding, job labels, the
   Settings "Recordings" row; the `meeting_type.rs` harness switches the
   stage off (those tests need an idle meeting).
3. **Docs** (`docs: describe the compress stage and the compress_video key`):
   `docs/config-contract.md`, `docs/setup.md`, `CLAUDE.md`, this folder.

## Things that could bite

- NVENC refuses frames below its minimum size and fails at
  `avcodec_open2`; the encoder loop moves on to the next candidate, so a
  missing NVIDIA runtime costs one failed open, not the job.
- NVENC's constant-quality mode needs the bitrate cap zeroed (`-b:v 0`);
  `EncoderSpec.zero_bit_rate` does that on the stream.
- Variable frame rates: `average_rate` may be `None`; it only feeds the
  encoder's rate hint, pts are copied. Containers with no duration report
  `progress: null` and are verified against the decoded span.
- x264 on a CPU-only machine is roughly real time for 1080p and holds the
  serial queue; the toggle is the escape hatch (documented in setup.md).
- A "kept as-is" outcome shows as a plain Done in the UI; the reason is in
  the service job's `warnings` (the Rust poll does not relay warnings).
  Relaying them into the snapshot's message is a possible follow-up.
