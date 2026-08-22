/**
 * Pure helpers over a *meeting folder's* own name — `<YYMMDD> - <Title>` —
 * as opposed to `fileName.ts`, which parses a dropped *file's* name
 * (`<Project> - <YYMMDD> - <Title>.<ext>`). The two conventions differ by
 * exactly the project component, because a filed meeting already carries its
 * project in the folder it sits under.
 *
 * Display-only: nothing here decides where anything lands on disk. The Rust
 * side (`vault::manage`) re-validates every part of a rename against the same
 * rules ingest uses, so this module's job is to seed the edit form with
 * sensible values and to render names, never to gate a save.
 */

export type ParsedMeetingName = {
  /** Six digits, `YYMMDD`. */
  date: string;
  title: string;
};

// Whitespace around the `-` separator is optional, the same rule the
// filename convention follows (`fileName.ts`, `vault::parse`): a
// hand-renamed `260812-Title` folder still parses.
const MEETING_NAME = /^(\d{6}) *- *(.+)$/;

/**
 * Splits a meeting folder name into its date and title, or returns `null`
 * when the folder does not follow the convention (a hand-renamed folder, or
 * one from before the convention existed).
 */
export function parseMeetingName(meetingName: string): ParsedMeetingName | null {
  const match = MEETING_NAME.exec(meetingName);
  if (!match) return null;
  const [, date, title] = match;
  return { date, title: title.trim() };
}

/**
 * The values the edit form should open with for a meeting.
 *
 * A folder whose name does not parse still has to be editable — that is
 * precisely the folder most in need of a rename — so the whole name becomes
 * the title and the date falls back to empty, leaving the operator to supply
 * one rather than the app inventing a date it cannot know.
 */
export function meetingEditDefaults(meetingName: string): ParsedMeetingName {
  return parseMeetingName(meetingName) ?? { date: "", title: meetingName };
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
