import styles from "./JobRow.module.css";
import type { JobSnapshot, JobState } from "../types";

export type JobRowProps = {
  job: JobSnapshot;
  /** Calls the Rust side by job id (FR-15) — never with a raw path string. */
  onReveal: (jobId: string) => void;
};

const STATE_LABEL: Record<JobState, string> = {
  pending: "Pending",
  ingesting: "Filing into vault",
  queued: "Queued",
  running: "Transcribing",
  done: "Done",
  failed: "Failed",
  rejected: "Rejected",
};

/** Presentational only: no invoke, no listen, no fetch (T6). */
export function JobRow({ job, onReveal }: JobRowProps) {
  // The most specific path known for this job yet -- mirrors the Rust
  // side's own fallback order in `reveal_job_handler`
  // (transcript_path -> source_dest -> meeting_dir), so Reveal is offered
  // as soon as *any* of them is set rather than only once transcription
  // has also finished (E14: a job filed but still awaiting/failed
  // transcription -- the FR-13 service-down flow -- was otherwise
  // unrevealable and showed no path at all).
  const revealablePath = job.transcript_path ?? job.source_dest ?? job.meeting_dir;

  return (
    <div className={styles.row} data-state={job.state}>
      <span className={`${styles.fileName} mono`}>{job.file_name}</span>
      <span className={styles.state}>{STATE_LABEL[job.state]}</span>
      {job.classification && <span className={styles.classification}>{job.classification}</span>}
      {job.state === "ingesting" && (
        <span role="status" className={styles.busy}>
          Ingesting, please wait...
        </span>
      )}
      {job.message && <p className={styles.message}>{job.message}</p>}
      {revealablePath && (
        <>
          <span className="mono">{revealablePath}</span>
          <button type="button" onClick={() => onReveal(job.id)}>
            Reveal
          </button>
        </>
      )}
    </div>
  );
}
