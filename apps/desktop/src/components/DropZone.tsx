import styles from "./DropZone.module.css";

export type DropZoneState = "idle" | "hovering" | "working";

export type DropZoneProps = {
  /** Drop target visual state (FR-5): idle, drag-hovering, or dropped/working. */
  state: DropZoneState;
  /** True before a meetings-root is configured (FR-18): refuses drops entirely. */
  disabled: boolean;
  /** Native file dialog fallback (FR-7). */
  onChooseFile: () => void;
  /** `"strip"` (default): the slim dashed bar shown above a non-empty job
   * list. `"hero"`: the large, centred drop target shown when there is
   * nothing in the session yet -- also teaches the naming convention, since
   * spec.md calls the bare empty state out as a design gap. */
  variant?: "strip" | "hero";
};

const STRIP_LABEL: Record<DropZoneState, string> = {
  idle: "Drop recordings anywhere in this window.",
  hovering: "Release to add this file.",
  working: "Filing dropped file(s)…",
};

const HERO_HEADLINE: Record<DropZoneState, string> = {
  idle: "Drop a recording anywhere in this window",
  hovering: "Release to add this file",
  working: "Filing dropped file(s)…",
};

/** Presentational only: no invoke, no listen, no fetch (T6). */
export function DropZone({ state, disabled, onChooseFile, variant = "strip" }: DropZoneProps) {
  if (disabled) {
    return (
      <div className={styles.prompt}>
        <p>Choose a meetings folder to start accepting recordings.</p>
      </div>
    );
  }

  if (variant === "hero") {
    return (
      <div className={styles.hero} data-state={state} role="region" aria-label="Drop zone">
        <p className={styles.heroHeadline}>{HERO_HEADLINE[state]}</p>
        <button type="button" className="btn btn-secondary" onClick={onChooseFile}>
          Choose file…
        </button>
        {state === "idle" && (
          <>
            <div className={styles.heroDivider} />
            <p className={styles.heroTeachIntro}>
              Name a file <span className="mono">Project - YYMMDD - Title.mp4</span> and it files
              itself:
            </p>
            <p className={`${styles.heroExample} mono`}>
              ELS - 260812 - Security issue.mp4
              <br />→ D:\MeetingVault\ELS\260812 - Security issue\
              <span className={styles.heroAccent}>transcript.json</span>
            </p>
            <p className={styles.heroNote}>
              Any other name is filed too — into unsorted\. Nothing is ever refused for its name.
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className={styles.zone} data-state={state} role="region" aria-label="Drop zone">
      <p className={styles.hint}>{STRIP_LABEL[state]}</p>
      <button type="button" className="btn btn-secondary" onClick={onChooseFile}>
        Choose file…
      </button>
    </div>
  );
}
