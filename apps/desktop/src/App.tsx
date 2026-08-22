/**
 * Composes the T6 presentational components with the T12 IPC layer. Owns no
 * formatting logic of its own: state comes from `api.ts`/`useJobs`, rendering
 * is delegated to the components. First-run (no meetings-root, or a
 * meetings-root but no model yet) replaces the drop zone and job ledger with
 * the numbered setup card and refuses drops (FR-18).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { DropZone, type DropZoneState } from "./components/DropZone";
import { FirstRun } from "./components/FirstRun";
import { JobsPanel } from "./components/JobsPanel";
import { ModelDownloadStep } from "./components/ModelDownloadStep";
import { ServiceBanner } from "./components/ServiceBanner";
import { Sidebar } from "./components/Sidebar";
import {
  api,
  chooseFile,
  chooseMeetingsFolder,
  onDragDrop,
  onServiceStatus,
  safeUnlisten,
} from "./api";
import type { ModelDownloadStatus } from "./lib/modelDownload";
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
  // First-run model-download step (T13, FR-12, FR-16, FR-17). `null` means
  // "not fetched yet" -- rendering stays gated on a real status so a fetch
  // failure (service not yet ready) never flashes a false "model missing".
  const [modelStatus, setModelStatus] = useState<ModelDownloadStatus | null>(null);
  const [modelSkipped, setModelSkipped] = useState(false);
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

  // Fetches the model-download status once the sidecar is reachable (T13,
  // FR-17) -- gating on "ready" rather than firing at mount avoids a
  // spurious call while the service is still starting up.
  useEffect(() => {
    if (serviceStatus.state !== "ready") return;
    api
      .modelDownloadStatus()
      .then(setModelStatus)
      .catch(() => {
        // Unreachable/unsupported: stays `null`, so the step simply does not
        // render yet rather than showing a false "missing" state.
      });
  }, [serviceStatus.state]);

  const modelDownloadCommands = useRef({
    start: () => api.startModelDownload(),
    cancel: () => api.cancelModelDownload(),
    status: () => api.modelDownloadStatus(),
  }).current;

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
      const folder = await chooseMeetingsFolder(settings?.default_meetings_root);
      if (!folder) return;
      const updated = await api.setMeetingsRoot(folder);
      setSettings(updated);
    } catch (error) {
      setLastError(error as AppError);
    }
  }, [settings?.default_meetings_root]);

  const handleReveal = useCallback((jobId: string) => {
    api.revealJob(jobId).catch((error: AppError) => setLastError(error));
  }, []);

  // The first-run setup path (spec.md 2a) covers both "no folder yet" and
  // "folder chosen but the model isn't here yet" -- one coherent path
  // instead of three unrelated blocks. "Skip for now" (modelSkipped) exits
  // it into normal operation with a persistent, compact notice instead
  // (FR-17): the rest of the app stays usable underneath.
  const modelKnown = modelStatus !== null;
  const modelPresent = modelStatus?.model_present ?? false;
  // The folder gate always wins (FR-18): drops stay refused with no folder
  // chosen even if a stray "Skip for now" click on the model step somehow
  // preceded it. Once a folder is chosen, setup continues until the model
  // is present or the operator explicitly skips it.
  const inSetup = !meetingsRoot || (!modelSkipped && modelKnown && !modelPresent);

  const modelStepElement = modelStatus ? (
    <ModelDownloadStep
      commands={modelDownloadCommands}
      initialStatus={modelStatus}
      compact={modelSkipped}
      onModelPresent={() =>
        setModelStatus((current) => (current ? { ...current, model_present: true } : current))
      }
      onSkip={() => setModelSkipped(true)}
    />
  ) : null;

  return (
    <div className="app-shell">
      {settings && (
        <Sidebar
          variant={inSetup ? "setup" : "full"}
          settings={settings}
          serviceStatus={serviceStatus}
          modelStatus={modelStatus}
          onChangeRoot={handleChooseFolder}
        />
      )}
      <main className="main-pane">
        {settings?.config_error && (
          // E3: a malformed config.json falls back to first-run defaults
          // instead of crashing before a window exists -- this is the
          // actionable error that fallback produced.
          <p role="alert" data-state="config-error" className="alert">
            Your settings file could not be read and has been reset to defaults:{" "}
            {settings.config_error}
          </p>
        )}

        {settings &&
          (inSetup ? (
            <FirstRun
              meetingsRoot={meetingsRoot}
              onChooseFolder={handleChooseFolder}
              modelStep={modelStepElement}
            />
          ) : (
            <>
              <ServiceBanner status={serviceStatus} />
              {modelStepElement}
              {jobs.length === 0 ? (
                <DropZone
                  variant="hero"
                  state={dropState}
                  disabled={false}
                  onChooseFile={handleChooseFile}
                />
              ) : (
                <>
                  <DropZone
                    variant="strip"
                    state={dropState}
                    disabled={false}
                    onChooseFile={handleChooseFile}
                  />
                  <JobsPanel jobs={jobs} onReveal={handleReveal} />
                </>
              )}
            </>
          ))}

        {lastError && (
          <p role="alert" className="alert">
            {lastError.message}
          </p>
        )}
      </main>
    </div>
  );
}

export default App;
