import type { ReactNode } from "react";
import styles from "./FirstRun.module.css";

export type FirstRunProps = {
  /** `null` until a vault folder is chosen (FR-18) -- step 1 stays open. */
  meetingsRoot: string | null;
  onChooseFolder: () => void;
  /** Step 2's content -- the app's `<ModelDownloadStep>` element, already
   * wired to its commands, or `null` while its status is not yet known
   * (e.g. the service is still starting). FirstRun stays presentational:
   * it never reaches into the model-download command layer itself (T6). */
  modelStep: ReactNode;
};

function CheckIcon() {
  return (
    <svg
      width="14"
      height="14"
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
}

/**
 * The first-run setup path (spec.md 2a): folder, then model download, then
 * the optional GPU runtime, presented as one numbered card instead of three
 * unrelated blocks. Replaces the drop zone until a meetings-root is
 * configured (FR-18) and stays mounted (with step 1 marked done) until the
 * model is present or skipped -- refuses drops the whole time it is shown.
 * Presentational only: no invoke, no listen, no fetch (T6).
 */
export function FirstRun({ meetingsRoot, onChooseFolder, modelStep }: FirstRunProps) {
  const folderChosen = Boolean(meetingsRoot);

  return (
    <div className={styles.card}>
      <h1 className={styles.title}>Set up Transcriber</h1>
      <p className={styles.subtitle}>
        Three steps, once. You can drop recordings as soon as a folder is chosen — transcription
        starts when the model is here.
      </p>

      <div className={styles.steps}>
        <div className={styles.step}>
          <div className={styles.numeral} data-done={folderChosen}>
            1
          </div>
          <div>
            <div className={styles.stepHeading}>
              Choose where meetings live
              {folderChosen && <CheckIcon />}
            </div>
            {folderChosen ? (
              <div className={styles.folderRow}>
                <span className="mono">{meetingsRoot}</span>
                <button type="button" className="btn btn-ghost" onClick={onChooseFolder}>
                  Change…
                </button>
              </div>
            ) : (
              <div className={styles.folderRow}>
                <button type="button" className="btn btn-secondary" onClick={onChooseFolder}>
                  Choose folder…
                </button>
              </div>
            )}
          </div>
        </div>

        <div className={styles.step}>
          <div className={styles.numeral} data-active={folderChosen}>
            2
          </div>
          <div>
            <div className={styles.stepHeading}>Download the transcription model</div>
            <div className={styles.stepSub}>large-v3 · 3.0 GB · one time, kept on disk</div>
            <div className={styles.stepBody}>
              {modelStep ?? (
                <p className={styles.waiting}>Waiting for the transcription service…</p>
              )}
            </div>
          </div>
        </div>

        <div className={styles.step}>
          <div className={styles.numeral}>3</div>
          <div>
            <div className={styles.stepHeading}>
              GPU acceleration <em>— optional</em>
            </div>
            <div className={styles.stepSub}>
              CUDA runtime · 1.4 GB. Without it transcription runs on the CPU — slower, but it
              works.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
