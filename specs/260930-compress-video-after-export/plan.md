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

- The encoder loop moves on to the next `EncoderSpec` when one fails to
  open (kept, and tested with an x264 spec carrying an unknown preset,
  even though the default list is x264 alone).
- FFmpeg decodes on one thread unless `thread_type = "AUTO"` is set on the
  stream; the decode is then far from the bottleneck (2500 fps for 1080p
  HEVC), the encoder is.
- Variable frame rates: `average_rate` may be `None`; it only feeds the
  encoder's rate hint, pts are copied. Containers with no duration report
  `progress: null` and are verified against the decoded span.
- x264 at 4K-to-1080p runs at about 70 fps on a desktop CPU (roughly 25
  minutes per hour of meeting) and holds the serial queue; the toggle is
  the escape hatch (documented in setup.md).
- A "kept as-is" outcome shows as a plain Done in the UI; the reason is in
  the service job's `warnings` (the Rust poll does not relay warnings).
  Relaying them into the snapshot's message is a possible follow-up.
