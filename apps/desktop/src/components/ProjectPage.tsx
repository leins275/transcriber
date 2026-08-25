import styles from "./ProjectPage.module.css";
import { VaultList } from "./VaultList";
import type { VaultMeetingView } from "../types";

export type ProjectPageProps = {
  project: string;
  /** This project's recordings, already filtered and ordered by the caller
   * (`entriesForProject` over the vault listing). */
  entries: VaultMeetingView[];
  onBack: () => void;
  onOpen: (entryId: string) => void;
};

/**
 * One project, full-window: its recordings, and nothing else.
 *
 * The page used to carry tabs over extracted action items, facts and dated
 * status reports. That synthesis now happens outside the app, against the
 * vault folder directly, so what is left is the one thing the app is the
 * best place to do: pick a recording of this project and open it.
 *
 * The same page pattern as `RecordingPage` (no router — `App.tsx` owns which
 * page is open). Presentational apart from its callbacks: no invoke, no
 * listen, no fetch.
 */
export function ProjectPage({ project, entries, onBack, onOpen }: ProjectPageProps) {
  return (
    <section className={styles.page} aria-label="Project" role="region">
      <div className={styles.head}>
        {/* A landmark rather than a bare row of controls: the project code
            appears both here and as the page heading, and the trail is what
            tells the two apart — for a screen reader as much as for the eye. */}
        <nav className={styles.breadcrumb} aria-label="Breadcrumb">
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            ← Recordings
          </button>
          <span className={styles.crumbSeparator}>/</span>
          <span className="pill">{project}</span>
        </nav>

        <div className={styles.titleRow}>
          <h2 className={`${styles.title} mono`}>{project}</h2>
          <span className={styles.count}>
            {entries.length} recording{entries.length === 1 ? "" : "s"}
          </span>
        </div>
      </div>

      <div className={styles.body}>
        {entries.length === 0 ? (
          <p className={styles.empty}>
            No recordings under <span className="mono">{project}</span> yet. A recording named{" "}
            <span className="mono">{project} - 260812 - Weekly sync.mp4</span> files itself here.
          </p>
        ) : (
          <VaultList entries={entries} onOpen={onOpen} />
        )}
      </div>
    </section>
  );
}
