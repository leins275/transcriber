import styles from "./VaultList.module.css";
import { VaultRow } from "./VaultRow";
import type { MeetingUpdate, TranscriptView, VaultMeetingView } from "../types";

export type VaultListProps = {
  entries: VaultMeetingView[];
  /** Project codes already in the vault, passed through to each row's
   * editor so re-filing offers the projects that exist. */
  projects: string[];
  onReveal: (entryId: string) => void;
  onReadTranscript: (entryId: string) => Promise<TranscriptView>;
  onUpdate: (entryId: string, update: MeetingUpdate) => Promise<void>;
  onDelete: (entryId: string) => Promise<void>;
};

/**
 * Renders the vault listing in the order the caller already put it in
 * (newest meeting date first — see `vault::list_meetings`), keyed by id.
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultList({
  entries,
  projects,
  onReveal,
  onReadTranscript,
  onUpdate,
  onDelete,
}: VaultListProps) {
  return (
    <ul className={styles.list}>
      {entries.map((entry) => (
        <li key={entry.id}>
          <VaultRow
            entry={entry}
            projects={projects}
            onReveal={onReveal}
            onReadTranscript={onReadTranscript}
            onUpdate={onUpdate}
            onDelete={onDelete}
          />
        </li>
      ))}
    </ul>
  );
}
