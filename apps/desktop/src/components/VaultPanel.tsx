import { useMemo, useState } from "react";
import styles from "./VaultPanel.module.css";
import { LedgerPanel } from "./LedgerPanel";
import { VaultList } from "./VaultList";
import {
  entriesForProject,
  projectCodes,
  resolveSelectedProject,
  unsortedEntries,
} from "../lib/vaultGroups";
import type { LedgerJobView, MeetingUpdate, TranscriptView, VaultMeetingView } from "../types";

/** The three views over what is already in the vault. `projects` leads
 * because a filed meeting is the normal case; `unsorted` is the queue of
 * things still needing a decision, which is why it carries a count. */
type Tab = "projects" | "unsorted" | "log";

export type VaultPanelProps = {
  entries: VaultMeetingView[];
  onReveal: (entryId: string) => void;
  onReadTranscript: (entryId: string) => Promise<TranscriptView>;
  onUpdate: (entryId: string, update: MeetingUpdate) => Promise<void>;
  onDelete: (entryId: string) => Promise<void>;
  onLoadServiceLog: () => Promise<LedgerJobView[]>;
};

/**
 * The vault browser: everything already ingested, split the way the vault
 * itself splits it.
 *
 * **Projects** shows one project at a time, chosen from a picker, rather
 * than every project at once — a mixed list forces the operator to filter by
 * eye on every glance. **Unsorted** is its own tab because those recordings
 * are a to-do list, not a category: they are the ones whose filename did not
 * follow the convention, and the tab exists so they can be renamed and filed
 * (which the row's own Rename action does). **Service log** is F2's durable
 * job ledger, kept here rather than in the session's Jobs panel because it
 * answers a different question — what has this service ever done, not what
 * is it doing now.
 *
 * The panel always mounts, even with an empty vault: the tabs are how the
 * operator reaches the service log, which is worth reading precisely when
 * nothing has landed in the vault.
 *
 * Presentational only: no invoke, no listen, no fetch — App.tsx owns
 * fetching and passes every action down.
 */
export function VaultPanel({
  entries,
  onReveal,
  onReadTranscript,
  onUpdate,
  onDelete,
  onLoadServiceLog,
}: VaultPanelProps) {
  const [tab, setTab] = useState<Tab>("projects");
  const [requestedProject, setRequestedProject] = useState<string | null>(null);

  const projects = useMemo(() => projectCodes(entries), [entries]);
  const unsorted = useMemo(() => unsortedEntries(entries), [entries]);
  // Derived rather than stored: re-filing the last meeting out of a project
  // makes that code disappear, and a selection held in state would leave the
  // tab pointed at a project that no longer exists.
  const selectedProject = resolveSelectedProject(projects, requestedProject);
  const projectEntries = useMemo(
    () => (selectedProject ? entriesForProject(entries, selectedProject) : []),
    [entries, selectedProject],
  );

  const rowActions = { onReveal, onReadTranscript, onUpdate, onDelete, projects };

  return (
    <section className={styles.panel} aria-label="Vault" role="region">
      <div className={styles.heading}>
        <h2 className={styles.title}>Vault</h2>
        <span className={styles.count}>{entries.length} in vault</span>
      </div>

      <div className={styles.tabs} role="tablist" aria-label="Vault views">
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
              <span className="mono">ELS</span>; anything already in Unsorted can be filed with
              Rename.
            </p>
          ) : (
            <>
              <div className={styles.picker}>
                {/* `aria-label` rather than a wrapping <label>: the count
                    beside the picker would otherwise be folded into the
                    control's accessible name. */}
                <span className={styles.pickerLabel} aria-hidden="true">
                  Project
                </span>
                <select
                  className={styles.pickerSelect}
                  aria-label="Project"
                  value={selectedProject ?? ""}
                  onChange={(event) => setRequestedProject(event.target.value)}
                >
                  {projects.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
                <span className={styles.pickerCount}>
                  {projectEntries.length} recording{projectEntries.length === 1 ? "" : "s"}
                </span>
              </div>
              <VaultList entries={projectEntries} {...rowActions} />
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
                These recordings did not follow the{" "}
                <span className="mono">Project - YYMMDD - Title</span> naming convention. Rename one
                to file it under a project.
              </p>
              <VaultList entries={unsorted} {...rowActions} />
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
