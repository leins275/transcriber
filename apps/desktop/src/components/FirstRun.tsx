import styles from "./FirstRun.module.css";

export type FirstRunProps = {
  onChooseFolder: () => void;
};

/**
 * Replaces the drop zone until a meetings-root is configured (FR-18).
 * Refuses drops entirely and exposes only the folder picker.
 * Presentational only: no invoke, no listen, no fetch (T6).
 */
export function FirstRun({ onChooseFolder }: FirstRunProps) {
  return (
    <div className={styles.prompt}>
      <p>Choose a meetings folder to begin.</p>
      <button type="button" onClick={onChooseFolder}>
        Choose folder...
      </button>
    </div>
  );
}
