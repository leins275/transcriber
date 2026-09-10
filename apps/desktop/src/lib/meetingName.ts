/**
 * Pure helpers over a *meeting folder's* own name —
 * `<YYMMDD> - <Title>[ - <Type>]` — as opposed to `fileName.ts`, which parses
 * a dropped *file's* name (`<Project> - <YYMMDD> - <Title>[ - <Type>].<ext>`).
 * The two conventions differ by exactly the project component, because a
 * filed meeting already carries its project in the folder it sits under.
 *
 * `-` is reserved as the separator in both: the name is split on *every*
 * hyphen, so a folder with more sections than the convention allows (a
 * recorder's default name such as `260731 - Запись встречи 11-04-56 - запись`)
 * does not parse at all rather than folding the extra hyphens into the title.
 *
 * Display-only: nothing here decides where anything lands on disk. The Rust
 * side (`vault::manage`, `vault::paths::parse_meeting_folder_name`) owns the
 * grammar and re-validates every part of a rename against the same rules
 * ingest uses, so this module's job is to seed the edit form with sensible
 * values and to render names, never to gate a save.
 */

export type ParsedMeetingName = {
  /** Six digits, `YYMMDD`. */
  date: string;
  title: string;
  /** The optional meeting type — the third section — or `null` when the
   * folder name carries only a date and a title. */
  kind: string | null;
};

/** Six digits, no calendar validation — `formatMeetingDate` is what decides
 * whether they denote a real day. Mirrors the Rust folder-name parser. */
const DATE = /^\d{6}$/;

/** Trims ASCII spaces only, the same trimming `vault::paths` applies to a
 * folder name's sections: whitespace around a separator is optional, but a
 * tab or a control character stays part of the section it sits in. */
function trimSpaces(section: string): string {
  return section.replace(/^ +| +$/g, "");
}

/**
 * Splits a meeting folder name into its date, title and optional type, or
 * returns `null` when the folder does not follow the convention (a
 * hand-renamed folder, one from before the convention existed, or one whose
 * own text contains the reserved `-`).
 */
export function parseMeetingName(meetingName: string): ParsedMeetingName | null {
  const sections = meetingName.split("-").map(trimSpaces);
  if (sections.length < 2 || sections.length > 3) return null;
  if (sections.some((section) => section === "")) return null;
  const [date, title, kind] = sections;
  if (!DATE.test(date)) return null;
  return { date, title, kind: kind ?? null };
}

/**
 * Reads an `unsorted/` folder's name: `<ingest date> - <stem>`, split on the
 * **first** hyphen only, with the stem kept exactly as it is.
 *
 * `unsorted/` is where the vault puts a recording whose name did *not*
 * conform (`vault::paths::unsorted_folder_name` prefixes the ingest date to
 * the sanitized stem, hyphens included), so the stem's own hyphens are the
 * operator's text and not separators. `null` when there is no six-digit
 * date in front — a folder made by hand, which callers show verbatim.
 */
function parseUnsortedName(meetingName: string): ParsedMeetingName | null {
  const separator = meetingName.indexOf("-");
  if (separator < 0) return null;
  const date = trimSpaces(meetingName.slice(0, separator));
  const stem = trimSpaces(meetingName.slice(separator + 1));
  if (!DATE.test(date) || stem === "") return null;
  return { date, title: stem, kind: null };
}

/**
 * How one *listed* meeting's folder name should be read for display, given
 * the project it is filed under (`null` = it sits in `unsorted/`).
 *
 * A meeting under a project carries a name this app itself built to the
 * convention, so it reads under the full grammar — title plus an optional
 * type. A meeting under `unsorted/` does not: only its date prefix is the
 * app's, the rest is the dropped file's stem kept verbatim *because* the
 * name did not conform. Reading that under the typed grammar would invent a
 * type out of the operator's own text — a drop of `ELS - 260812.mp4` would
 * show as title `ELS` with a `260812` tag — so an unsorted folder never
 * carries a type here, and keeps its whole stem as the title (what the app
 * showed before the type existed).
 *
 * The edit form deliberately does *not* use this: `meetingEditDefaults`
 * keeps seeding an unsorted name through the full grammar, so re-filing one
 * into a project stays a single click that produces a name the convention
 * accepts, rather than a hyphenated title the rename would refuse.
 */
export function parseEntryName(
  meetingName: string,
  project: string | null,
): ParsedMeetingName | null {
  return project === null ? parseUnsortedName(meetingName) : parseMeetingName(meetingName);
}

/**
 * The values the edit form should open with for a meeting.
 *
 * A folder whose name does not parse still has to be editable — that is
 * precisely the folder most in need of a rename — so the whole name becomes
 * the title and the date falls back to empty, leaving the operator to supply
 * one rather than the app inventing a date it cannot know. The type stays
 * empty for the same reason: it is not this helper's job to guess which of
 * the sections the operator meant as one.
 */
export function meetingEditDefaults(meetingName: string): ParsedMeetingName {
  return parseMeetingName(meetingName) ?? { date: "", title: meetingName, kind: null };
}

/**
 * Renders a `YYMMDD` string as a readable date (`22 Aug 2026`), or returns
 * the input unchanged when it is not six digits denoting a real calendar
 * date — never a "NaN" or a silently wrong date.
 */
export function formatMeetingDate(date: string): string {
  if (!/^\d{6}$/.test(date)) return date;
  const year = 2000 + Number(date.slice(0, 2));
  const month = Number(date.slice(2, 4));
  const day = Number(date.slice(4, 6));
  const parsed = new Date(Date.UTC(year, month - 1, day));
  const roundTrips =
    parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === day;
  if (!roundTrips) return date;
  return parsed.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}
