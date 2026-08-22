/**
 * The update check's own state machine, kept out of the component so the
 * transitions can be reasoned about (and tested) without a Tauri runtime.
 *
 * Deliberately not a boolean pair. "Checking", "up to date", "an update is
 * available", "downloading it", "installed, restart to finish" and "the
 * check failed" are six genuinely different things to say to someone, and
 * collapsing them into `loading`/`error` flags is how a UI ends up claiming
 * it is downloading when it has actually given up.
 */

export type UpdateInfo = {
  version: string;
  /** The release notes, as the manifest carries them. Optional: a release
   * published without notes is normal, not an error. */
  notes: string | null;
  /** ISO-8601, straight from the manifest. */
  date: string | null;
};

export type UpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "up-to-date" }
  | { status: "available"; update: UpdateInfo }
  | { status: "downloading"; update: UpdateInfo; percent: number | null }
  | { status: "installed"; update: UpdateInfo }
  | { status: "error"; message: string };

/**
 * Percent complete, or `null` when the manifest gave no content length.
 *
 * A server that does not report a total is common enough that guessing one
 * would be worse than admitting the progress is unknown -- an invented
 * denominator produces a bar that races to 100% and then sits there.
 */
export function downloadPercent(downloaded: number, total: number | null): number | null {
  if (total === null || !Number.isFinite(total) || total <= 0) return null;
  const ratio = downloaded / total;
  return Math.max(0, Math.min(1, ratio)) * 100;
}

/** What the notice says, for each state that says anything at all. */
export function updateMessage(state: UpdateState): string | null {
  switch (state.status) {
    case "available":
      return `Version ${state.update.version} is available.`;
    case "downloading":
      return state.percent === null
        ? `Downloading version ${state.update.version}…`
        : `Downloading version ${state.update.version} — ${Math.round(state.percent)}%`;
    case "installed":
      return `Version ${state.update.version} is installed. Restart to finish.`;
    case "error":
      return `Could not check for updates: ${state.message}`;
    // "checking" and "up-to-date" say nothing: an app that announces it is
    // looking for updates, or that there are none, is interrupting to report
    // that nothing happened.
    case "idle":
    case "checking":
    case "up-to-date":
      return null;
  }
}

/** Whether this state should be shown at all. */
export function isVisible(state: UpdateState): boolean {
  return updateMessage(state) !== null;
}
