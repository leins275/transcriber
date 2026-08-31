import styles from "./ProjectPage.module.css";
import { ProjectChat, type ProjectChatProps } from "./ProjectChat";
import { VaultList } from "./VaultList";
import type { VaultMeetingView } from "../types";

export type ProjectPageProps = {
  project: string;
  /** This project's recordings, already filtered and ordered by the caller
   * (`entriesForProject` over the vault listing). */
  entries: VaultMeetingView[];
  onBack: () => void;
  onOpen: (entryId: string) => void;
  /** The chat slice (`useChat` in App); a cited source opens through the
   * same `onOpen` as a list row. */
  chat: Omit<ProjectChatProps, "project" | "onOpenSource">;
};

/**
 * One project, full-window: its recordings, and a chat with the local
 * language model over its materials.
 *
 * The page once carried tabs over extracted action items, facts and dated
 * status reports; those jobs were retired. What returned in their place is
 * the thing only the app can do: ask questions across everything this
 * project's meetings produced, with cited, clickable sources.
 *
 * The same page pattern as `RecordingPage` (no router — `App.tsx` owns which
 * page is open). Presentational apart from its callbacks: no invoke, no
 * listen, no fetch.
 */
export function ProjectPage({ project, entries, onBack, onOpen, chat }: ProjectPageProps) {
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

        <ProjectChat project={project} onOpenSource={onOpen} {...chat} />
      </div>
    </section>
  );
}
