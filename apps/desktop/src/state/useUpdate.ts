import { useCallback, useEffect, useRef, useState } from "react";
import { api, checkForUpdate, relaunchApp, type PendingUpdate } from "../api";
import { downloadPercent, type UpdateInfo, type UpdateState } from "../lib/update";

/** How long a failed check's notice stays up before dismissing itself. Long
 * enough to be read, short enough that an offline machine's app is not
 * permanently wearing an error banner for the normal state of being
 * offline. */
export const UPDATE_ERROR_DISMISS_MS = 6000;

/** The plugin reports absent release notes and dates as `undefined`; every
 * view type in this app uses `null` for "not present", and mixing the two
 * is how a `?? "none"` fallback ends up firing on one and not the other. */
function describe(update: PendingUpdate): UpdateInfo {
  return {
    version: update.version,
    notes: update.body ?? null,
    date: update.date ?? null,
  };
}

/**
 * Checks for an update once at launch, and drives install/restart.
 *
 * Once, not on a timer. This app is opened to deal with a recording and then
 * closed; a background poll would buy nothing a launch check does not
 * already give, and would mean network activity at a moment nobody asked
 * for it — which is a promise this app makes rather carefully ("Local ·
 * Nothing uploaded").
 *
 * A failed check is reported, never thrown. Not being able to reach GitHub
 * is the normal state of an offline machine, and it must not be able to
 * interfere with transcribing.
 */
export function useUpdate() {
  const [state, setState] = useState<UpdateState>({ status: "idle" });
  // Held so Install can act on the same handle the check produced, rather
  // than checking a second time and racing a release published in between.
  const pending = useRef<PendingUpdate | null>(null);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "checking" });
    checkForUpdate()
      .then((update) => {
        if (cancelled) return;
        if (update === null) {
          setState({ status: "up-to-date" });
          return;
        }
        pending.current = update;
        setState({ status: "available", update: describe(update) });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message =
          typeof error === "object" && error !== null && "message" in error
            ? String((error as { message: unknown }).message)
            : String(error);
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const install = useCallback(async () => {
    const update = pending.current;
    if (update === null) return;
    const info = describe(update);
    setState({ status: "downloading", update: info, percent: null });

    let downloaded = 0;
    let total: number | null = null;
    try {
      // Download and install are two deliberate steps, not
      // `downloadAndInstall`: between them the bundled Python sidecar must
      // be stopped (`prepare_update`), because the NSIS installer
      // overwrites `pyenv\` in place and a still-running interpreter holds
      // locks on its own DLLs -- the "Error opening file for writing:
      // ...\pyenv\..." install failure. Stopping only after the download
      // has succeeded also means a failed/offline download never takes the
      // transcription service down for nothing.
      await update.download((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? null;
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength;
          setState({
            status: "downloading",
            update: info,
            percent: downloadPercent(downloaded, total),
          });
        }
      });
      await api.prepareUpdate();
      await update.install();
      setState({ status: "installed", update: info });
    } catch (error: unknown) {
      const message =
        typeof error === "object" && error !== null && "message" in error
          ? String((error as { message: unknown }).message)
          : String(error);
      setState({ status: "error", message });
    }
  }, []);

  // A failed check dismisses itself. It is worth saying once -- the app
  // cannot tell whether it is current -- but it is a notification, not a
  // condition the operator can act on, so it must not sit on screen for
  // the rest of the session. The actionable states ("available",
  // "installed") never auto-dismiss: hiding a button someone was about to
  // click is worse than a lingering strip.
  useEffect(() => {
    if (state.status !== "error") return;
    const handle = setTimeout(() => setState({ status: "idle" }), UPDATE_ERROR_DISMISS_MS);
    return () => clearTimeout(handle);
  }, [state.status]);

  const restart = useCallback(() => {
    relaunchApp().catch(() => {
      // Nothing useful to say: the update is installed either way, and it
      // takes effect the next time the app opens.
      setState({ status: "idle" });
    });
  }, []);

  const dismiss = useCallback(() => setState({ status: "idle" }), []);

  return { state, install, restart, dismiss };
}
