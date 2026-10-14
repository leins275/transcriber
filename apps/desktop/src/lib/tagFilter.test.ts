import { describe, expect, it } from "vitest";
import {
  EMPTY_TAG_FILTER,
  entryTags,
  filterByTags,
  pruneTagFilter,
  toggleTag,
  vaultTags,
} from "./tagFilter";
import type { VaultMeetingView } from "../types";

function buildEntry(id: string, project: string | null, meeting_name: string): VaultMeetingView {
  return {
    id,
    project,
    meeting_name,
    meeting_dir: `D:\\Meetings\\${project ?? "unsorted"}\\${meeting_name}`,
    has_source: true,
    has_transcript: true,
  };
}

const entries = [
  buildEntry("a", "GIS", "260812 - Weekly - Standup, Planning"),
  buildEntry("b", null, "260811 - loose - file"),
  buildEntry("c", "ELS", "260810 - Budget - planning"),
  buildEntry("d", "ELS", "260809 - Audit"),
  buildEntry("e", "ELS", "260808 - Retro - Standup"),
];

const ids = (list: VaultMeetingView[]) => list.map((entry) => entry.id);

describe("entryTags", () => {
  it("reads the tags off a filed meeting's folder name", () => {
    expect(entryTags(entries[0])).toEqual(["Standup", "Planning"]);
    expect(entryTags(entries[3])).toEqual([]);
  });

  it("reads no tags out of an unsorted folder's verbatim stem", () => {
    expect(entryTags(entries[1])).toEqual([]);
  });
});

describe("vaultTags", () => {
  it("lists every tag once, A→Z, folding case to the first spelling seen", () => {
    expect(vaultTags(entries)).toEqual(["Planning", "Standup"]);
  });

  it("is empty when no meeting carries a tag", () => {
    expect(vaultTags([entries[3]])).toEqual([]);
  });
});

describe("toggleTag", () => {
  it("adds a tag that is not chosen and removes one that is, case-insensitively", () => {
    const one = toggleTag(EMPTY_TAG_FILTER, "Standup");
    expect(one.tags).toEqual(["Standup"]);
    expect(toggleTag(one, "standup").tags).toEqual([]);
  });

  it("keeps the match mode", () => {
    expect(toggleTag({ tags: [], match: "all" }, "Standup").match).toBe("all");
  });
});

describe("filterByTags", () => {
  it("lets everything through when nothing is chosen", () => {
    expect(filterByTags(entries, EMPTY_TAG_FILTER)).toBe(entries);
    expect(filterByTags(entries, { tags: [], match: "all" })).toBe(entries);
  });

  it("keeps rows carrying any of the chosen tags in 'any' mode", () => {
    expect(ids(filterByTags(entries, { tags: ["Standup", "Planning"], match: "any" }))).toEqual([
      "a",
      "c",
      "e",
    ]);
  });

  it("keeps only rows carrying every chosen tag in 'all' mode", () => {
    expect(ids(filterByTags(entries, { tags: ["Standup", "Planning"], match: "all" }))).toEqual([
      "a",
    ]);
  });

  it("matches tags case-insensitively", () => {
    expect(ids(filterByTags(entries, { tags: ["PLANNING"], match: "any" }))).toEqual(["a", "c"]);
  });
});

describe("pruneTagFilter", () => {
  it("drops chosen tags that are no longer on offer and returns the same object otherwise", () => {
    const filter = { tags: ["Standup", "Gone"], match: "any" as const };
    expect(pruneTagFilter(filter, ["Standup"]).tags).toEqual(["Standup"]);
    expect(pruneTagFilter(filter, ["standup", "gone"])).toBe(filter);
  });
});
