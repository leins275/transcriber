import styles from "./SettingsBar.module.css";
import type { SettingsView } from "../types";

export type SettingsBarProps = {
  settings: SettingsView;
  onChangeRoot: () => void;
};

/** Presentational only: no invoke, no listen, no fetch (T6). */
export function SettingsBar({ settings, onChangeRoot }: SettingsBarProps) {
  return (
    <div className={styles.bar} role="region" aria-label="Settings">
      <span className="mono">{settings.meetings_root ?? "(not set)"}</span>
      <button type="button" onClick={onChangeRoot}>
        Change...
      </button>
      {settings.meetings_root && !settings.meetings_root_exists && (
        <p className={styles.warning}>
          This folder no longer exists. Choose a valid meetings folder.
        </p>
      )}
    </div>
  );
}
