/**
 * Pure column sorting for the library's recordings table.
 *
 * The table's columns follow the naming convention's own order — project,
 * date, name, type — plus the transcript state. The vault listing already
 * arrives newest first (`vault::list_meetings`), so that is the table's
 * resting order and the default sort returns the entries untouched; every
 * other sort is a stable re-ordering of that same list, which keeps
 * "newest first" as the tie-break inside any column.
 */
import { parseEntryName } from "./meetingName";
import type { VaultMeetingView } from "../types";

export type VaultSortColumn = "project" | "date" | "name" | "type" | "transcript";

export type VaultSort = {
  column: VaultSortColumn;
  direction: "asc" | "desc";
};

/** Newest first: the order the vault listing itself comes in. */
export const DEFAULT_VAULT_SORT: VaultSort = { column: "date", direction: "desc" };

export function isDefaultVaultSort(sort: VaultSort): boolean {
  return (
    sort.column === DEFAULT_VAULT_SORT.column && sort.direction === DEFAULT_VAULT_SORT.direction
  );
}

/**
 * What a click on a column header does to the sort.
 *
 * A column cycles ascending → descending → back to the resting order. The
 * date column *is* the resting order, so it has no third state and simply
 * flips between newest first and oldest first.
 */
export function nextVaultSort(current: VaultSort, column: VaultSortColumn): VaultSort {
  if (column === "date") {
    const newestFirst = current.column === "date" && current.direction === "desc";
    return { column: "date", direction: newestFirst ? "asc" : "desc" };
  }
  if (current.column !== column) return { column, direction: "asc" };
  if (current.direction === "asc") return { column, direction: "desc" };
  return DEFAULT_VAULT_SORT;
}

/** The value a row sorts by in one column, or `null` when it has none (an
 * unsorted recording has no project, an untyped meeting no type, a
 * hand-named folder no date). */
function sortKey(entry: VaultMeetingView, column: VaultSortColumn): string | null {
  const parsed = parseEntryName(entry.meeting_name, entry.project);
  switch (column) {
    case "project":
      return entry.project;
    case "date":
      return parsed ? parsed.date : null;
    case "name":
      return parsed ? parsed.title : entry.meeting_name;
    case "type":
      return parsed ? parsed.kind : null;
    case "transcript":
      return entry.has_transcript ? "0" : entry.has_source ? "1" : "2";
  }
}

/**
 * The entries in the order the sort asks for. Rows with nothing in the
 * sorted column go last in *both* directions — reversing a sort should
 * reverse the values, not float the blanks to the top.
 */
export function sortVaultTable(entries: VaultMeetingView[], sort: VaultSort): VaultMeetingView[] {
  if (isDefaultVaultSort(sort)) return entries;
  const flip = sort.direction === "asc" ? 1 : -1;
  return entries
    .map((entry, index) => ({ entry, index, key: sortKey(entry, sort.column) }))
    .sort((a, b) => {
      if (a.key === null || b.key === null) {
        if (a.key !== null) return -1;
        if (b.key !== null) return 1;
        return a.index - b.index;
      }
      const order = a.key.localeCompare(b.key);
      return order !== 0 ? order * flip : a.index - b.index;
    })
    .map((item) => item.entry);
}
