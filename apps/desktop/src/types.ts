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
