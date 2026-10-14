/**
 * Pure tag filtering for the library's recordings table.
 *
 * A meeting's tags are read off its folder name (`parseEntryName`), so this
 * module owns no data of its own: the set of tags on offer is derived from
 * the rows on screen, and the filter is a list of chosen tags plus how they
 * combine — `any` (a row needs one of them, "or") or `all` (a row needs
 * every one, "and"). Tags compare case-insensitively, the way the Rust side
 * folds duplicates when it writes a folder name, so `standup` and `Standup`
 * are one tag here too.
 */
import { parseEntryName } from "./meetingName";
import type { VaultMeetingView } from "../types";

/** How the chosen tags combine: `any` is "or", `all` is "and". */
export type TagMatch = "any" | "all";

export type TagFilter = {
  tags: string[];
  match: TagMatch;
};

/** Nothing chosen: every row passes. */
export const EMPTY_TAG_FILTER: TagFilter = { tags: [], match: "any" };

function fold(tag: string): string {
  return tag.toLocaleLowerCase();
}

/** The tags one listed meeting carries, or none for an unsorted or
 * hand-named folder. */
export function entryTags(entry: VaultMeetingView): string[] {
  return parseEntryName(entry.meeting_name, entry.project)?.tags ?? [];
}

/**
 * The distinct tags across `entries`, A→Z, one spelling per tag (the first
 * seen wins, like the folder-name writer). Alphabetical for the same reason
 * the project picker is: a row of chips the operator scans for a tag they
 * already have in mind should not reorder itself as meetings arrive.
 */
export function vaultTags(entries: VaultMeetingView[]): string[] {
  const seen = new Map<string, string>();
  for (const entry of entries) {
    for (const tag of entryTags(entry)) {
      const key = fold(tag);
      if (!seen.has(key)) seen.set(key, tag);
    }
  }
  return [...seen.values()].sort((a, b) => a.localeCompare(b));
}

export function hasTag(filter: TagFilter, tag: string): boolean {
  const key = fold(tag);
  return filter.tags.some((chosen) => fold(chosen) === key);
}

/** The filter with `tag` added if absent, removed if present. */
export function toggleTag(filter: TagFilter, tag: string): TagFilter {
  const key = fold(tag);
  const tags = hasTag(filter, tag)
    ? filter.tags.filter((chosen) => fold(chosen) !== key)
    : [...filter.tags, tag];
  return { ...filter, tags };
}

/**
 * The filter narrowed to the tags in `available` — the chips on screen —
 * so a choice that outlived its target (the last tagged meeting re-filed
 * out of the project in view) silently stops applying rather than leaving
 * an empty list with no explanation, the way a stale project pick does.
 */
export function pruneTagFilter(filter: TagFilter, available: string[]): TagFilter {
  const keys = new Set(available.map(fold));
  const tags = filter.tags.filter((tag) => keys.has(fold(tag)));
  return tags.length === filter.tags.length ? filter : { ...filter, tags };
}

/** The rows the filter lets through, in the order given. An empty choice
 * lets everything through, whatever the match mode. */
export function filterByTags(entries: VaultMeetingView[], filter: TagFilter): VaultMeetingView[] {
  if (filter.tags.length === 0) return entries;
  const wanted = filter.tags.map(fold);
  return entries.filter((entry) => {
    const carried = new Set(entryTags(entry).map(fold));
    return filter.match === "all"
      ? wanted.every((tag) => carried.has(tag))
      : wanted.some((tag) => carried.has(tag));
  });
}
