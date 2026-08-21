import styles from "./DropZone.module.css";

export type DropZoneState = "idle" | "hovering" | "working";

export type DropZoneProps = {
  /** Drop target visual state (FR-5): idle, drag-hovering, or dropped/working. */
  state: DropZoneState;
  /** True before a meetings-root is configured (FR-18): refuses drops entirely. */
  disabled: boolean;
  /** Native file dialog fallback (FR-7). */
  onChooseFile: () => void;
};

const STATE_LABEL: Record<DropZoneState, string> = {
  idle: "Drop a recording here, or choose a file.",
  hovering: "Release to add this file.",
  working: "Filing dropped file(s)...",
};

/** Presentational only: no invoke, no listen, no fetch (T6). */
export function DropZone({ state, disabled, onChooseFile }: DropZoneProps) {
  if (disabled) {
    return (
      <div className={styles.prompt}>
        <p>Choose a meetings folder to start accepting recordings.</p>
      </div>
    );
  }

  return (
    <div className={styles.zone} data-state={state} role="region" aria-label="Drop zone">
      <p className={styles.hint}>{STATE_LABEL[state]}</p>
      <button type="button" onClick={onChooseFile}>
        Choose file...
      </button>
    </div>
  );
}
