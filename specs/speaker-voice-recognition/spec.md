---
slug: speaker-voice-recognition
created: 2026-09-03
status: implemented
---

# Spec: Voice recognition across meetings — make manual speaker labels reusable

## Summary

The operator asked for `speakers.json` to be stored "per project" so the
speaker annotation they make by hand in older meetings carries over to
future uploads in the same project. The file cannot move: it is a `segment
id -> name` map and segment ids are per meeting. What actually delivers the
goal is (1) making speaker diarization runnable in the installed app, (2)
attaching diarization labels and voice embeddings to the meetings already
labelled by hand, and (3) turning the pass on for new uploads — after which
the existing cross-meeting matcher (`speaker_matching.py`) pre-names every
returning voice.

## Problem & context

- Project-level voice memory already existed: `collect_project_voiceprints`
  scans the sibling meetings of a project, joins each one's `speakers.json`
  to the `diarization.speaker_embeddings` in its `transcript.json` by
  majority vote, and `auto_assign_speakers` pre-fills a newly diarized
  meeting's `speakers.json` (additive only). Name suggestions are likewise
  project-wide (`list_project_speaker_names`).
- It had never fired on the operator's machine: all 34 transcripts were
  labelled by hand on undiarized transcripts (no labels, no embeddings), and
  diarization could not run in the installed app — the baked environment
  has no pyannote/torch, the pyannote models are gated on Hugging Face, no
  token was configured, `diarize` defaulted to off, and no UI exposed any
  of it.

## Decisions (with the operator, 2026-09-03)

- **Real voice recognition**, not a names-only roster.
- **CUDA build only** (`torch 2.8.0+cu126` / `torchaudio 2.8.0+cu126`,
  the same versions `uv.lock` pins from PyPI, so the rest of the extra's
  closure stays valid; 2.8 is the last series with the `torchaudio.info` /
  `AudioMetaData` API pyannote 3 imports -- the first real run on the
  operator's machine failed on torchaudio 2.11 exactly there): ~3 GB
  fetched on demand. The recording is decoded to a waveform by
  faster-whisper's bundled FFmpeg before it reaches pyannote, since
  torchaudio on Windows cannot open mp4/m4a. Machines without an
  NVIDIA GPU are not offered the feature, the same rule the STT CUDA
  runtime follows. `cu126` runs on every driver from the 525 series and on
  GPUs up to Ada (the operator's RTX 4070).
- **No `<PROJECT>/speakers.json`.** The on-demand sibling scan stays the
  project memory: nothing to keep in sync, and moving a meeting between
  projects moves its voice memory with it.
- **Backfill = re-diarize the existing meeting**, never a separate
  registry: the `diarize` job runs the same pipeline over `source.<ext>`,
  labels the *existing* segments (ids untouched) and writes the
  `diarization` block into `transcript.json`. Same model, same embedding
  space as future uploads, so cosine matching is exact; `speakers.json` is
  not touched (it already outranks the labels everywhere).
- **Segments are cut at changes of voice** for a transcript being created
  (`split_segments_at_turns`, word-level, jitter under 2 words and 0.4 s
  folded back), answering the operator's follow-up about segments where
  several people talk. Never applied to an existing transcript.
- **Offline, pinned model loads.** The model fetch snapshots the three
  repos at pinned revisions into the app's own hub cache
  (`PYANNOTE_CACHE`) and pins `refs/main`; the diarizer then loads with
  the hub forced offline and without the token. The token is only ever
  used by the fetch.
- **The models ship in the installer; no token exists anywhere** (operator
  follow-up, same day: "bake my token", then "this token should not even
  exist"). Baking a personal token into a distributed artifact would hand
  the operator's Hugging Face credentials to anyone who downloads it, and
  a CI secret still means a token to own and rotate; instead the ~32 MB of
  trimmed snapshots (config, weights, license, card) are committed under
  `apps/desktop/src-tauri/resources/models/diarization/` and bundled to
  `<install dir>\models\diarization\`. `build_installer.py`'s
  `diarization_models_check` stage fails a build whose committed tree
  drifts from the service's pins. The in-app token box only appears on a
  build without them. Redistribution is within the models' licenses (MIT;
  CC BY 4.0 with attribution in the service README).

## What shipped

- Service: `diarization_runtime.py` (runtime fetch on `CudaRuntimeDownload`,
  whole-wheel + tarball extraction with `archive_root`; the model fetch;
  `GET /v1/diarization/status`; the two `/v1/diarization-*/download`
  slots), the generated manifest + `scripts/gen_diarization_runtime.py`
  (`make lint` checks drift), the `diarize` job, `split_segments_at_turns`,
  `speakers.json` folded into the search index fingerprint.
- Rust shell: typed `diarize`/`hf_token` settings + `set_diarization_settings`
  (restarts the sidecar), `commands/speakers.rs` (status, slots,
  `diarize_vault_entry`, `diarize_labelled_meetings`), `LlmJobKind::Diarize`.
- UI: Settings → Speakers row (five steps), RecordingPage → "Identify
  speakers", `diarize` job rows.
- Docs: `docs/setup.md`, `docs/config-contract.md`,
  `services/transcription/README.md`, `CLAUDE.md`.

## Out of scope

- A CPU torch build in the installed app (dev environments get it from
  the `diarization` extra).
- Tuning `speaker_match_threshold` (0.5) against real data — to be
  revisited once the operator's vault has been backfilled.
- Re-diarizing meetings whose recording is gone (nothing to identify).
