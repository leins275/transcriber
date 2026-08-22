import styles from "./Sidebar.module.css";
import { SettingsBar } from "./SettingsBar";
import { serviceStatusLabel } from "../lib/serviceLabel";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { ServiceStatusView, SettingsView } from "../types";

export type SidebarProps = {
  /** `"setup"`: the reduced column shown alongside the first-run setup card
   * (spec.md 2a) -- vault/model/service aren't known yet, or are already
   * spelled out in the setup card itself, so this stays a short strapline.
   * `"full"`: the normal working column with Vault/Model/Service. */
  variant: "setup" | "full";
  settings: SettingsView;
  serviceStatus: ServiceStatusView;
  modelStatus: ModelDownloadStatus | null;
  onChangeRoot: () => void;
};

function extensionList(extensions: string[]): string {
  return extensions.map((ext) => ext.replace(/^\./, "")).join(" ");
}

/** The 264px left column: brand, then either the setup strapline or the
 * Vault/Model/Service status trio, with the accepted-extensions list
 * pinned to the bottom. Presentational only: no invoke, no listen, no
 * fetch (T6) -- `SettingsBar` remains the only piece that owns the Vault
 * region's markup. */
export function Sidebar({
  variant,
  settings,
  serviceStatus,
  modelStatus,
  onChangeRoot,
}: SidebarProps) {
  return (
    <aside className={styles.sidebar}>
      <div>
        <div className={styles.kicker}>Local · Nothing uploaded</div>
        <div className={styles.brand}>Transcriber</div>
      </div>
      <div className={styles.divider} />

      {variant === "setup" ? (
        <p className={styles.strapline}>
          Meeting recordings are filed and transcribed entirely on this machine.
        </p>
      ) : (
        <>
          <SettingsBar settings={settings} onChangeRoot={onChangeRoot} />

          <div className={styles.section}>
            <div className={styles.sectionKicker}>Model</div>
            {modelStatus &&
              (modelStatus.model_present ? (
                <div className={styles.modelLine}>
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
                <div className={styles.modelLine}>large-v3 not installed</div>
              ))}
          </div>

          <div className={styles.section}>
            <div className={styles.sectionKicker}>Service</div>
            <div className={styles.serviceLine}>
              <span className={styles.dot} data-state={serviceStatus.state} />
              {serviceStatusLabel(serviceStatus.state, modelStatus?.cuda_runtime_present)}
            </div>
            {serviceStatus.state === "unavailable" && (
              <div className={styles.serviceNote}>Filing still works.</div>
            )}
          </div>
        </>
      )}

      <div className={styles.accepts}>
        <div className={styles.sectionKicker}>Accepts</div>
        <div className={`${styles.acceptsList} mono`}>
          {extensionList(settings.supported_extensions)}
        </div>
      </div>
    </aside>
  );
}
