/**
 * Pure presentational parsing of the `Project - YYMMDD - Title.ext` naming
 * convention (spec.md's "sorted vs unsorted" domain concept) purely from the
 * `file_name` field the frozen `JobSnapshot` contract already carries -- no
 * new IPC data, just a display-only re-derivation of the same convention the
 * Rust side already used to set `classification: "sorted"`.
 */

export type ParsedFileName = {
  project: string;
  date: string;
  title: string;
};

/** Returns `null` when `fileName` does not follow the convention -- callers
 * should only trust the result for a job whose `classification` is already
 * `"sorted"` (an unsorted job's name may coincidentally match).
 *
 * Mirrors the Rust side (`vault::parse::classify_filename`): split on the
 * first two `-` separators -- the whitespace around them is optional --
 * and trim spaces from each part. Only the first two hyphens are
 * structural, so a title may itself contain `-`. */
export function parseFileName(fileName: string): ParsedFileName | null {
  const withoutExtension = fileName.replace(/\.[^./\\]+$/, "");
  const firstDash = withoutExtension.indexOf("-");
  const secondDash = firstDash === -1 ? -1 : withoutExtension.indexOf("-", firstDash + 1);
  if (secondDash === -1) return null;
  const project = withoutExtension.slice(0, firstDash).trim();
  const date = withoutExtension.slice(firstDash + 1, secondDash).trim();
  const title = withoutExtension.slice(secondDash + 1).trim();
  if (project.length === 0 || title.length === 0) return null;
  if (!/^\d{6}$/.test(date)) return null;
  return { project, date, title };
}
