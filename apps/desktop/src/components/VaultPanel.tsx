import { useMemo, useState } from "react";
import styles from "./VaultPanel.module.css";
import { JobList } from "./JobList";
import { LedgerPanel } from "./LedgerPanel";
import { VaultList } from "./VaultList";
import { entriesForProject, projectCodes, unsortedEntries } from "../lib/vaultGroups";
import type { JobSnapshot, LedgerJobView, VaultMeetingView } from "../types";

/** The two views: the recordings themselves, and F2's durable job ledger. */
type Tab = "recordings" | "log";

/** The picker value meaning "only recordings filed under `unsorted/`".
 * Lowercase, so it can never collide with a real project code — the vault
 * capitalizes those. */
const UNSORTED_FILTER = "unsorted";

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
 * One list, grouping optional (redesign turn 8): recordings render flat,
 * newest first, each row carrying its project as a small tag. One filter
 * row narrows to a project (or to Unsorted) and can switch to grouped-by-
 * project headers — a view preference over the same list, not a different
 * page. There are no project pages.
 *
 * Presentational only: no invoke, no listen, no fetch — App.tsx owns
 * fetching and passes every action down.
 */
export function VaultPanel({
  entries,
  jobs,
  onOpen,
  onRevealJob,
  onCancelJob,
  onLoadServiceLog,
}: VaultPanelProps) {
  const [tab, setTab] = useState<Tab>("recordings");
  const [filter, setFilter] = useState<string>("");
  const [grouped, setGrouped] = useState(false);

  const projects = useMemo(() => projectCodes(entries), [entries]);
  const unsorted = useMemo(() => unsortedEntries(entries), [entries]);
  const pinned = useMemo(
    () => jobs.filter((job) => isInFlight(job) || needsAttention(job)),
    [jobs],
  );
  const inFlight = useMemo(() => jobs.filter(isInFlight).length, [jobs]);

  // A selection can outlive its target (the last meeting re-filed out of a
  // project, or out of unsorted). Fall back to "everything" rather than
  // rendering an empty list with no explanation.
  const validFilter =
    filter === UNSORTED_FILTER
      ? unsorted.length > 0
        ? filter
        : ""
      : projects.includes(filter)
        ? filter
        : "";

  const shown = useMemo(() => {
    if (validFilter === "") return entries;
    if (validFilter === UNSORTED_FILTER) return unsorted;
    return entriesForProject(entries, validFilter);
  }, [entries, unsorted, validFilter]);

  // The filter row earns its place only when there is something to choose
  // or to group -- a vault of one project with nothing unsorted needs
  // neither control (grouping it would draw a single header over the same
  // list).
  const showFilterRow = projects.length + (unsorted.length > 0 ? 1 : 0) > 1;

  // Grouped view: which project groups render, and whether Unsorted tails.
  const shownProjects =
    validFilter === "" ? projects : validFilter === UNSORTED_FILTER ? [] : [validFilter];
  const showUnsortedGroup =
    (validFilter === "" || validFilter === UNSORTED_FILTER) && unsorted.length > 0;

  return (
    <section className={styles.panel} aria-label="Recordings" role="region">
      <div className={styles.heading}>
        <h2 className={styles.title}>Recordings</h2>
        <div className={styles.tabs} role="tablist" aria-label="Recording views">
          <button
            type="button"
            role="tab"
            id="vault-tab-recordings"
            aria-selected={tab === "recordings"}
            aria-controls="vault-panel-recordings"
            className={styles.tab}
            onClick={() => setTab("recordings")}
          >
            Recordings
            <span className={styles.tabCount}>{entries.length}</span>
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

      {tab === "recordings" && (
        <div
          role="tabpanel"
          id="vault-panel-recordings"
          aria-labelledby="vault-tab-recordings"
          className={styles.body}
        >
          {entries.length === 0 ? (
            <p className={styles.empty}>
              No recordings yet. A recording named{" "}
              <span className="mono">ELS - 260812 - Weekly sync.mp4</span> files itself under
              project <span className="mono">ELS</span>; anything else lands in Unsorted.
            </p>
          ) : (
            <>
              {showFilterRow && (
                <div className={styles.filterRow}>
                  <select
                    className={styles.pickerSelect}
                    aria-label="Project"
                    value={validFilter}
                    onChange={(event) => setFilter(event.target.value)}
                  >
                    <option value="">All projects</option>
                    {projects.map((code) => (
                      <option key={code} value={code}>
                        {code}
                      </option>
                    ))}
                    {unsorted.length > 0 && <option value={UNSORTED_FILTER}>Unsorted</option>}
                  </select>
                  <label className={styles.groupToggle}>
                    <input
                      type="checkbox"
                      className={styles.groupSwitch}
                      checked={grouped}
                      onChange={(event) => setGrouped(event.target.checked)}
                    />
                    Group by project
                  </label>
                </div>
              )}
              {validFilter === UNSORTED_FILTER && (
                <p className={styles.hint}>
                  These did not follow the <span className="mono">Project - YYMMDD - Title</span>{" "}
                  naming convention. Open one and rename it to file it under a project.
                </p>
              )}
              {!grouped ? (
                <VaultList entries={shown} onOpen={onOpen} />
              ) : (
                <>
                  {shownProjects.map((code) => {
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
                        </div>
                        <VaultList entries={group} onOpen={onOpen} showProject={false} />
                      </div>
                    );
                  })}
                  {showUnsortedGroup && (
                    <div className={styles.group}>
                      <div className={styles.groupHead}>
                        <h3 className={`${styles.groupName} mono`}>Unsorted</h3>
                        <span className={styles.groupCount}>
                          {unsorted.length} recording{unsorted.length === 1 ? "" : "s"}
                        </span>
                      </div>
                      <VaultList entries={unsorted} onOpen={onOpen} showProject={false} />
                    </div>
                  )}
                </>
              )}
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
