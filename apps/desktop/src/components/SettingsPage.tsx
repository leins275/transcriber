import styles from "./SettingsPage.module.css";
import { serviceStatusLabel } from "../lib/serviceLabel";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { LlmModelDownloadStatus, ServiceStatusView, SettingsView } from "../types";

export type SettingsPageProps = {
  settings: SettingsView;
  serviceStatus: ServiceStatusView;
  modelStatus: ModelDownloadStatus | null;
  /** The GGUF (assistant LLM) model's status -- `null` while unknown, which
   * omits the actions rather than showing a false "missing". */
  llmModelStatus: LlmModelDownloadStatus | null;
  /** The installed build's version, once known -- `null` simply omits the
   * row rather than showing a placeholder. */
  appVersion: string | null;
  onBack: () => void;
  onChangeRoot: () => void;
  onStartLlmDownload: () => void;
  onCancelLlmDownload: () => void;
};

function extensionList(extensions: string[]): string {
  return extensions.map((ext) => ext.replace(/^\./, "")).join(" · ");
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
  llmModelStatus,
  appVersion,
  onBack,
  onChangeRoot,
  onStartLlmDownload,
  onCancelLlmDownload,
}: SettingsPageProps) {
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
          {llmModelStatus === null ? (
            <div className={styles.line}>Local language model</div>
          ) : llmModelStatus.model_present &&
            (llmModelStatus.state === "downloading" || llmModelStatus.state === "verifying") ? (
            // The GGUF is here; what's transferring is the GPU build.
            <div className={styles.line}>
              Downloading GPU acceleration · {Math.round(llmModelStatus.percent)}%
              <button type="button" className="btn btn-ghost" onClick={onCancelLlmDownload}>
                Cancel
              </button>
            </div>
          ) : llmModelStatus.model_present ? (
            <>
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
                Local language model installed
                {llmModelStatus.gpu_build_present === false && (
                  <button type="button" className="btn btn-secondary" onClick={onStartLlmDownload}>
                    Enable GPU acceleration (~460 MB)
                  </button>
                )}
              </div>
              <p className={styles.hint}>
                {llmModelStatus.gpu_build_present === false
                  ? "Summaries currently run on CPU. Enabling GPU acceleration downloads the " +
                    "CUDA build of the local runtime and offloads as much of the model as " +
                    "fits in your GPU's memory."
                  : "Summaries, action items, facts and project reports run on this machine."}
              </p>
            </>
          ) : llmModelStatus.state === "downloading" || llmModelStatus.state === "verifying" ? (
            <>
              <div className={styles.line}>
                Downloading the language model · {Math.round(llmModelStatus.percent)}%
                <button type="button" className="btn btn-ghost" onClick={onCancelLlmDownload}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <>
              <div className={styles.line}>
                Local language model not installed
                <button type="button" className="btn btn-secondary" onClick={onStartLlmDownload}>
                  Download (~20 GB)
                </button>
              </div>
              {llmModelStatus.error_message && (
                <p className={styles.warning}>{llmModelStatus.error_message}</p>
              )}
              <p className={styles.hint}>
                Needed for summaries, action items, facts and project reports. Everything runs
                locally; nothing leaves this machine.
              </p>
            </>
          )}
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
