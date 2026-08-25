import { describe, expect, it } from "vitest";
import { entriesForProject, projectCodes, sortVaultEntries, unsortedEntries } from "./vaultGroups";
import type { VaultMeetingView } from "../types";

function entry(
  id: string,
  project: string | null,
  meeting_name = "260812 - Meeting",
): VaultMeetingView {
  return {
    id,
    project,
    meeting_name,
    meeting_dir: `D:\\Meetings\\${project ?? "unsorted"}\\${meeting_name}`,
    has_source: true,
    has_transcript: true,
  };
}

describe("unsortedEntries", () => {
  it("keeps only project-less meetings, in the order given", () => {
    const entries = [entry("a", "ELS"), entry("b", null), entry("c", "GIS")];

    expect(unsortedEntries(entries).map((e) => e.id)).toEqual(["b"]);
  });
});

describe("projectCodes", () => {
  it("returns each code once, alphabetically, ignoring unsorted", () => {
    const entries = [entry("a", "GIS"), entry("b", null), entry("c", "ELS"), entry("d", "GIS")];

    expect(projectCodes(entries)).toEqual(["ELS", "GIS"]);
  });

  it("is empty for a vault with only unsorted recordings", () => {
    expect(projectCodes([entry("a", null)])).toEqual([]);
  });
});

describe("entriesForProject", () => {
  it("returns only that project's meetings, in the order given", () => {
    const entries = [
      entry("a", "ELS", "260812 - One"),
      entry("b", "GIS", "260811 - Two"),
      entry("c", "ELS", "260810 - Three"),
    ];

    expect(entriesForProject(entries, "ELS").map((e) => e.meeting_name)).toEqual([
      "260812 - One",
      "260810 - Three",
    ]);
  });
});

describe("sortVaultEntries", () => {
  it("orders newest meeting date first", () => {
    const entries = [
      entry("a", "ELS", "260101 - Oldest"),
      entry("b", "ELS", "260812 - Newest"),
      entry("c", "ELS", "260601 - Middle"),
    ];

    expect(sortVaultEntries(entries).map((e) => e.meeting_name)).toEqual([
      "260812 - Newest",
      "260601 - Middle",
      "260101 - Oldest",
    ]);
  });

  it("sorts a name with no parseable date after every dated one", () => {
    const entries = [entry("a", "ELS", "not a date at all"), entry("b", "ELS", "260101 - Dated")];

    expect(sortVaultEntries(entries).map((e) => e.meeting_name)).toEqual([
      "260101 - Dated",
      "not a date at all",
    ]);
  });

  it("does not mutate the array it is given", () => {
    const entries = [entry("a", "ELS", "260101 - Old"), entry("b", "ELS", "260812 - New")];
    const snapshot = entries.map((e) => e.id);

    sortVaultEntries(entries);

    expect(entries.map((e) => e.id)).toEqual(snapshot);
  });
});
