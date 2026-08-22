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
};

export type JobState =
  "pending" | "ingesting" | "queued" | "running" | "done" | "failed" | "rejected";

export type JobSnapshot = {
  id: string;
  source_path: string;
  file_name: string;
  state: JobState;
  classification: "sorted" | "unsorted" | null;
  meeting_dir: string | null;
  source_dest: string | null;
  transcript_path: string | null;
  progress: number | null;
  message: string | null;
  error_kind: string | null;
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

/** A meeting's `summary.md`, if anything has written one. Nothing in this
 * app generates summaries -- that needs a language model -- but the vault
 * has reserved the name since F1's first spec, so one written by hand is
 * readable here. */
export type SummaryView = {
  entry_id: string;
  path: string;
  markdown: string | null;
};

/** A meeting's requested new identity. `project: null` files it under
 * `unsorted/`; the Rust side validates all three parts against exactly the
 * rules ingest applies to a filename. */
export type MeetingUpdate = {
  project: string | null;
  /** Six digits, `YYMMDD`. */
  date: string;
  title: string;
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
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  provider: string | null;
  model: string | null;
  device: string | null;
  source_path: string | null;
  output_path: string | null;
  audio_duration_sec: number | null;
  elapsed_sec: number | null;
  realtime_factor: number | null;
  language: string | null;
  segment_count: number | null;
  error_kind: string | null;
  error_message: string | null;
  service_version: string | null;
};
