import styles from "./UpdateNotice.module.css";
import { isVisible, updateMessage, type UpdateState } from "../lib/update";

export type UpdateNoticeProps = {
  state: UpdateState;
  onInstall: () => void;
  onRestart: () => void;
  onDismiss: () => void;
};

/**
 * The launch-time update notice.
 *
 * A quiet strip, never a modal. An update is not urgent enough to stand
 * between someone and the recording they opened the app to deal with, and a
 * dialog that must be dismissed before the app is usable makes the update
 * feel like an interruption rather than an offer.
 *
 * Silent while checking and when already current: an app that announces it
 * is looking for updates, or that there are none, is interrupting to report
 * that nothing happened.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function UpdateNotice({ state, onInstall, onRestart, onDismiss }: UpdateNoticeProps) {
  if (!isVisible(state)) return null;

  const message = updateMessage(state);
  const isError = state.status === "error";

  return (
    <div
      className={`note ${styles.notice}`}
      data-state={state.status}
      // An available update is an offer, not a problem: `status` rather than
      // `alert`, which would interrupt a screen reader mid-sentence. A
      // failed check is the one case worth interrupting for, since it means
      // the app cannot tell whether it is current.
      role={isError ? "alert" : "status"}
    >
      <span className="note-kicker">{isError ? "Update" : "Update available"}</span>
      <div className={styles.body}>
        <p className={styles.message}>{message}</p>
        {state.status === "available" && state.update.notes && (
          <p className={styles.notes}>{state.update.notes}</p>
        )}
        {state.status === "downloading" && state.percent !== null && (
          <div className={styles.track}>
            <div className={styles.fill} style={{ width: `${state.percent}%` }} />
          </div>
        )}
      </div>
      <div className={styles.actions}>
        {state.status === "available" && (
          <button type="button" className="btn" onClick={onInstall}>
            Install
          </button>
        )}
        {state.status === "installed" && (
          <button type="button" className="btn" onClick={onRestart}>
            Restart now
          </button>
        )}
        {state.status !== "downloading" && (
          <button type="button" className="btn btn-ghost" onClick={onDismiss}>
            {state.status === "installed" ? "Later" : "Dismiss"}
          </button>
        )}
      </div>
    </div>
  );
}
