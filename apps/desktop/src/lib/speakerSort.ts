/**
 * Pure filtering and column sorting for the library's Speakers table — the
 * sibling of `vaultSort.ts`, over people instead of recordings.
 *
 * The table rests alphabetical by name. Text columns start ascending and
 * number columns start descending (the first click on "Meetings" should
 * answer "who speaks most", not "who speaks least"); a second click on the
 * same column flips it.
 */
import type { SpeakerView } from "../types";

export type SpeakerSortColumn = "name" | "projects" | "meetings" | "speech" | "voice";

export type SpeakerSort = {
  column: SpeakerSortColumn;
  direction: "asc" | "desc";
};

export const DEFAULT_SPEAKER_SORT: SpeakerSort = { column: "name", direction: "asc" };

const TEXT_COLUMNS: SpeakerSortColumn[] = ["name", "projects"];

/** What a click on a column header does to the sort. */
export function nextSpeakerSort(current: SpeakerSort, column: SpeakerSortColumn): SpeakerSort {
  if (current.column === column) {
    return { column, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { column, direction: TEXT_COLUMNS.includes(column) ? "asc" : "desc" };
}

/**
 * The people whose name or one of whose aliases contains the query,
 * case-insensitively. A blank query keeps everybody.
 */
export function filterSpeakers(people: SpeakerView[], query: string): SpeakerView[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return people;
  return people.filter((person) =>
    [person.name, ...person.aliases].some((name) => name.toLowerCase().includes(needle)),
  );
}

function compare(a: SpeakerView, b: SpeakerView, column: SpeakerSortColumn): number {
  switch (column) {
    case "name":
      return a.name.localeCompare(b.name);
    case "projects":
      return a.projects.join(", ").localeCompare(b.projects.join(", "));
    case "meetings":
      return a.meetings - b.meetings;
    case "speech":
      return a.speech_sec - b.speech_sec;
    case "voice":
      return a.voice_samples - b.voice_samples;
  }
}

/**
 * The people in the order the sort asks for. Ties fall back to the name,
 * ascending in either direction, so equal rows never trade places. A person
 * in no project goes last under the Projects sort both ways — reversing a
 * sort should reverse the values, not float the blanks to the top.
 */
export function sortSpeakers(people: SpeakerView[], sort: SpeakerSort): SpeakerView[] {
  const flip = sort.direction === "asc" ? 1 : -1;
  return [...people].sort((a, b) => {
    if (sort.column === "projects") {
      const aBlank = a.projects.length === 0;
      const bBlank = b.projects.length === 0;
      if (aBlank !== bBlank) return aBlank ? 1 : -1;
    }
    const order = compare(a, b, sort.column);
    return order !== 0 ? order * flip : a.name.localeCompare(b.name);
  });
}
