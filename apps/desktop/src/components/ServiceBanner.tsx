import styles from "./ServiceBanner.module.css";
import type { ServiceStatusView } from "../types";

export type ServiceBannerProps = {
  status: ServiceStatusView;
};

/**
 * Announces sidecar/service reachability (FR-13). Renders nothing when the
 * service is ready — noteworthy states only, never a transient toast. A
 * degraded service reads as a note, not a failure: filing still works while
 * transcription is down. Presentational only: no invoke, no listen, no
 * fetch (T6).
 */
export function ServiceBanner({ status }: ServiceBannerProps) {
  if (status.state === "ready") {
    return null;
  }

  if (status.state === "starting") {
    return (
      <div className={`note ${styles.banner}`} data-state="starting" role="status">
        <span className="note-kicker">Note</span>
        <p>Starting the transcription service...</p>
      </div>
    );
  }

  return (
    <div className={`note ${styles.banner}`} data-state="unavailable" role="status">
      <span className="note-kicker">Note</span>
      <div>
        <p>
          Transcription service unavailable at{" "}
          <span className="mono">{status.base_url ?? "(no address configured)"}</span>. Ingest keeps
          working; recordings already filed are safe. Re-drop them once the service is back.
        </p>
        {status.detail && <p>{status.detail}</p>}
      </div>
    </div>
  );
}
