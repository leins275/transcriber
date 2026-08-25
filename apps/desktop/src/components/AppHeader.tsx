import styles from "./AppHeader.module.css";
import { Logo } from "./Logo";
import { serviceStatusLabel } from "../lib/serviceLabel";
import type { ActiveJobView } from "../lib/activeJob";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { ServiceStatusView } from "../types";

export type AppHeaderProps = {
  serviceStatus: ServiceStatusView;
  modelStatus: ModelDownloadStatus | null;
  /** Whether the Settings page is open -- the gear stays visibly active so
   * the header itself says where you are. */
  settingsOpen: boolean;
  onToggleSettings: () => void;
  /** The in-flight job the header narrates instead of the service-status
   * chip (mockup 7a) -- passed only while the operator is away from the
   * main view, where the queue itself is not visible. `null` keeps the
   * plain status chip. */
  activeJob?: ActiveJobView | null;
  /** Fired from the progress chip -- returns to the Recordings list, where
   * the full queue lives. */
  onShowRecordings?: () => void;
};

/**
 * The slim header that replaced the 264px sidebar (redesign turn 6): brand,
 * a one-line live status chip, and the gear that opens Settings. Everything
 * the sidebar used to explain at length -- vault path, model, accepted
 * formats -- lives on the Settings page now; only what changes while you
 * watch (service state) stays permanently on screen.
 *
 * While a job is in flight and the operator is away from the main view, the
 * status chip gives way to that job's live progress (mockup 7a) -- the one
 * place the running pipeline stays visible without yanking anyone back to
 * the library; clicking it returns to Recordings deliberately instead.
 *
 * Presentational only: no invoke, no listen, no fetch (T6).
 */
export function AppHeader({
  serviceStatus,
  modelStatus,
  settingsOpen,
  onToggleSettings,
  activeJob = null,
  onShowRecordings,
}: AppHeaderProps) {
  const label = serviceStatusLabel(serviceStatus.state, modelStatus?.cuda_runtime_present);
  const modelSuffix = modelStatus?.model_present ? " · large-v3" : "";

  return (
    <header className={styles.header}>
      <Logo size={22} />
      <span className={styles.brand}>Transcriber</span>
      <div className={styles.right}>
        {activeJob ? (
          <button
            type="button"
            className={styles.jobChip}
            title="Back to Recordings"
            onClick={onShowRecordings}
          >
            <svg
              className={styles.jobSpinner}
              aria-hidden="true"
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            >
              <circle cx="12" cy="12" r="9" strokeDasharray="34 22"></circle>
            </svg>
            <span className={styles.jobLabel}>{activeJob.label}</span>
            {activeJob.percent != null && (
              <span className={styles.jobPercent}>· {activeJob.percent}%</span>
            )}
          </button>
        ) : (
          <span className={styles.status}>
            <span className={styles.dot} data-state={serviceStatus.state} />
            {label}
            {modelSuffix}
          </span>
        )}
        <span className={styles.divider} aria-hidden="true" />
        <button
          type="button"
          className={styles.gear}
          aria-label="Settings"
          aria-pressed={settingsOpen}
          onClick={onToggleSettings}
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="12" cy="12" r="3"></circle>
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
          </svg>
        </button>
      </div>
    </header>
  );
}
