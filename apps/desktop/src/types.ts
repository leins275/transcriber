/**
 * IPC contract types, transcribed exactly from the plan's frozen contract
 * (specs/tauri-desktop-app/plan.md — "IPC contract"). This file is the
 * single source of truth for these shapes on the frontend; T12 wires the
 * `api.ts` layer around them. Nothing here imports `@tauri-apps/api`.
 */

export type SettingsView = {
  meetings_root: string | null;
  meetings_root_exists: boolean; // false => actionable error state, not a panic
  service_base_url: string | null;
  supported_extensions: string[]; // single source of truth, from the Rust side
  // Set when config.json existed but failed to parse at startup (E3): the
  // app still opened (falling back to first-run defaults) instead of
  // panicking, and this is the actionable error to render.
  config_error: string | null;
  // A sane starting point for the vault folder picker (windows-installer-
  // build E2, FR-10). Additive to the frozen contract above.
  default_meetings_root: string | null;
  /** Speaker identification on new transcriptions (the service's `diarize`
   * key, resolved to its default when unset). Additive. */
  diarize: boolean;
  /** Whether a Hugging Face token is stored; the token itself never
   * reaches the UI. Additive. */
  hf_token_present: boolean;
  /** Whether a dropped video is compressed once its transcript, summary
   * and export are done (the app's `compress_video` key, on when unset).
   * Additive. */
  compress_video: boolean;
};

/** `GET /v1/diarization/status` through the Rust shell: which of speaker
 * identification's prerequisites are met on this machine. */
export type DiarizationStatusView = {
  /** The pyannote/torch runtime is importable (fetched or installed). */
  runtime_present: boolean;
  /** Every pinned model snapshot is in the app's cache. */
  model_present: boolean;
  /** A Hugging Face token is configured. */
  token_present: boolean;
  /** `diarize`: new transcriptions run the pass. */
  enabled: boolean;
  /** An NVIDIA GPU is present -- without one the feature is not offered. */
  gpu_present: boolean;
  /** What the runtime fetch would transfer, for the button's label. */
  runtime_total_bytes: number;
};

/** One of speaker identification's two download slots (the runtime, the
 * models): the plain download fields; presence lives on the status. */
export type DiarizationDownloadStatus = {
  state: "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
};

export type JobState =
  "pending" | "ingesting" | "queued" | "running" | "done" | "failed" | "rejected";

/** Which pipeline a job runs. `transcribe` is the original; the rest are
 * the derived jobs over an already-transcribed meeting (additive to the
 * frozen contract): the LLM feature's summary and export, `diarize`,
 * speaker identification written into the existing transcript, and
 * `compress`, the chain's last stage re-encoding a video recording in its
 * place. `suggest_title` asks the language model for a short meeting name
 * out of the summary; it writes nothing and answers on the snapshot. */
export type JobType =
  "transcribe" | "summarize" | "export" | "diarize" | "compress" | "suggest_title";

export type JobSnapshot = {
  id: string;
  source_path: string;
  file_name: string;
  job_type: JobType;
  state: JobState;
  classification: "sorted" | "unsorted" | null;
  meeting_dir: string | null;
  source_dest: string | null;
  transcript_path: string | null;
  progress: number | null;
  /** The sub-step a running job is in ("rendering PDF", "writing summary ·
   * 812 tokens"), or null while queued, while the headline verb already says
   * it all, and in every terminal state. Optional so snapshot literals
   * written before the service reported phases still typecheck. */
  phase?: string | null;
  message: string | null;
  error_kind: string | null;
  /** The meeting name a finished `suggest_title` job proposes; null for
   * every other job and until that one is done. A proposal only — the
   * recording page prefills its rename form with it and the operator saves.
   * Optional so snapshot literals written before the feature still
   * typecheck. */
  suggested_title?: string | null;
  created_at: string;
};

export type ServiceStatusView = {
  state: "starting" | "ready" | "unavailable";
  base_url: string | null;
  detail: string | null;
};

export type ErrorKind =
  | "not_configured"
  | "invalid_argument"
  | "outside_root"
  | "unsupported_extension"
  | "not_a_file"
  | "vault"
  | "collision"
  | "service_unavailable"
  | "service"
  | "config"
  | "io"
  | "internal";

export type AppError = {
  kind: ErrorKind;
  message: string;
};

// Vault-browser extension to the IPC contract (additive: a new command and
// view type, mirroring the Rust `VaultMeetingView`/`list_vault`/
// `reveal_vault_entry` — never a change to an existing field/command).
export type VaultMeetingView = {
  id: string;
  project: string | null;
  meeting_name: string;
  meeting_dir: string;
  has_source: boolean;
  has_transcript: boolean;
};

// Per-meeting extension to the IPC contract (additive, mirroring the Rust
// `commands::meetings` module): a transcript to read, and the shape
// `update_vault_entry` accepts.
export type TranscriptSegmentView = {
  id: number;
  /** Seconds from the start of the recording. */
  start: number;
  end: number;
  text: string;
};

/** The languages the service decodes in. `null` anywhere this appears as
 * a request field means "auto" — the service picks between these itself
 * (specs/transcript-language-selection; Turkish added 2026-09). */
export type TranscriptLanguage = "ru" | "en" | "tr";

export type TranscriptView = {
  entry_id: string;
  meeting_name: string;
  language: string | null;
  created_at: string | null;
  duration_sec: number | null;
  model: string | null;
  device: string | null;
  text: string;
  segments: TranscriptSegmentView[];
  /** `segment id -> speaker name`, from the meeting's `speakers.json`.
   * Empty for a transcript nobody has labelled yet. */
  speakers: Record<string, string>;
  /** Where `transcript.json` actually is, for the reading view's footer. */
  transcript_path: string;
};

/** How a project governs speaker naming: `open` is the historical
 * behaviour -- free text with the sibling-meeting names as hints -- while
 * `roster` restricts the name controls to the project's listed names. */
export type RosterMode = "open" | "roster";

/** A project's speaker roster, as `read_project_roster` and
 * `save_project_roster` return it (`<PROJECT>/roster.json`). Names only:
 * no ids and no voice embeddings, so this is not the rejected per-project
 * voice store -- recognition stays the on-demand sibling scan. A project
 * with no roster file reads as `{ mode: "open", names: [] }`. */
export type ProjectRosterView = {
  mode: RosterMode;
  /** Normalized on the Rust side: trimmed, no blanks, no case-insensitive
   * duplicates, in the operator's order. */
  names: string[];
};

/** A meeting's `summary.md`, if anything has written one. Nothing in this
 * app generates summaries -- that needs a language model -- but the vault
 * has reserved the name since F1's first spec, so one written by hand is
 * readable here. */
export type SummaryView = {
  entry_id: string;
  path: string;
  markdown: string | null;
};

/** A meeting's `note.md` -- the operator's own markdown note, edited in the
 * app. `markdown: null` means no note exists yet. */
export type NoteView = {
  entry_id: string;
  path: string;
  markdown: string | null;
};

/** One hybrid-search hit over the vault (transcripts, summaries, notes).
 * Named by entry id, never a path -- clicking one opens the recording the
 * same way a list row does. */
export type SearchResultView = {
  entry_id: string;
  kind: "transcript" | "summary" | "note";
  meeting_name: string;
  project: string | null;
  snippet: string;
  score: number;
  start_sec: number | null;
  timestamp: string | null;
};

/** One chat turn on the wire to `chat_stream`. */
export type ChatWireMessage = {
  role: "user" | "assistant";
  content: string;
};

/** One event of a streamed chat answer, as the Tauri channel delivers it. */
export type ChatEventView =
  | { type: "delta"; text: string }
  | { type: "sources"; sources: SearchResultView[] }
  | { type: "done"; finish_reason: string }
  | { type: "error"; message: string };

/** A stored answer's cited source, resolved for display: `entry_id` is
 * present while the cited meeting is still listed (clickable), null when
 * it is gone (display-only). */
export type ChatSourceView = {
  entry_id: string | null;
  kind: string;
  meeting_name: string;
  timestamp: string | null;
  start_sec: number | null;
};

/** One row of a project's saved-chat history. */
export type ChatSummaryView = {
  id: string;
  title: string;
  updated_at_ms: number;
  question_count: number;
};

export type ChatMessageView = {
  role: string;
  content: string;
  sources: ChatSourceView[];
};

export type ChatConversationView = {
  id: string;
  title: string;
  messages: ChatMessageView[];
};

/** What `save_chat` receives: sources reference the session's entry ids;
 * the Rust side resolves them to durable vault-relative dirs on write (the
 * frontend never handles paths). */
export type ChatStoredMessage = {
  role: string;
  content: string;
  sources: ChatSourceView[];
};

/** One meeting's row of the index-status panel. */
export type IndexMeetingView = {
  name: string;
  state: "indexed" | "pending" | "no_transcript";
  chunks: number;
};

export type IndexStatusView = {
  project: string;
  updated_at_sec: number | null;
  indexing: boolean;
  progress: number | null;
  indexed_count: number;
  total_count: number;
  meetings: IndexMeetingView[];
};

/** Why the voice memory uses a voice sample (`ok`) or sets it aside:
 * `unconfirmed` was named by the machine and never by the operator,
 * `partial` carries its name on only a few of the voice's segments, `short`
 * holds too little speech, `conflict` sounds like somebody else. */
export type VoiceSampleQuality = "ok" | "unconfirmed" | "partial" | "short" | "conflict";

/** One diarized voice of one meeting, as the voice memory judges it. */
export type VoiceSampleView = {
  label: string;
  name: string;
  speech_sec: number;
  quality: VoiceSampleQuality;
  conflicts_with: string | null;
};

/** One meeting's row of the voice-memory panel. */
export type VoiceMeetingView = {
  name: string;
  state: "named" | "unnamed" | "no_voices" | "no_transcript";
  scanned_at: number | null;
  voices: VoiceSampleView[];
};

/** One person a meeting of the project can be named for. */
export type VoiceSummaryView = {
  name: string;
  /** Samples recognition uses, across the whole vault. */
  samples: number;
  /** How many of them come from this project's own meetings. */
  here: number;
  other_projects: string[];
  speech_sec: number;
  set_aside: number;
};

/** The voice memory as one project sees it (`voice_memory_status`). */
export type VoiceStatusView = {
  project: string;
  /** The project's roster is strict: `voices` is that roster, nothing else. */
  roster_only: boolean;
  updated_at: number | null;
  /** The project's meetings this very read had to re-read. */
  rescanned: string[];
  rescanned_elsewhere: number;
  voices: VoiceSummaryView[];
  meetings: VoiceMeetingView[];
};

/** A meeting's requested new identity. `project: null` files it under
 * `unsorted/`; the Rust side validates all four parts against exactly the
 * rules ingest applies to a filename. */
export type MeetingUpdate = {
  project: string | null;
  /** Six digits, `YYMMDD`. */
  date: string;
  title: string;
  /** The optional meeting type — the folder name's third section. Absent
   * means "no type", which also strips one the meeting already carries;
   * `api.updateVaultEntry` spells that out as `null` for the Rust
   * `Option<String>`. Never contains the reserved `-`. */
  kind?: string;
};

// LLM-feature extension to the IPC contract (additive): the GGUF download.

/** The GGUF (LLM model) download status — the whisper trio's shape minus
 * the CUDA fields. */
export type LlmModelDownloadStatus = {
  state: "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
  model_present: boolean;
  /** Whether the first-run CUDA build of the LLM runtime is on disk.
   * `null` = no NVIDIA GPU on this machine (never offer it) or unknown. */
  gpu_build_present: boolean | null;
};

/** The bge-m3 (search embeddings) download status — the LLM trio's shape
 * minus the GPU field (the embedder is CPU-only by design). */
export type EmbeddingModelDownloadStatus = {
  state: "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
  model_present: boolean;
};

/** One curated model's download slot (the plain download fields; the
 * health-derived extras live once on `LlmModelsView`). */
export type LlmModelDownload = {
  state: "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
};

/** One row of the curated LLM model catalog. */
export type LlmCatalogModel = {
  id: string;
  label: string;
  file: string;
  /** Approximate GGUF size for display; `null` for a hand-configured model
   * outside the catalog. */
  size_bytes: number | null;
  catalog: boolean;
  present: boolean;
  active: boolean;
  download: LlmModelDownload;
};

/** `list_llm_models` response: the catalog plus which model is active and
 * the one machine-level field the rows share. */
export type LlmModelsView = {
  active: string;
  gpu_build_present: boolean | null;
  models: LlmCatalogModel[];
};

// Service-log extension to the IPC contract: one row of F2's own sqlite job
// ledger (`GET /v1/jobs`), proxied through `list_service_jobs`. Every field
// but `job_id`/`status` is nullable because the row is filled in over the
// job's lifetime -- a queued job genuinely has no `elapsed_sec` yet.
export type LedgerJobView = {
  job_id: string;
  /** F2's own five-state vocabulary, uncollapsed: `queued`, `running`,
   * `succeeded`, `failed`, `cancelled`. */
  status: string;
  /** The service's job type (`transcribe`, `summarize`, `compress`, ...). */
  job_type: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  provider: string | null;
  model: string | null;
  device: string | null;
  source_path: string | null;
  output_path: string | null;
  /** The recording's original file name as it was dropped, recorded at
   * submit time in the ledger's `meeting_json`. `null` for every row that
   * predates that (and for retranscribes of an already-filed recording,
   * where only `source.<ext>` exists on disk) -- those fall back to a name
   * derived from `source_path`. Parsed once on the Rust side. */
  original_file_name: string | null;
  audio_duration_sec: number | null;
  elapsed_sec: number | null;
  realtime_factor: number | null;
  language: string | null;
  segment_count: number | null;
  error_kind: string | null;
  error_message: string | null;
  service_version: string | null;
  /** A finished `compress` row's result; `null` for every other row. */
  compression: LedgerCompressionView | null;
};

/** What a `compress` job did to the recording. `replaced: false` means the
 * original was kept and `warning` says why. */
export type LedgerCompressionView = {
  replaced: boolean;
  encoder: string | null;
  before_bytes: number;
  after_bytes: number;
  warning: string | null;
};

// Speakers-database extension to the IPC contract (additive). A person has
// no id: any of their names -- the canonical one or an alias -- addresses
// them, so every command takes a name.

/** One row of the Speakers table (`list_speakers`). */
export type SpeakerView = {
  /** The canonical name. */
  name: string;
  /** Other spellings of the same person found in meeting labels. */
  aliases: string[];
  bio: string;
  /** `false`: named in meeting labels only, never edited. Saving anything
   * about them registers them. */
  registered: boolean;
  /** Sorted project codes the person is labelled in. */
  projects: string[];
  /** Meetings holding at least one labelled segment of theirs. */
  meetings: number;
  labelled_segments: number;
  /** Segments the operator labelled, as opposed to machine-named ones. */
  hand_segments: number;
  /** Labelled speech time, in seconds. */
  speech_sec: number;
  /** Voice-memory samples recognition uses / sets aside. */
  voice_samples: number;
  voice_set_aside: number;
};

/** The registry entry `save_speaker` answers with. */
export type SpeakerRecord = {
  name: string;
  aliases: string[];
  bio: string;
  registered: boolean;
};

/** What a save changes. A field left out is left alone; one that is present
 * replaces the stored value wholesale. */
export type SpeakerUpdate = {
  /** Rename: the old canonical name stays behind as an alias. */
  newName?: string;
  /** Adding another person's name here merges that person in. */
  aliases?: string[];
  bio?: string;
};

export type SpeakerProjectView = {
  project: string;
  meetings: number;
  speech_sec: number;
  /** The person is on that project's roster. */
  in_roster: boolean;
};

export type SpeakerSegmentView = {
  id: number;
  start: number;
  end: number;
  text: string;
};

/** One meeting a person was labelled in. */
export type SpeakerMeetingView = {
  /** The vault entry id the recording opens by -- never a path. `null` when
   * the meeting is not in the library listing: shown, but not a link. */
  entry_id: string | null;
  project: string;
  /** The meeting's folder name. */
  meeting: string;
  labelled_segments: number;
  hand_segments: number;
  speech_sec: number;
  /** `null`: the meeting holds no voice sample of this person. */
  voice_quality: VoiceSampleQuality | null;
  /** For a `conflict`: whose voice the sample sounds like. */
  voice_conflicts_with?: string | null;
  /** The person's hand-labelled segments in time order, capped per meeting. */
  segments: SpeakerSegmentView[];
  segments_truncated: boolean;
};

/** The speaker page's data (`speaker_detail`). */
export type SpeakerDetailView = {
  name: string;
  aliases: string[];
  bio: string;
  registered: boolean;
  projects: SpeakerProjectView[];
  voice: { samples: number; set_aside: number; speech_sec: number };
  /** Newest first. */
  meetings: SpeakerMeetingView[];
};
