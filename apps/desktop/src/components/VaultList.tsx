import { useMemo } from "react";
import styles from "./VaultList.module.css";
import { VaultRow } from "./VaultRow";
import {
  nextVaultSort,
  sortVaultTable,
  type VaultSort,
  type VaultSortColumn,
} from "../lib/vaultSort";
import type { VaultMeetingView } from "../types";

export type VaultListProps = {
  entries: VaultMeetingView[];
  onOpen: (entryId: string) => void;
  /** The column sort, owned by the caller so it outlives this table. */
  sort: VaultSort;
  onSortChange: (sort: VaultSort) => void;
};

/** The columns, in the naming convention's own order —
 * `<Project> - <Date> - <Name> - <Type>` — then the transcript state. */
const COLUMNS: { column: VaultSortColumn; label: string }[] = [
  { column: "project", label: "Project" },
  { column: "date", label: "Date" },
  { column: "name", label: "Name" },
  { column: "type", label: "Type" },
  { column: "transcript", label: "Transcript" },
];

/**
 * The recordings as a sortable table. The caller hands them over newest
 * meeting date first (see `vault::list_meetings`); a click on a column
 * header re-orders them through `lib/vaultSort`.
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultList({ entries, onOpen, sort, onSortChange }: VaultListProps) {
  const sorted = useMemo(() => sortVaultTable(entries, sort), [entries, sort]);

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            {COLUMNS.map(({ column, label }) => {
              const active = sort.column === column;
              const ascending = sort.direction === "asc";
              return (
                <th
                  key={column}
                  scope="col"
                  aria-sort={active ? (ascending ? "ascending" : "descending") : "none"}
                >
                  <button
                    type="button"
                    className={styles.sortButton}
                    data-active={active || undefined}
                    onClick={() => onSortChange(nextVaultSort(sort, column))}
                  >
                    {label}
                    <span className={styles.sortMark} aria-hidden="true">
                      {active ? (ascending ? "▲" : "▼") : "⇅"}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((entry) => (
            <VaultRow key={entry.id} entry={entry} onOpen={onOpen} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
