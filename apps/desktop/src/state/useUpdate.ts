import { useCallback, useEffect, useRef, useState } from "react";
import { checkForUpdate, relaunchApp, type PendingUpdate } from "../api";
import { downloadPercent, type UpdateInfo, type UpdateState } from "../lib/update";

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
      await update.downloadAndInstall((event) => {
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
      setState({ status: "installed", update: info });
    } catch (error: unknown) {
      const message =
        typeof error === "object" && error !== null && "message" in error
          ? String((error as { message: unknown }).message)
          : String(error);
      setState({ status: "error", message });
    }
  }, []);

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
