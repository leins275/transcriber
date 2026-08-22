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

const NAMING_CONVENTION = /^(.+?) - (\d{6}) - (.+)$/;

/** Returns `null` when `fileName` does not follow the convention -- callers
 * should only trust the result for a job whose `classification` is already
 * `"sorted"` (an unsorted job's name may coincidentally match). */
export function parseFileName(fileName: string): ParsedFileName | null {
  const withoutExtension = fileName.replace(/\.[^./\\]+$/, "");
  const match = NAMING_CONVENTION.exec(withoutExtension);
  if (!match) return null;
  const [, project, date, title] = match;
  return { project, date, title };
}
