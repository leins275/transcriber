/**
 * Composes the T6 presentational components with the T12 IPC layer. Owns no
 * formatting logic of its own: state comes from `api.ts`/`useJobs`, rendering
 * is delegated to the components. First-run (no meetings-root, or a
 * meetings-root but no model yet) replaces the drop zone and job ledger with
 * the numbered setup card and refuses drops (FR-18).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { AppHeader } from "./components/AppHeader";
import { DropZone, type DropZoneState } from "./components/DropZone";
import { FirstRun } from "./components/FirstRun";
import { RecordingPage } from "./components/RecordingPage";
import { ModelDownloadStep } from "./components/ModelDownloadStep";
import { ServiceBanner } from "./components/ServiceBanner";
import { SettingsPage } from "./components/SettingsPage";
import { UpdateNotice } from "./components/UpdateNotice";
import { VaultPanel } from "./components/VaultPanel";
import {
  api,
  appVersion,
  chooseFile,
  chooseMeetingsFolder,
  onDragDrop,
  onServiceStatus,
  safeUnlisten,
} from "./api";
import { activeJobView } from "./lib/activeJob";
import type { ModelDownloadStatus } from "./lib/modelDownload";
import { projectCodes } from "./lib/vaultGroups";
import { useJobs } from "./state/useJobs";
import { useUpdate } from "./state/useUpdate";
import { useVault } from "./state/useVault";
import type {
  AppError,
  JobType,
  LlmModelsView,
  MeetingUpdate,
  ServiceStatusView,
  SettingsView,
  TranscriptLanguage,
} from "./types";

const INITIAL_SERVICE_STATUS: ServiceStatusView = {
  state: "starting",
  base_url: null,
  detail: null,
};

/** How long the bottom-of-pane error notice stays up before dismissing
 * itself. Errors here are per-action ("that drop was rejected", "reveal
 * failed") -- said once, they are stale the moment the operator moves on,
 * so they must not accumulate at the bottom of the pane for the rest of
 * the session. */
export const ERROR_DISMISS_MS = 6000;

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
  const { jobs, enqueue, upsert: upsertJob } = useJobs();
  const update = useUpdate();
  // Vault browser: a persistent list of already-ingested meetings (the
  // "after reopen I don't see already uploaded recordings" problem), plus
  // the per-meeting actions over them.
  const {
    entries: vaultEntries,
    refresh: refreshVault,
    reveal: revealVaultEntry,
    readTranscript,
    readSummary,
    readNote,
    saveNote,
    saveSpeakers,
    update: updateVaultEntry,
    remove: deleteVaultEntry,
    loadServiceLog,
  } = useVault();
  // Which recording is open, or `null` for the library. A single piece of
  // state rather than a router: this app has a handful of places to be, and
  // a URL would be a fiction in a window with no address bar.
  const [openEntryId, setOpenEntryId] = useState<string | null>(null);
  // The library's filter controls, lifted here so they survive the
  // VaultPanel unmount that opening a recording (or Settings) causes --
  // coming back must not silently reset the operator's project filter.
  const [vaultFilter, setVaultFilter] = useState<string>("");
  const [vaultGrouped, setVaultGrouped] = useState(false);
  // The Settings page (redesign turn 6): the old sidebar's vault/model/
  // service content, behind the header's gear. Rendered over whatever else
  // is open; closing it returns there untouched.
  const [settingsOpen, setSettingsOpen] = useState(false);

  // The installed build's own version, for the sidebar. `null` until (or
  // unless) the app plugin answers -- the sidebar simply omits the line
  // rather than showing a placeholder.
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then(setSettings)
      .catch((error: AppError) => setLastError(error));
    appVersion()
      .then(setVersion)
      .catch(() => {
        // Cosmetic only: a failed lookup must never surface as an error.
      });
  }, []);

  // Transient notification: each error replaces the last and dismisses
  // itself. Re-armed per error object, so a new failure arriving mid-count
  // gets its own full stretch on screen.
  useEffect(() => {
    if (!lastError) return;
    const handle = setTimeout(() => setLastError(null), ERROR_DISMISS_MS);
    return () => clearTimeout(handle);
  }, [lastError]);

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

  // The assistant (LLM) model catalog, same gating; polled while any
  // model's download is in flight so Settings shows live progress. The
  // "ready" gate also refetches after a model switch: selecting a model
  // restarts the sidecar, and the status walks back to ready.
  const [llmModels, setLlmModels] = useState<LlmModelsView | null>(null);
  useEffect(() => {
    if (serviceStatus.state !== "ready") return;
    api
      .listLlmModels()
      .then(setLlmModels)
      .catch(() => {
        // Older service / unreachable: stays `null`, the row renders inert.
      });
  }, [serviceStatus.state]);
  const anyLlmTransferring =
    llmModels?.models.some(
      (model) => model.download.state === "downloading" || model.download.state === "verifying",
    ) ?? false;
  useEffect(() => {
    if (!anyLlmTransferring) return;
    const handle = setInterval(() => {
      api
        .listLlmModels()
        .then(setLlmModels)
        .catch(() => {});
    }, 1500);
    return () => clearInterval(handle);
  }, [anyLlmTransferring]);

  const handleStartLlmModelDownload = useCallback((modelId: string) => {
    api
      .startLlmModelDownloadFor(modelId)
      .then(setLlmModels)
      .catch((error: AppError) => setLastError(error));
  }, []);

  const handleCancelLlmModelDownload = useCallback((modelId: string) => {
    api
      .cancelLlmModelDownloadFor(modelId)
      .then(setLlmModels)
      .catch((error: AppError) => setLastError(error));
  }, []);

  const modelDownloadCommands = useRef({
    start: () => api.startModelDownload(),
    cancel: () => api.cancelModelDownload(),
    status: () => api.modelDownloadStatus(),
  }).current;

  // Loads on startup once settings resolve to a meetings root that is both
  // set and exists (spec: "Loads on startup once settings resolve") --
  // first-run (no root yet) never calls list_vault at all.
  useEffect(() => {
    if (!settings?.meetings_root || !settings.meetings_root_exists) return;
    void refreshVault();
  }, [settings?.meetings_root, settings?.meetings_root_exists, refreshVault]);

  // Refreshes after each job reaches a terminal *filed* state (done, or
  // failed-but-already-ingested per FR-13) -- simplest re-fetch trigger per
  // the vault-browser spec, rather than a dedicated Rust-side event. Tracks
  // which job ids have already triggered a refresh so a job's other
  // (non-terminal) transitions, or a later render with the same jobs array,
  // never cause a redundant re-fetch.
  const refreshedForJobRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const alreadyRefreshed = refreshedForJobRef.current;
    let shouldRefresh = false;
    for (const job of jobs) {
      if ((job.state === "done" || job.state === "failed") && !alreadyRefreshed.has(job.id)) {
        alreadyRefreshed.add(job.id);
        shouldRefresh = true;
      }
    }
    if (shouldRefresh) {
      void refreshVault();
    }
  }, [jobs, refreshVault]);

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

  const handleRevealVaultEntry = useCallback(
    (entryId: string) => {
      revealVaultEntry(entryId).catch((error: AppError) => setLastError(error));
    },
    [revealVaultEntry],
  );

  // Rename/delete deliberately re-throw rather than routing through
  // `setLastError`: the row that triggered the action shows the failure
  // beside the form the operator is still looking at, which is far more
  // useful than a banner at the bottom of the pane.
  const handleUpdateVaultEntry = useCallback(
    (entryId: string, update: MeetingUpdate) => updateVaultEntry(entryId, update),
    [updateVaultEntry],
  );

  const handleDeleteVaultEntry = useCallback(
    async (entryId: string) => {
      await deleteVaultEntry(entryId);
      // The page the operator is on has just ceased to exist.
      setOpenEntryId((current) => (current === entryId ? null : current));
    },
    [deleteVaultEntry],
  );

  const handleCancelJob = useCallback((jobId: string) => {
    api.cancelJob(jobId).catch((error: AppError) => setLastError(error));
  }, []);

  const handleTranscribe = useCallback(
    async (entryId: string, language: TranscriptLanguage | null) => {
      const snapshot = await api.transcribeVaultEntry(entryId, language);
      // Show it in the pinned list immediately; its later transitions
      // arrive through `jobs://updated` like any other job's.
      upsertJob(snapshot);
    },
    [upsertJob],
  );

  // The LLM feature's on-demand jobs: each enqueues on the Rust side and
  // pins the returned snapshot immediately, exactly like Transcribe.
  const handleSummarize = useCallback(
    async (entryId: string) => {
      upsertJob(await api.summarizeVaultEntry(entryId));
    },
    [upsertJob],
  );

  const handleExportPdf = useCallback(
    async (entryId: string) => {
      upsertJob(await api.exportRecording(entryId));
    },
    [upsertJob],
  );

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

  // Derived, never stored: `update` replaces the entry in place and a
  // refresh reissues every id, so holding the entry itself would leave the
  // page rendering a stale copy of a meeting that has since moved.
  const openEntry = vaultEntries.find((entry) => entry.id === openEntryId) ?? null;

  // Derived-job bookkeeping for the open recording: which LLM jobs are still
  // in flight for it (its buttons render busy), and how many summarize jobs
  // have finished for it (the summary tab re-reads).
  const activeLlmJobs: JobType[] = openEntry
    ? jobs
        .filter(
          (job) =>
            job.job_type !== "transcribe" &&
            (job.state === "pending" || job.state === "queued" || job.state === "running") &&
            job.source_path === openEntry.meeting_dir,
        )
        .map((job) => job.job_type)
    : [];
  const summaryReloadToken = openEntry
    ? jobs.filter(
        (job) =>
          job.job_type === "summarize" &&
          job.state === "done" &&
          job.source_path === openEntry.meeting_dir,
      ).length
    : 0;

  // The header narrates the in-flight job only while the operator is away
  // from the main view (mockup 7a) -- on the library the queue itself is
  // already on screen, and a finished job must never yank anyone off the
  // page they are reading; the chip is the deliberate way back.
  const onMainView = !settingsOpen && !openEntry;
  const headerJob = onMainView ? null : activeJobView(jobs);
  const showRecordings = useCallback(() => {
    setOpenEntryId(null);
    setSettingsOpen(false);
  }, []);

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
      <AppHeader
        serviceStatus={serviceStatus}
        modelStatus={modelStatus}
        settingsOpen={settingsOpen}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
        activeJob={headerJob}
        onShowRecordings={showRecordings}
      />
      <main className="main-pane">
        {/* Above the config error and everything else: it is the one notice
            that is about the app itself rather than about the operator's
            vault, and it disappears on its own when there is nothing to
            say. */}
        <UpdateNotice
          state={update.state}
          onInstall={update.install}
          onRestart={update.restart}
          onDismiss={update.dismiss}
        />

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
          (settingsOpen ? (
            <SettingsPage
              settings={settings}
              serviceStatus={serviceStatus}
              modelStatus={modelStatus}
              llmModels={llmModels}
              appVersion={version}
              onBack={() => setSettingsOpen(false)}
              onChangeRoot={handleChooseFolder}
              onStartLlmModelDownload={handleStartLlmModelDownload}
              onCancelLlmModelDownload={handleCancelLlmModelDownload}
            />
          ) : inSetup ? (
            <FirstRun
              meetingsRoot={meetingsRoot}
              onChooseFolder={handleChooseFolder}
              modelStep={modelStepElement}
            />
          ) : (
            <>
              <ServiceBanner status={serviceStatus} />
              {modelStepElement}
              {openEntry ? (
                <RecordingPage
                  entry={openEntry}
                  projects={projectCodes(vaultEntries)}
                  onBack={() => setOpenEntryId(null)}
                  onReveal={handleRevealVaultEntry}
                  onReadTranscript={readTranscript}
                  onReadSummary={readSummary}
                  onReadNote={readNote}
                  onSaveNote={saveNote}
                  onSaveSpeakers={saveSpeakers}
                  onUpdate={handleUpdateVaultEntry}
                  onDelete={handleDeleteVaultEntry}
                  onTranscribe={handleTranscribe}
                  onSummarize={handleSummarize}
                  onExportPdf={handleExportPdf}
                  activeLlmJobs={activeLlmJobs}
                  summaryReloadToken={summaryReloadToken}
                />
              ) : (
                <>
                  {vaultEntries.length === 0 && jobs.length === 0 ? (
                    <DropZone
                      variant="hero"
                      state={dropState}
                      disabled={false}
                      onChooseFile={handleChooseFile}
                    />
                  ) : (
                    <DropZone
                      variant="strip"
                      state={dropState}
                      disabled={false}
                      onChooseFile={handleChooseFile}
                    />
                  )}
                  <VaultPanel
                    entries={vaultEntries}
                    jobs={jobs}
                    filter={vaultFilter}
                    onFilterChange={setVaultFilter}
                    grouped={vaultGrouped}
                    onGroupedChange={setVaultGrouped}
                    onOpen={setOpenEntryId}
                    onRevealJob={handleReveal}
                    onCancelJob={handleCancelJob}
                    onLoadServiceLog={loadServiceLog}
                  />
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
