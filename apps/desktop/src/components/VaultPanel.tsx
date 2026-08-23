import { useMemo, useState } from "react";
import styles from "./VaultPanel.module.css";
import { JobList } from "./JobList";
import { LedgerPanel } from "./LedgerPanel";
import { VaultList } from "./VaultList";
import { entriesForProject, projectCodes, unsortedEntries } from "../lib/vaultGroups";
import type { JobSnapshot, LedgerJobView, VaultMeetingView } from "../types";

/** The three views over what is in the vault. `projects` leads because a
 * filed recording is the normal case; `unsorted` is the queue of things
 * still needing a decision, which is why it carries a count. */
type Tab = "projects" | "unsorted" | "log";

/** A job still moving through the pipeline. A terminal one has already
 * become (or updated) a real recording in the list below, so leaving it
 * pinned would show the same meeting twice. */
function isInFlight(job: JobSnapshot): boolean {
  return (
    job.state === "pending" ||
    job.state === "ingesting" ||
    job.state === "queued" ||
    job.state === "running"
  );
}

/** A job that ended badly still has something to say -- a rejected drop
 * never became a recording at all, and a failed transcription left a filed
 * recording the list cannot explain on its own. */
function needsAttention(job: JobSnapshot): boolean {
  return job.state === "rejected" || job.state === "failed";
}

export type VaultPanelProps = {
  entries: VaultMeetingView[];
  /** This session's jobs. Rendered *in* the list rather than in a panel of
   * their own -- see the component docs. */
  jobs: JobSnapshot[];
  onOpen: (entryId: string) => void;
  /** Opens the project page (action items / facts / reports). */
  onOpenProject: (project: string) => void;
  onReveal: (entryId: string) => void;
  onRevealJob: (jobId: string) => void;
  onCancelJob: (jobId: string) => void;
  onLoadServiceLog: () => Promise<LedgerJobView[]>;
};

/**
 * The library: everything in the vault, and everything on its way in.
 *
 * The single most important decision here is that **jobs are recordings**.
 * A separate "Jobs" panel above a "Vault" panel showed the same meeting
 * twice — once as work in progress, once as a filed result — and made the
 * operator hold the correspondence in their head. Live work now renders in
 * place at the top of the same list, and drops out of the pinned section as
 * soon as it is a recording like any other.
 *
 * Within **Projects**, recordings are grouped under their project rather
 * than mixed, so scanning does not mean filtering by eye. The picker filters
 * to one project when there are enough to want that.
 *
 * Presentational only: no invoke, no listen, no fetch — App.tsx owns
 * fetching and passes every action down.
 */
export function VaultPanel({
  entries,
  jobs,
  onOpen,
  onOpenProject,
  onReveal,
  onRevealJob,
  onCancelJob,
  onLoadServiceLog,
}: VaultPanelProps) {
  const [tab, setTab] = useState<Tab>("projects");
  const [project, setProject] = useState<string>("");

  const projects = useMemo(() => projectCodes(entries), [entries]);
  const unsorted = useMemo(() => unsortedEntries(entries), [entries]);
  const pinned = useMemo(
    () => jobs.filter((job) => isInFlight(job) || needsAttention(job)),
    [jobs],
  );
  const inFlight = useMemo(() => jobs.filter(isInFlight).length, [jobs]);

  // "" means every project: the default, matching the design's grouped
  // list. A selection narrows it without changing the grouping, so the
  // headers stay meaningful either way.
  const shown = useMemo(
    () => (project === "" ? projects : projects.filter((code) => code === project)),
    [projects, project],
  );

  return (
    <section className={styles.panel} aria-label="Recordings" role="region">
      <div className={styles.heading}>
        <h2 className={styles.title}>Recordings</h2>
        <div className={styles.tabs} role="tablist" aria-label="Recording views">
          <button
            type="button"
            role="tab"
            id="vault-tab-projects"
            aria-selected={tab === "projects"}
            aria-controls="vault-panel-projects"
            className={styles.tab}
            onClick={() => setTab("projects")}
          >
            Projects
            <span className={styles.tabCount}>{projects.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            id="vault-tab-unsorted"
            aria-selected={tab === "unsorted"}
            aria-controls="vault-panel-unsorted"
            className={styles.tab}
            onClick={() => setTab("unsorted")}
          >
            Unsorted
            <span className={styles.tabCount}>{unsorted.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            id="vault-tab-log"
            aria-selected={tab === "log"}
            aria-controls="vault-panel-log"
            className={styles.tab}
            onClick={() => setTab("log")}
          >
            Service log
          </button>
        </div>
        <span className={styles.count}>
          {entries.length} recording{entries.length === 1 ? "" : "s"}
          {inFlight > 0 && ` · ${inFlight} in flight`}
        </span>
      </div>

      {/* Pinned above whichever tab is open: work in progress is not filed
          anywhere yet, so hiding it behind a tab would hide it from the
          person who just started it. */}
      {tab !== "log" && pinned.length > 0 && (
        <div className={styles.pinned}>
          <JobList jobs={pinned} onReveal={onRevealJob} onCancel={onCancelJob} />
        </div>
      )}

      {tab === "projects" && (
        <div
          role="tabpanel"
          id="vault-panel-projects"
          aria-labelledby="vault-tab-projects"
          className={styles.body}
        >
          {projects.length === 0 ? (
            <p className={styles.empty}>
              No projects yet. A recording named{" "}
              <span className="mono">ELS - 260812 - Weekly sync.mp4</span> files itself under{" "}
              <span className="mono">ELS</span>; anything in Unsorted can be filed by renaming it.
            </p>
          ) : (
            <>
              {projects.length > 1 && (
                <div className={styles.picker}>
                  <span className={styles.pickerLabel} aria-hidden="true">
                    Project
                  </span>
                  <select
                    className={styles.pickerSelect}
                    aria-label="Project"
                    value={project}
                    onChange={(event) => setProject(event.target.value)}
                  >
                    <option value="">All projects</option>
                    {projects.map((code) => (
                      <option key={code} value={code}>
                        {code}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {shown.map((code) => {
                const group = entriesForProject(entries, code);
                return (
                  <div key={code} className={styles.group}>
                    <div className={styles.groupHead}>
                      <span className={styles.groupKicker}>Project</span>
                      {/* A real heading, not a styled span: this is the
                          structure of the list, and it is how the group is
                          reached by anything navigating by headings. */}
                      <h3 className={`${styles.groupName} mono`}>{code}</h3>
                      <span className={styles.groupCount}>
                        {group.length} recording{group.length === 1 ? "" : "s"}
                      </span>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => onOpenProject(code)}
                      >
                        Open project →
                      </button>
                    </div>
                    <VaultList entries={group} onOpen={onOpen} onReveal={onReveal} />
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}

      {tab === "unsorted" && (
        <div
          role="tabpanel"
          id="vault-panel-unsorted"
          aria-labelledby="vault-tab-unsorted"
          className={styles.body}
        >
          {unsorted.length === 0 ? (
            <p className={styles.empty}>
              Nothing unsorted — every recording in the vault is filed under a project.
            </p>
          ) : (
            <>
              <p className={styles.hint}>
                These did not follow the <span className="mono">Project - YYMMDD - Title</span>{" "}
                naming convention. Open one and rename it to file it under a project.
              </p>
              <VaultList entries={unsorted} onOpen={onOpen} onReveal={onReveal} />
            </>
          )}
        </div>
      )}

      {tab === "log" && (
        <div
          role="tabpanel"
          id="vault-panel-log"
          aria-labelledby="vault-tab-log"
          className={styles.body}
        >
          <LedgerPanel onLoad={onLoadServiceLog} />
        </div>
      )}
    </section>
  );
}
