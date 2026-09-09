import styles from "./JobRow.module.css";
import { parseFileName } from "../lib/fileName";
import type { JobSnapshot, JobState, JobType } from "../types";

export type JobRowProps = {
  job: JobSnapshot;
  /** Calls the Rust side by job id (FR-15) — never with a raw path string. */
  onReveal: (jobId: string) => void;
  /** Optional: offered only while the job is still running, and only when
   * the caller can actually cancel. */
  onCancel?: (jobId: string) => void;
};

const STATE_TEXT: Record<JobState, string> = {
  pending: "Pending",
  ingesting: "Filing into vault",
  queued: "Queued",
  running: "Transcribing",
  done: "Done",
  // Failed transcription never means the recording was lost -- it was
  // filed before transcription ran, and stays filed (spec.md section 5.6).
  failed: "Transcription failed — the recording is safely filed.",
  rejected: "Rejected",
};

/** What a running job of each type is doing, for the meta line. */
const RUNNING_TEXT: Record<JobType, string> = {
  transcribe: "Transcribing",
  summarize: "Summarizing",
  export: "Exporting PDF",
  diarize: "Identifying speakers",
};

/** A failed derived job loses no source material — unlike transcription's
 * carefully-worded failure line, "failed" plus the service's own message
 * (rendered below the meta line) says everything. */
const FAILED_TEXT: Record<JobType, string> = {
  transcribe: STATE_TEXT.failed,
  summarize: "Summary failed.",
  export: "Export failed.",
  diarize: "Speaker identification failed.",
};

/** The project name from the `Project - YYMMDD - Title` convention, for a
 * job the Rust side already classified `"sorted"` -- a display-only
 * re-derivation of the existing `file_name` field, not new data. */
function projectFor(job: JobSnapshot): string | null {
  if (job.classification !== "sorted") return null;
  return parseFileName(job.file_name)?.project ?? null;
}

/** The bar's fraction as whole percent, clamped: the service is the source
 * of truth, but a fraction that overshoots must never paint past the end of
 * the track (nor announce more than 100). `null` means this phase has no
 * linear signal at all -- an honest absence, not a zero. */
function percentFor(job: JobSnapshot): number | null {
  if (job.progress == null) return null;
  return Math.round(Math.max(0, Math.min(1, job.progress)) * 100);
}

function metaLine(job: JobSnapshot, project: string | null): string {
  const jobType: JobType = job.job_type ?? "transcribe";
  switch (job.state) {
    case "queued":
      return project ? `Queued · next in line · ${project}` : "Queued · next in line";
    case "running": {
      const parts = [RUNNING_TEXT[jobType] ?? "Working"];
      // <verb> · <phase> · <NN%> · <project>, each part only when it exists.
      if (job.phase) parts.push(job.phase);
      const percent = percentFor(job);
      if (percent != null) parts.push(`${percent}%`);
      if (project) parts.push(project);
      return parts.join(" · ");
    }
    case "done":
      return project ? `Done · ${project}` : "Done";
    case "failed":
      return FAILED_TEXT[jobType] ?? STATE_TEXT.failed;
    default:
      return STATE_TEXT[job.state];
  }
}

function StateIcon({ state }: { state: JobState }) {
  switch (state) {
    case "done":
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
    case "running":
      return (
        <svg
          className={styles.spin}
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2.2"
          strokeLinecap="round"
        >
          <circle cx="12" cy="12" r="9" strokeDasharray="34 22"></circle>
        </svg>
      );
    case "failed":
      return (
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--danger)"
          strokeWidth="2.5"
          strokeLinecap="round"
        >
          <line x1="6" y1="6" x2="18" y2="18"></line>
          <line x1="18" y1="6" x2="6" y2="18"></line>
        </svg>
      );
    case "rejected":
      return (
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--text-faint)"
          strokeWidth="2.5"
          strokeLinecap="round"
        >
          <line x1="6" y1="6" x2="18" y2="18"></line>
          <line x1="18" y1="6" x2="6" y2="18"></line>
        </svg>
      );
    default:
      return <span className={styles.ring} />;
  }
}

/** Presentational only: no invoke, no listen, no fetch (T6). */
export function JobRow({ job, onReveal, onCancel }: JobRowProps) {
  // The most specific path known for this job yet -- mirrors the Rust
  // side's own fallback order in `reveal_job_handler`
  // (transcript_path -> source_dest -> meeting_dir), so Reveal is offered
  // as soon as *any* of them is set rather than only once transcription
  // has also finished (E14: a job filed but still awaiting/failed
  // transcription -- the FR-13 service-down flow -- was otherwise
  // unrevealable and showed no path at all).
  const revealablePath = job.transcript_path ?? job.source_dest ?? job.meeting_dir;
  const project = projectFor(job);
  const percent = percentFor(job);

  return (
    <div className={styles.row} data-state={job.state}>
      <span className={styles.icon} aria-hidden="true">
        <StateIcon state={job.state} />
      </span>
      <div className={styles.content}>
        <span className={`${styles.fileName} mono`}>{job.file_name}</span>
        <span
          className={job.state === "failed" ? `${styles.meta} ${styles.dangerMeta}` : styles.meta}
        >
          {metaLine(job, project)}
          {job.classification === "unsorted" && <span className="pill">filed · unsorted</span>}
        </span>
        {job.state === "ingesting" && (
          <span role="status" className={styles.busy}>
            Ingesting, please wait...
          </span>
        )}
        {job.state === "running" &&
          (percent != null ? (
            <div
              className={styles.progressTrack}
              role="progressbar"
              aria-label="Job progress"
              aria-valuenow={percent}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <div className={styles.progressFill} style={{ width: `${percent}%` }} />
            </div>
          ) : (
            // The phase reports no linear signal, so the bar announces no
            // value: a sliver slides across the track to say "still working"
            // rather than an empty 0% bar claiming no progress was made.
            <div
              className={styles.progressTrack}
              role="progressbar"
              aria-label="Job progress"
              data-indeterminate="true"
            >
              <div className={`${styles.progressFill} ${styles.progressSliver}`} />
            </div>
          ))}
        {job.message && <p className={styles.message}>{job.message}</p>}
        {revealablePath && <span className={`${styles.path} mono`}>{revealablePath}</span>}
      </div>
      <div className={styles.actions}>
        {revealablePath && (
          <button type="button" className="btn btn-secondary" onClick={() => onReveal(job.id)}>
            Reveal
          </button>
        )}
        {/* Only while there is something to stop. A queued job has been
            handed to the service and can still be withdrawn; a finished one
            cannot, and offering the button anyway would be a lie. */}
        {onCancel && (job.state === "queued" || job.state === "running") && (
          <button type="button" className="btn btn-ghost" onClick={() => onCancel(job.id)}>
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
