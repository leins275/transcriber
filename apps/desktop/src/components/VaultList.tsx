import styles from "./VaultList.module.css";
import { VaultRow } from "./VaultRow";
import type { VaultMeetingView } from "../types";

export type VaultListProps = {
  entries: VaultMeetingView[];
  onOpen: (entryId: string) => void;
  onReveal: (entryId: string) => void;
};

/**
 * Renders the recordings in the order the caller already put them in
 * (newest meeting date first — see `vault::list_meetings`), keyed by id.
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultList({ entries, onOpen, onReveal }: VaultListProps) {
  return (
    <ul className={styles.list}>
      {entries.map((entry) => (
        <li key={entry.id}>
          <VaultRow entry={entry} onOpen={onOpen} onReveal={onReveal} />
        </li>
      ))}
    </ul>
  );
}
