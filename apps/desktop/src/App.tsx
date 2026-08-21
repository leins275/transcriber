/**
 * Composes the T6 presentational components with the T12 IPC layer. Owns no
 * formatting logic of its own: state comes from `api.ts`/`useJobs`, rendering
 * is delegated to the components. First-run (no meetings-root) replaces the
 * drop zone with the folder-picker prompt and refuses drops (FR-18).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { DropZone, type DropZoneState } from "./components/DropZone";
import { FirstRun } from "./components/FirstRun";
import { JobList } from "./components/JobList";
import { ServiceBanner } from "./components/ServiceBanner";
import { SettingsBar } from "./components/SettingsBar";
import {
  api,
  chooseFile,
  chooseMeetingsFolder,
  onDragDrop,
  onServiceStatus,
  safeUnlisten,
} from "./api";
import { useJobs } from "./state/useJobs";
import type { AppError, ServiceStatusView, SettingsView } from "./types";

const INITIAL_SERVICE_STATUS: ServiceStatusView = {
  state: "starting",
  base_url: null,
  detail: null,
};

function App() {
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [serviceStatus, setServiceStatus] = useState<ServiceStatusView>(INITIAL_SERVICE_STATUS);
  const [dropState, setDropState] = useState<DropZoneState>("idle");
  const [lastError, setLastError] = useState<AppError | null>(null);
  const { jobs, enqueue } = useJobs();

  useEffect(() => {
    api
      .getSettings()
      .then(setSettings)
      .catch((error: AppError) => setLastError(error));
  }, []);

  useEffect(() => {
    api
      .serviceStatus()
      .then(setServiceStatus)
      .catch(() => {
        // service_status itself failing is surfaced by staying in "starting";
        // service://status events are the primary channel for this state.
      });

    let cancelled = false;
    let unlisten: (() => void) | undefined;
    onServiceStatus(setServiceStatus).then((fn) => {
      if (cancelled) {
        safeUnlisten(fn);
      } else {
        unlisten = fn;
      }
    });
    return () => {
      cancelled = true;
      safeUnlisten(unlisten);
    };
  }, []);

  const submitPaths = useCallback(
    async (paths: string[]) => {
      if (paths.length === 0) return;
      setDropState("working");
      try {
        await enqueue(paths);
      } catch (error) {
        setLastError(error as AppError);
      } finally {
        setDropState("idle");
      }
    },
    [enqueue],
  );

  const meetingsRoot = settings?.meetings_root ?? null;

  // Read via refs inside the listener so the drag-drop subscription itself
  // is registered exactly once (mount/unmount only), instead of tearing
  // down and re-subscribing every time settings or the submit callback
  // change identity.
  const meetingsRootRef = useRef(meetingsRoot);
  meetingsRootRef.current = meetingsRoot;
  const submitPathsRef = useRef(submitPaths);
  submitPathsRef.current = submitPaths;

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;

    onDragDrop((event) => {
      if (!meetingsRootRef.current) {
        // First-run: no meetings-root configured yet, refuse drops (FR-18).
        return;
      }
      switch (event.type) {
        case "enter":
        case "over":
          setDropState("hovering");
          break;
        case "leave":
          setDropState("idle");
          break;
        case "drop":
          void submitPathsRef.current(event.paths);
          break;
      }
    }).then((fn) => {
      if (cancelled) {
        safeUnlisten(fn);
      } else {
        unlisten = fn;
      }
    });

    return () => {
      cancelled = true;
      safeUnlisten(unlisten);
    };
  }, []);

  const handleChooseFile = useCallback(async () => {
    try {
      const paths = await chooseFile();
      await submitPaths(paths);
    } catch (error) {
      setLastError(error as AppError);
    }
  }, [submitPaths]);

  const handleChooseFolder = useCallback(async () => {
    try {
      const folder = await chooseMeetingsFolder();
      if (!folder) return;
      const updated = await api.setMeetingsRoot(folder);
      setSettings(updated);
    } catch (error) {
      setLastError(error as AppError);
    }
  }, []);

  const handleReveal = useCallback((jobId: string) => {
    api.revealJob(jobId).catch((error: AppError) => setLastError(error));
  }, []);

  return (
    <div className="app-shell">
      <h1>Transcriber</h1>
      {settings?.config_error && (
        // E3: a malformed config.json falls back to first-run defaults
        // instead of crashing before a window exists -- this is the
        // actionable error that fallback produced.
        <p role="alert" data-state="config-error">
          Your settings file could not be read and has been reset to defaults:{" "}
          {settings.config_error}
        </p>
      )}
      {settings && <SettingsBar settings={settings} onChangeRoot={handleChooseFolder} />}
      <ServiceBanner status={serviceStatus} />
      {settings &&
        (meetingsRoot ? (
          <DropZone state={dropState} disabled={false} onChooseFile={handleChooseFile} />
        ) : (
          <FirstRun onChooseFolder={handleChooseFolder} />
        ))}
      <section aria-label="Jobs" role="region">
        <JobList jobs={jobs} onReveal={handleReveal} />
      </section>
      {lastError && <p role="alert">{lastError.message}</p>}
    </div>
  );
}

export default App;
