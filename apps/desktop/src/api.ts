/**
 * The only module in this app that imports `@tauri-apps/api` (T12). Typed
 * wrappers over the six IPC commands from the plan's frozen contract
 * (specs/tauri-desktop-app/plan.md — "IPC contract"), plus `listen` helpers
 * for the two events and the dialog-plugin calls for the folder/file
 * pickers. Drag-drop uses the window drag-drop event
 * (`getCurrentWebview().onDragDropEvent`) — there is no HTML5
 * `drop`/`dataTransfer` code path anywhere in this app (FR-4).
 */
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import type { DragDropEvent } from "@tauri-apps/api/webview";
import { open } from "@tauri-apps/plugin-dialog";
import type {
  AppError,
  JobSnapshot,
  LedgerJobView,
  MeetingUpdate,
  ServiceStatusView,
  SettingsView,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
} from "./types";
import type { ModelDownloadStatus } from "./lib/modelDownload";

/** Narrows an unknown IPC rejection into the frozen `AppError` shape (NFR-6). */
function toAppError(error: unknown): AppError {
  if (
    typeof error === "object" &&
    error !== null &&
    "kind" in error &&
    "message" in error &&
    typeof (error as { message: unknown }).message === "string"
  ) {
    return error as AppError;
  }
  const message = error instanceof Error ? error.message : String(error);
  return { kind: "internal", message };
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  try {
    return await invoke<T>(cmd, args);
  } catch (error) {
    throw toAppError(error);
  }
}

export const api = {
  getSettings: (): Promise<SettingsView> => call<SettingsView>("get_settings"),
  setMeetingsRoot: (path: string): Promise<SettingsView> =>
    call<SettingsView>("set_meetings_root", { path }),
  enqueuePaths: (paths: string[]): Promise<JobSnapshot[]> =>
    call<JobSnapshot[]>("enqueue_paths", { paths }),
  listJobs: (): Promise<JobSnapshot[]> => call<JobSnapshot[]>("list_jobs"),
  serviceStatus: (): Promise<ServiceStatusView> => call<ServiceStatusView>("service_status"),
  revealJob: (jobId: string): Promise<void> => call<void>("reveal_job", { jobId }),
  // Vault-browser trio (additive) -- lists already-ingested meetings on
  // startup/after ingest, and reveals one by its server-issued id (never a
  // raw path).
  listVault: (): Promise<VaultMeetingView[]> => call<VaultMeetingView[]>("list_vault"),
  revealVaultEntry: (entryId: string): Promise<void> =>
    call<void>("reveal_vault_entry", { entryId }),
  // Per-meeting commands (additive) -- all three name the meeting by the
  // id `list_vault` issued, never by a path, exactly like `revealVaultEntry`.
  readTranscript: (entryId: string): Promise<TranscriptView> =>
    call<TranscriptView>("read_transcript", { entryId }),
  updateVaultEntry: (entryId: string, update: MeetingUpdate): Promise<VaultMeetingView> =>
    call<VaultMeetingView>("update_vault_entry", {
      entryId,
      project: update.project,
      date: update.date,
      title: update.title,
    }),
  deleteVaultEntry: (entryId: string): Promise<void> =>
    call<void>("delete_vault_entry", { entryId }),
  /** Replaces a meeting's speaker labels wholesale -- renaming a speaker is
   * by definition an edit to every segment they hold, so sending the whole
   * map keeps that one operation atomic. */
  setSpeakerLabels: (entryId: string, assignments: Record<string, string>): Promise<void> =>
    call<void>("set_speaker_labels", { entryId, assignments }),
  readSummary: (entryId: string): Promise<SummaryView> =>
    call<SummaryView>("read_summary", { entryId }),
  /** Re-runs transcription over a recording already filed in the vault --
   * never a second ingest, which would file a duplicate under a suffixed
   * name. */
  transcribeVaultEntry: (entryId: string): Promise<JobSnapshot> =>
    call<JobSnapshot>("transcribe_vault_entry", { entryId }),
  /** Resolves `false` when there was nothing left to cancel (the job never
   * reached the service, or already finished) -- information, not an error. */
  cancelJob: (jobId: string): Promise<boolean> => call<boolean>("cancel_job", { jobId }),
  // F2's own sqlite job ledger, newest first (`GET /v1/jobs` behind the
  // Rust proxy). `limit` is clamped Rust-side to what F2 accepts.
  listServiceJobs: (limit?: number): Promise<LedgerJobView[]> =>
    call<LedgerJobView[]>("list_service_jobs", { limit: limit ?? null }),
  // Model-download trio (T13, FR-12, FR-17) -- thin proxies over the Rust
  // commands, which themselves proxy the T11 HTTP endpoints. None takes a
  // path argument: the destination is resolved on the Rust side only.
  modelDownloadStatus: (): Promise<ModelDownloadStatus> =>
    call<ModelDownloadStatus>("model_download_status"),
  startModelDownload: (): Promise<ModelDownloadStatus> =>
    call<ModelDownloadStatus>("start_model_download"),
  cancelModelDownload: (): Promise<ModelDownloadStatus> =>
    call<ModelDownloadStatus>("cancel_model_download"),
};

/** Upsert-by-id feed of job transitions (FR-8, FR-14). */
export function onJobsUpdated(handler: (job: JobSnapshot) => void): Promise<UnlistenFn> {
  return listen<JobSnapshot>("jobs://updated", (event) => handler(event.payload));
}

/** Sidecar/service reachability changes (FR-13). */
export function onServiceStatus(handler: (status: ServiceStatusView) => void): Promise<UnlistenFn> {
  return listen<ServiceStatusView>("service://status", (event) => handler(event.payload));
}

/** Window drag-drop events (FR-4, FR-5) — the only source of dropped paths. */
export function onDragDrop(handler: (event: DragDropEvent) => void): Promise<UnlistenFn> {
  return getCurrentWebview().onDragDropEvent((event) => handler(event.payload));
}

/** Native file dialog fallback (FR-7). */
export async function chooseFile(): Promise<string[]> {
  const selected = await open({ multiple: true, directory: false });
  if (selected === null) return [];
  return Array.isArray(selected) ? selected : [selected];
}

/**
 * Native folder picker for the meetings-root setting (FR-16, FR-18). Offers
 * a sane starting point under the user's own profile (FR-10's "a sane
 * default") rather than opening wherever the OS last left the dialog.
 */
export async function chooseMeetingsFolder(defaultPath?: string | null): Promise<string | null> {
  const selected = await open({
    multiple: false,
    directory: true,
    defaultPath: defaultPath ?? undefined,
  });
  return typeof selected === "string" ? selected : null;
}

/**
 * Calls an `UnlistenFn` defensively. Tauri's real unlisten is async under
 * the hood even though the public type is `() => void`; if the webview (or
 * a test's mocked internals) has already torn down by the time a React
 * effect cleanup runs, that hidden promise can reject. Never let that
 * become an unhandled rejection.
 */
export function safeUnlisten(fn: UnlistenFn | undefined): void {
  if (!fn) return;
  try {
    const result: unknown = fn();
    if (result && typeof (result as PromiseLike<unknown>).then === "function") {
      Promise.resolve(result as PromiseLike<unknown>).catch(() => {});
    }
  } catch {
    // ignore: teardown race, nothing left to unlisten from.
  }
}
