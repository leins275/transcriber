import styles from "./VaultRow.module.css";
import type { VaultMeetingView } from "../types";

export type VaultRowProps = {
  entry: VaultMeetingView;
  /** Calls the Rust side by the entry's server-issued id (FR: never a raw
   * path from the UI) -- mirrors `JobRow`'s own `onReveal(job.id)` contract. */
  onReveal: (entryId: string) => void;
};

/** A filled check for a meeting that already has a transcript, a hollow
 * ring (matching `JobRow`'s own pending indicator) when it does not. */
function TranscriptIcon({ present }: { present: boolean }) {
  if (present) {
    return (
      <svg
        width="15"
        height="15"
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
  return <span className={styles.ring} />;
}

/**
 * One row of the vault browser: the meeting's own name, its transcript
 * status, a project pill (or "unsorted"), the full path in monospace, and a
 * Reveal button. Presentational only: no invoke, no listen, no fetch.
 */
export function VaultRow({ entry, onReveal }: VaultRowProps) {
  return (
    <div className={styles.row}>
      <span className={styles.icon} aria-hidden="true">
        <TranscriptIcon present={entry.has_transcript} />
      </span>
      <div className={styles.content}>
        <span className={`${styles.name} mono`}>{entry.meeting_name}</span>
        <span className={styles.meta}>
          {entry.has_transcript ? "Transcript ready" : "No transcript yet"}
          <span className="pill">{entry.project ?? "unsorted"}</span>
        </span>
        <span className={`${styles.path} mono`}>{entry.meeting_dir}</span>
      </div>
      <div className={styles.actions}>
        <button type="button" className="btn btn-secondary" onClick={() => onReveal(entry.id)}>
          Reveal
        </button>
      </div>
    </div>
  );
}
