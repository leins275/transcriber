import styles from "./VaultList.module.css";
import { VaultRow } from "./VaultRow";
import type { VaultMeetingView } from "../types";

export type VaultListProps = {
  entries: VaultMeetingView[];
  onReveal: (entryId: string) => void;
};

/**
 * Renders the vault listing in the order the backend already returned it
 * (newest meeting date first — see `vault::list_meetings`), keyed by id.
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultList({ entries, onReveal }: VaultListProps) {
  return (
    <ul className={styles.list}>
      {entries.map((entry) => (
        <li key={entry.id}>
          <VaultRow entry={entry} onReveal={onReveal} />
        </li>
      ))}
    </ul>
  );
}
