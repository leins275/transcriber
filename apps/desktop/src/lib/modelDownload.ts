/**
 * Pure, framework-agnostic model-download logic (T13, FR-12, FR-17):
 * formatting helpers and the status-poll scheduler `ModelDownloadStep.tsx`
 * drives. No `@tauri-apps/api` import here -- `api.ts` stays the only module
 * that imports it; this file is handed injected command functions instead
 * (mirrors the "fake command layer" the component test uses).
 */

export type ModelDownloadState =
  "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";

export type ModelDownloadStatus = {
  state: ModelDownloadState;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
  model_present: boolean;
  /** E13: verbatim backend text, set once a `SetupDownload`'s CUDA-runtime
   * phase has failed and the setup continued into the model phase anyway
   * (E4). Never re-worded on this side. */
  cuda_warning: string | null;
  /** E13: `null` when the service itself judged this host GPU-less (never
   * prompt about a runtime that machine could never use). */
  cuda_runtime_present: boolean | null;
};

/** E8/FR-12 acceptance ("updates at least once a second"): the service
 * emits progress at a 1 s cadence, so this polls at the same 1000 ms rather
 * than F3's 1500 ms job-poll cadence (Q3-A) -- a cheap in-memory read, so
 * there is no cost to matching the documented acceptance criterion exactly
 * instead of under-polling it. */
export const MODEL_DOWNLOAD_POLL_INTERVAL_MS = 1000;

const UNITS = ["B", "KB", "MB", "GB", "TB"];

/** Renders a byte count as a short, human string ("1.5 GB"). Whole bytes
 * under 1 KB are rendered with no decimal ("512 B"). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${Math.round(bytes)} B`;
  }
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${UNITS[unitIndex]}`;
}

/** "1.5 GB / 3.0 GB (50%)" -- bytes *and* percentage together (FR-12). */
export function formatProgress(
  status: Pick<ModelDownloadStatus, "downloaded_bytes" | "total_bytes" | "percent">,
): string {
  return `${formatBytes(status.downloaded_bytes)} / ${formatBytes(status.total_bytes)} (${Math.round(
    status.percent,
  )}%)`;
}

/** Whether `state` is still an active transfer worth polling for. */
export function isInProgress(state: ModelDownloadState): boolean {
  return state === "downloading" || state === "verifying";
}

/**
 * Polls `getStatus` every `intervalMs`, reporting each result to `onUpdate`.
 * Stops itself automatically once a result is no longer in progress (FR-12
 * acceptance: a terminal state -- complete, cancelled or error -- needs no
 * further polling), and a transient rejection is swallowed rather than
 * killing the loop (the next tick simply tries again). Returns a stop
 * function safe to call more than once (component unmount racing a
 * self-stop).
 */
export function pollDownloadStatus(
  getStatus: () => Promise<ModelDownloadStatus>,
  onUpdate: (status: ModelDownloadStatus) => void,
  intervalMs: number = MODEL_DOWNLOAD_POLL_INTERVAL_MS,
): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setInterval> | undefined;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer !== undefined) {
      clearInterval(timer);
      timer = undefined;
    }
  };

  const tick = () => {
    if (stopped) return;
    getStatus()
      .then((status) => {
        if (stopped) return;
        onUpdate(status);
        if (!isInProgress(status.state)) {
          stop();
        }
      })
      .catch(() => {
        // Transient poll failure: never let it kill the loop -- the next
        // tick retries on its own.
      });
  };

  timer = setInterval(tick, intervalMs);
  return stop;
}
