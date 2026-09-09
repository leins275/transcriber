/**
 * Pure helpers over a project's speaker roster (`<PROJECT>/roster.json`).
 *
 * Two jobs, both deliberately free of IPC so they can be reasoned about (and
 * tested) on their own:
 *
 *   1. **Normalization**, mirrored from the Rust side of the boundary
 *      (`commands/roster.rs`): trim, drop blanks, collapse case-insensitive
 *      duplicates onto the first spelling, keep the operator's order. The
 *      editor applies exactly these rules while the operator types, so the
 *      list it shows is the list `save_project_roster` will store -- a name
 *      never changes shape on save. Case folding is `toLowerCase()`, never
 *      `toLocaleLowerCase()`: the locale-independent Unicode default mapping
 *      is what Rust's `str::to_lowercase` implements, and a Turkish-locale
 *      host would otherwise fold "Ilya" to "ılya" here and "ilya" there.
 *   2. **`pickerSource`** -- the single place that turns a roster's mode into
 *      what the two speaker name controls offer: a pick-list over the roster
 *      (`roster` mode) or free text with the sibling-scan names as datalist
 *      hints (`open` mode, and any project whose roster has not loaded).
 *
 * The roster holds names only. It is not a voice store: cross-meeting speaker
 * recognition remains the on-demand sibling scan in the service.
 */
import type { ProjectRosterView } from "../types";

/** What a project with no `roster.json` reads as, and the safe fallback for a
 * read that failed -- naming keeps working, unrestricted. */
export const EMPTY_ROSTER: ProjectRosterView = { mode: "open", names: [] };

/**
 * Trims, drops blanks and collapses case-insensitive duplicates onto the
 * first spelling, preserving order. Idempotent, and identical to the Rust
 * normalization the command applies before writing.
 */
export function normalizeRosterNames(names: string[]): string[] {
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (const candidate of names) {
    const name = candidate.trim();
    if (name === "") continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    normalized.push(name);
  }
  return normalized;
}

/** Appends one name to the end of the roster. A blank name, or one already
 * listed under any casing, changes nothing. Never mutates its argument. */
export function addRosterName(roster: ProjectRosterView, name: string): ProjectRosterView {
  return { mode: roster.mode, names: normalizeRosterNames([...roster.names, name]) };
}

/** Drops that name from the roster, matched the way the roster itself
 * deduplicates -- case-insensitively -- and leaves the rest in order. */
export function removeRosterName(roster: ProjectRosterView, name: string): ProjectRosterView {
  const target = name.trim().toLowerCase();
  return {
    mode: roster.mode,
    names: roster.names.filter((listed) => listed.trim().toLowerCase() !== target),
  };
}

/** Appends the seed names the roster does not already hold, in the order they
 * arrive -- the "Add names already used in this project" button, which seeds
 * from the sibling scan and never removes anything. */
export function mergeRosterNames(roster: ProjectRosterView, seed: string[]): ProjectRosterView {
  return { mode: roster.mode, names: normalizeRosterNames([...roster.names, ...seed]) };
}

/** What a speaker name control should offer. `roster` present -- even empty --
 * means a pick-list over exactly those names; `undefined` means free text with
 * `suggestions` as datalist hints. */
export type PickerSource = {
  roster: string[] | undefined;
  suggestions: string[];
};

/**
 * Turns a project's roster into the control's source of names.
 *
 * In `roster` mode the roster is the only source, sibling-scan names included
 * or not; an empty roster still yields an (empty) pick-list rather than
 * falling back to free text -- strict mode means new names come from the
 * roster editor, not from the transcript. In `open` mode, and for a meeting
 * with no project or no loaded roster, the roster names are ignored entirely
 * and the sibling names stay the hints they are today.
 */
export function pickerSource(
  roster: ProjectRosterView | undefined,
  siblingNames: string[],
): PickerSource {
  if (roster?.mode === "roster") {
    return { roster: roster.names, suggestions: [] };
  }
  return { roster: undefined, suggestions: siblingNames };
}
