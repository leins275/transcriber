/**
 * Pure grouping and ordering for the vault browser's two meeting tabs.
 *
 * The split is the same one the vault itself makes on disk: a meeting either
 * sits under a project folder or under `unsorted/`. The Rust side already
 * decides which (and capitalizes the project code), so nothing here
 * re-derives a classification from a name — it only slices and orders what
 * `list_vault` returned.
 */
import type { VaultMeetingView } from "../types";

/** Every meeting filed under `unsorted/`, in the order given. */
export function unsortedEntries(entries: VaultMeetingView[]): VaultMeetingView[] {
  return entries.filter((entry) => entry.project === null);
}

/** Every meeting filed under a real project, in the order given. */
export function sortedEntries(entries: VaultMeetingView[]): VaultMeetingView[] {
  return entries.filter((entry) => entry.project !== null);
}

/**
 * The distinct project codes present in the vault, A→Z.
 *
 * Alphabetical rather than by recency: this feeds a picker the operator
 * scans for a code they already have in mind, and a list that reorders
 * itself as meetings arrive is a list they have to re-read every time.
 */
export function projectCodes(entries: VaultMeetingView[]): string[] {
  const codes = new Set<string>();
  for (const entry of entries) {
    if (entry.project !== null) codes.add(entry.project);
  }
  return [...codes].sort((a, b) => a.localeCompare(b));
}

/** The meetings under one project code, in the order given. */
export function entriesForProject(
  entries: VaultMeetingView[],
  project: string,
): VaultMeetingView[] {
  return entries.filter((entry) => entry.project === project);
}

/**
 * Keeps a selected project valid as the vault changes.
 *
 * Re-filing the last meeting out of a project makes that code vanish; rather
 * than leaving the tab pointed at a project that no longer exists (an empty
 * list with no explanation), fall back to the first available code, or to
 * `null` when the vault holds no projects at all.
 */
export function resolveSelectedProject(
  available: string[],
  selected: string | null,
): string | null {
  if (selected !== null && available.includes(selected)) return selected;
  return available[0] ?? null;
}

/**
 * Re-applies the backend's own ordering — newest meeting date first, by the
 * `YYMMDD` prefix of the folder name — to a list that has been edited in
 * place.
 *
 * Renaming a meeting can change its date, and re-fetching the whole vault
 * just to reorder one row would reissue every entry id and collapse whatever
 * the operator had open. Sorting locally with the same rule keeps the list
 * honest without that. A name with no parseable date sorts after every dated
 * one, then by name — exactly what `vault::list::list_meetings` does.
 */
export function sortVaultEntries(entries: VaultMeetingView[]): VaultMeetingView[] {
  return entries.slice().sort((a, b) => {
    const dateA = datePrefix(a.meeting_name);
    const dateB = datePrefix(b.meeting_name);
    if (dateA !== null && dateB !== null) {
      if (dateA !== dateB) return dateB.localeCompare(dateA);
      return a.meeting_name.localeCompare(b.meeting_name);
    }
    if (dateA !== null) return -1;
    if (dateB !== null) return 1;
    return a.meeting_name.localeCompare(b.meeting_name);
  });
}

/**
 * The leading `YYMMDD` of a meeting folder name, or `null`.
 *
 * Only checks that the six characters are digits — not that they denote a
 * real calendar date — because this is a sort key, and `260230` still orders
 * more usefully among dated names than it would banished to the undated tail.
 */
function datePrefix(meetingName: string): string | null {
  const prefix = meetingName.slice(0, 6);
  return /^\d{6}$/.test(prefix) ? prefix : null;
}
