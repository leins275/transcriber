/**
 * Pure presentational parsing of the `Project - YYMMDD - Title[ - tag, tag].ext`
 * naming convention (spec.md's "sorted vs unsorted" domain concept) purely
 * from the `file_name` field the frozen `JobSnapshot` contract already
 * carries -- no new IPC data, just a display-only re-derivation of the same
 * convention the Rust side already used to set `classification: "sorted"`.
 */

import { splitTags } from "./meetingName";

export type ParsedFileName = {
  project: string;
  date: string;
  title: string;
  /** The optional fourth section split on `,`, the meeting's tags; empty
   * when the name has only the three required sections. */
  tags: string[];
};

/** ASCII spaces around a separator are optional; anything else (a tab, a
 * control character) stays in the section, mirroring the Rust parser, which
 * trims `' '` only and lets the rest reach its validators. */
function trimSpaces(section: string): string {
  return section.replace(/^ +| +$/g, "");
}

/** Returns `null` when `fileName` does not follow the convention -- callers
 * should only trust the result for a job whose `classification` is already
 * `"sorted"` (an unsorted job's name may coincidentally match).
 *
 * Mirrors the Rust side (`vault::parse::classify_filename`): `-` is reserved
 * as the separator, so the stem is split on *every* hyphen and each section
 * is trimmed of spaces. Three sections are an untagged meeting (`tags: []`),
 * four carry the comma-separated tags, and anything else -- five or more
 * sections, a missing separator, an empty section, a tag section with no tag
 * in it, a date that is not six digits -- is non-conforming and gives
 * `null`. Being display-only, this mirror does not
 * repeat the Rust validators' character rules (illegal characters, reserved
 * device names); those names route to `unsorted` on the Rust side. */
export function parseFileName(fileName: string): ParsedFileName | null {
  const withoutExtension = fileName.replace(/\.[^./\\]+$/, "");
  const sections = withoutExtension.split("-").map(trimSpaces);
  if (sections.length !== 3 && sections.length !== 4) return null;
  const [project, date, title, tagSection] = sections;
  if (project.length === 0 || title.length === 0) return null;
  const tags = tagSection === undefined ? [] : splitTags(tagSection);
  if (tagSection !== undefined && tags.length === 0) return null;
  if (!/^\d{6}$/.test(date)) return null;
  return { project, date, title, tags };
}
