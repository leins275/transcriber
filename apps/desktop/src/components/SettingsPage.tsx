import { useState } from "react";
import styles from "./SettingsPage.module.css";
import { serviceStatusLabel } from "../lib/serviceLabel";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { LlmCatalogModel, LlmModelsView, ServiceStatusView, SettingsView } from "../types";

export type SettingsPageProps = {
  settings: SettingsView;
  serviceStatus: ServiceStatusView;
  modelStatus: ModelDownloadStatus | null;
  /** The curated assistant (LLM) model catalog -- `null` while unknown,
   * which renders the row inert rather than showing a false "missing". */
  llmModels: LlmModelsView | null;
  /** The installed build's version, once known -- `null` simply omits the
   * row rather than showing a placeholder. */
  appVersion: string | null;
  onBack: () => void;
  onChangeRoot: () => void;
  onStartLlmModelDownload: (modelId: string) => void;
  onCancelLlmModelDownload: (modelId: string) => void;
  /** Queues an incremental search-index pass over the whole vault. */
  onReindex: () => Promise<void>;
};

function extensionList(extensions: string[]): string {
  return extensions.map((ext) => ext.replace(/^\./, "")).join(" · ");
}

function sizeLabel(sizeBytes: number | null): string {
  if (sizeBytes === null) return "";
  return `~${(sizeBytes / 1_000_000_000).toFixed(1)} GB`;
}

function isTransferring(model: LlmCatalogModel): boolean {
  return model.download.state === "downloading" || model.download.state === "verifying";
}

const CHECK_ICON = (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="var(--accent)"
    strokeWidth="2.5"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polyline points="20 6 9 17 4 12"></polyline>
  </svg>
);

/** The one curated model's row in the Assistant section. The catalog is
 * deliberately a single model with no switching, so the row's only actions
 * are download-shaped. */
function LlmModelRow({
  model,
  gpuBuildPresent,
  anyTransferring,
  onStartDownload,
  onCancelDownload,
}: {
  model: LlmCatalogModel;
  gpuBuildPresent: boolean | null;
  anyTransferring: boolean;
  onStartDownload: (modelId: string) => void;
  onCancelDownload: (modelId: string) => void;
}) {
  const transferring = isTransferring(model);
  const size = sizeLabel(model.size_bytes);
  return (
    <div>
      <div className={styles.line}>
        {model.present && CHECK_ICON}
        {model.label}
        {size && <span className={styles.detail}>{size}</span>}
        {transferring ? (
          <>
            {model.present
              ? `Downloading GPU acceleration · ${Math.round(model.download.percent)}%`
              : `Downloading · ${Math.round(model.download.percent)}%`}
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => onCancelDownload(model.id)}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            {!model.present && (
              <button
                type="button"
                className="btn btn-secondary"
                disabled={anyTransferring}
                onClick={() => onStartDownload(model.id)}
              >
                Download {size && `(${size})`}
              </button>
            )}
            {model.present && gpuBuildPresent === false && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => onStartDownload(model.id)}
              >
                Enable GPU acceleration (~460 MB)
              </button>
            )}
          </>
        )}
      </div>
      {model.download.error_message && (
        <p className={styles.warning}>{model.download.error_message}</p>
      )}
    </div>
  );
}

/**
 * The Settings page (redesign turn 6): everything the old sidebar carried --
 * vault, model, service, accepted formats -- as a labelled ledger, reached
 * from the header's gear. A page rather than a modal for the same reason the
 * recording view is a page: this app has a handful of places to be, and
 * each gets the whole window.
 *
 * Presentational only: no invoke, no listen, no fetch (T6) -- App.tsx owns
 * the data and passes every action down.
 */
export function SettingsPage({
  settings,
  serviceStatus,
  modelStatus,
  llmModels,
  appVersion,
  onBack,
  onChangeRoot,
  onStartLlmModelDownload,
  onCancelLlmModelDownload,
  onReindex,
}: SettingsPageProps) {
  const anyLlmTransferring = llmModels?.models.some(isTransferring) ?? false;
  const [reindexState, setReindexState] = useState<"idle" | "queueing" | "queued" | "failed">(
    "idle",
  );
  const [reindexError, setReindexError] = useState<string | null>(null);

  const reindex = () => {
    setReindexState("queueing");
    setReindexError(null);
    onReindex()
      .then(() => setReindexState("queued"))
      .catch((caught: unknown) => {
        setReindexState("failed");
        const message =
          typeof caught === "object" && caught !== null && "message" in caught
            ? String((caught as { message: unknown }).message)
            : String(caught);
        setReindexError(message);
      });
  };
  return (
    <section className={styles.page} role="region" aria-label="Settings">
      <button type="button" className={`btn btn-ghost ${styles.back}`} onClick={onBack}>
        ← Recordings
      </button>
      <h2 className={styles.title}>Settings</h2>

      <div className={styles.row}>
        <div className={styles.kicker}>Vault</div>
        <div className={styles.value}>
          <div className={styles.line}>
            <span className={`${styles.path} mono`}>{settings.meetings_root ?? "(not set)"}</span>
            <button type="button" className="btn btn-secondary" onClick={onChangeRoot}>
              Change…
            </button>
          </div>
          {settings.meetings_root && !settings.meetings_root_exists && (
            <p className={styles.warning}>
              This folder no longer exists. Choose a valid meetings folder.
            </p>
          )}
          <p className={styles.hint}>
            Where recordings and transcripts are filed. Everything stays on this machine.
          </p>
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Model</div>
        <div className={styles.value}>
          {modelStatus ? (
            modelStatus.model_present ? (
              <div className={styles.line}>
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="var(--accent)"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
                large-v3 installed
              </div>
            ) : (
              <>
                <div className={styles.line}>large-v3 not installed</div>
                <p className={styles.hint}>Download it from the notice on the Recordings page.</p>
              </>
            )
          ) : (
            <div className={styles.line}>large-v3</div>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Assistant</div>
        <div className={styles.value}>
          {llmModels === null ? (
            <div className={styles.line}>Local language model</div>
          ) : (
            <>
              {llmModels.models.map((model) => (
                <LlmModelRow
                  key={model.id}
                  model={model}
                  gpuBuildPresent={llmModels.gpu_build_present}
                  anyTransferring={anyLlmTransferring}
                  onStartDownload={onStartLlmModelDownload}
                  onCancelDownload={onCancelLlmModelDownload}
                />
              ))}
              <p className={styles.hint}>
                {llmModels.gpu_build_present === false
                  ? "Summaries currently run on CPU. Enabling GPU acceleration downloads the " +
                    "CUDA build of the local runtime and offloads as much of the model as " +
                    "fits in your GPU's memory."
                  : "Summaries and action items run on this machine, on the one built-in model."}
              </p>
            </>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Search</div>
        <div className={styles.value}>
          <div className={styles.line}>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={reindexState === "queueing" || serviceStatus.state !== "ready"}
              onClick={reindex}
            >
              {reindexState === "queueing" ? "Queueing…" : "Rebuild search index"}
            </button>
            {reindexState === "queued" && (
              <span className={styles.detail}>Queued — runs after any current job.</span>
            )}
          </div>
          {reindexError && <p className={styles.warning}>{reindexError}</p>}
          <p className={styles.hint}>
            The index updates itself after every transcription and note save; this catches up a
            vault that changed outside the app. Incremental — unchanged meetings are skipped.
          </p>
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Service</div>
        <div className={styles.value}>
          <div className={styles.line}>
            <span className={styles.dot} data-state={serviceStatus.state} />
            {serviceStatusLabel(serviceStatus.state, modelStatus?.cuda_runtime_present)}
          </div>
          {serviceStatus.base_url && (
            <div className={`${styles.detail} mono`}>{serviceStatus.base_url}</div>
          )}
          {serviceStatus.state === "unavailable" && (
            <p className={styles.hint}>Filing still works.</p>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Accepted formats</div>
        <div className={styles.value}>
          <div className={`${styles.detail} mono`}>
            {extensionList(settings.supported_extensions)}
          </div>
        </div>
      </div>

      {appVersion && (
        <div className={styles.row}>
          <div className={styles.kicker}>Version</div>
          <div className={styles.value}>
            <div className={styles.line}>Transcriber v{appVersion}</div>
          </div>
        </div>
      )}
    </section>
  );
}
