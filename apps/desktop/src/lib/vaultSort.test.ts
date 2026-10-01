import { describe, expect, it } from "vitest";
import { DEFAULT_VAULT_SORT, nextVaultSort, sortVaultTable } from "./vaultSort";
import type { VaultMeetingView } from "../types";

function buildEntry(
  id: string,
  project: string | null,
  meeting_name: string,
  overrides: Partial<VaultMeetingView> = {},
): VaultMeetingView {
  return {
    id,
    project,
    meeting_name,
    meeting_dir: `D:\\Meetings\\${project ?? "unsorted"}\\${meeting_name}`,
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

// Newest first, the way the vault listing arrives.
const entries = [
  buildEntry("a", "GIS", "260812 - Weekly - Sync"),
  buildEntry("b", null, "260811 - loose file", { has_transcript: false }),
  buildEntry("c", "ELS", "260810 - Budget"),
  buildEntry("d", "ELS", "260809 - Audit - Review", { has_source: false, has_transcript: false }),
];

function ids(sorted: VaultMeetingView[]): string[] {
  return sorted.map((entry) => entry.id);
}

describe("sortVaultTable", () => {
  it("leaves the listing's own order alone for the default sort", () => {
    expect(sortVaultTable(entries, DEFAULT_VAULT_SORT)).toBe(entries);
  });

  it("sorts oldest first when the date is ascending", () => {
    expect(ids(sortVaultTable(entries, { column: "date", direction: "asc" }))).toEqual([
      "d",
      "c",
      "b",
      "a",
    ]);
  });

  it("sorts by project, newest first within one, unsorted last", () => {
    expect(ids(sortVaultTable(entries, { column: "project", direction: "asc" }))).toEqual([
      "c",
      "d",
      "a",
      "b",
    ]);
  });

  it("keeps rows with no value last when the direction reverses", () => {
    expect(ids(sortVaultTable(entries, { column: "project", direction: "desc" }))).toEqual([
      "a",
      "c",
      "d",
      "b",
    ]);
    expect(ids(sortVaultTable(entries, { column: "type", direction: "desc" }))).toEqual([
      "a",
      "d",
      "b",
      "c",
    ]);
  });

  it("sorts by the title, not the folder name", () => {
    expect(ids(sortVaultTable(entries, { column: "name", direction: "asc" }))).toEqual([
      "d",
      "c",
      "b",
      "a",
    ]);
  });

  it("sorts by transcript state: ready, awaiting, no recording", () => {
    expect(ids(sortVaultTable(entries, { column: "transcript", direction: "asc" }))).toEqual([
      "a",
      "c",
      "b",
      "d",
    ]);
  });
});

describe("nextVaultSort", () => {
  it("cycles a column ascending, descending, then back to newest first", () => {
    const first = nextVaultSort(DEFAULT_VAULT_SORT, "name");
    expect(first).toEqual({ column: "name", direction: "asc" });
    const second = nextVaultSort(first, "name");
    expect(second).toEqual({ column: "name", direction: "desc" });
    expect(nextVaultSort(second, "name")).toEqual(DEFAULT_VAULT_SORT);
  });

  it("flips the date column between newest and oldest first", () => {
    const oldest = nextVaultSort(DEFAULT_VAULT_SORT, "date");
    expect(oldest).toEqual({ column: "date", direction: "asc" });
    expect(nextVaultSort(oldest, "date")).toEqual(DEFAULT_VAULT_SORT);
  });

  it("returns to newest first when the date is clicked from another column", () => {
    expect(nextVaultSort({ column: "project", direction: "asc" }, "date")).toEqual(
      DEFAULT_VAULT_SORT,
    );
  });
});
