import styles from "./VaultList.module.css";
import { VaultRow } from "./VaultRow";
import type { VaultMeetingView } from "../types";

export type VaultListProps = {
  entries: VaultMeetingView[];
  onOpen: (entryId: string) => void;
  /** Forwarded to every row: whether it names its project in the meta line.
   * Defaults on (the flat list); a grouped caller turns it off. */
  showProject?: boolean;
};

/**
 * Renders the recordings in the order the caller already put them in
 * (newest meeting date first — see `vault::list_meetings`), keyed by id.
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultList({ entries, onOpen, showProject = true }: VaultListProps) {
  return (
    <ul className={styles.list}>
      {entries.map((entry) => (
        <li key={entry.id}>
          <VaultRow entry={entry} onOpen={onOpen} showProject={showProject} />
        </li>
      ))}
    </ul>
  );
}
