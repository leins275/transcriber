import styles from "./JobList.module.css";
import { JobRow } from "./JobRow";
import type { JobSnapshot } from "../types";

export type JobListProps = {
  jobs: JobSnapshot[];
  onReveal: (jobId: string) => void;
  /** Optional: a list rendered somewhere cancelling makes no sense simply
   * omits it, and the rows drop the action rather than showing a dead one. */
  onCancel?: (jobId: string) => void;
};

/**
 * Renders the current-session job list in submission order, keyed by id
 * (FR-8). Presentational only: no invoke, no listen, no fetch (T6).
 */
export function JobList({ jobs, onReveal, onCancel }: JobListProps) {
  return (
    <ul className={styles.list}>
      {jobs.map((job) => (
        <li key={job.id}>
          <JobRow job={job} onReveal={onReveal} onCancel={onCancel} />
        </li>
      ))}
    </ul>
  );
}
