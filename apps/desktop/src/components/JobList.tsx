import styles from "./JobList.module.css";
import { JobRow } from "./JobRow";
import type { JobSnapshot } from "../types";

export type JobListProps = {
  jobs: JobSnapshot[];
  onReveal: (jobId: string) => void;
};

/**
 * Renders the current-session job list in submission order, keyed by id
 * (FR-8). Presentational only: no invoke, no listen, no fetch (T6).
 */
export function JobList({ jobs, onReveal }: JobListProps) {
  return (
    <ul className={styles.list}>
      {jobs.map((job) => (
        <li key={job.id}>
          <JobRow job={job} onReveal={onReveal} />
        </li>
      ))}
    </ul>
  );
}
