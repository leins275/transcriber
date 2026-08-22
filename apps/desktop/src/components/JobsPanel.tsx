import styles from "./JobsPanel.module.css";
import { JobList } from "./JobList";
import type { JobSnapshot } from "../types";

export type JobsPanelProps = {
  jobs: JobSnapshot[];
  onReveal: (jobId: string) => void;
};

/**
 * The session's job ledger: a heading with the running count, and the
 * ruled row list underneath. Only mounted once at least one job exists for
 * the session -- with none yet, the drop zone's hero variant takes the
 * whole main pane instead (spec.md's teaching empty state). Presentational
 * only: no invoke, no listen, no fetch (T6).
 */
export function JobsPanel({ jobs, onReveal }: JobsPanelProps) {
  return (
    <section className={styles.panel} aria-label="Jobs" role="region">
      <div className={styles.heading}>
        <h2 className={styles.title}>Jobs</h2>
        <span className={styles.count}>{jobs.length} this session</span>
      </div>
      <div className={styles.list}>
        <JobList jobs={jobs} onReveal={onReveal} />
      </div>
    </section>
  );
}
