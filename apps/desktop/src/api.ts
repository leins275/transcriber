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
import type { AppError, JobSnapshot, ServiceStatusView, SettingsView } from "./types";

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

/** Native folder picker for the meetings-root setting (FR-16, FR-18). */
export async function chooseMeetingsFolder(): Promise<string | null> {
  const selected = await open({ multiple: false, directory: true });
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
