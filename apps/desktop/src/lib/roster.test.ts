import { describe, expect, it } from "vitest";
import {
  EMPTY_ROSTER,
  addRosterName,
  mergeRosterNames,
  normalizeRosterNames,
  pickerSource,
  removeRosterName,
} from "./roster";
import type { ProjectRosterView } from "../types";

/**
 * The FR-2 normalization fixture, shared **verbatim** with
 * `apps/desktop/src-tauri/tests/roster.rs`: trimmed, blanks dropped,
 * case-insensitive duplicates collapsed onto the first spelling, original
 * order preserved. If these two literals ever diverge between the TypeScript
 * and the Rust file, the editor is showing a list the backend would silently
 * rewrite on save.
 */
const MESSY_NAMES = ["  Anna ", "anna", "", "Maxim", "Maxim"];
const NORMALIZED_NAMES = ["Anna", "Maxim"];

function roster(mode: ProjectRosterView["mode"], names: string[]): ProjectRosterView {
  return { mode, names };
}

describe("normalizeRosterNames", () => {
  it("trims, drops blanks and collapses case-insensitive duplicates onto the first spelling", () => {
    expect(normalizeRosterNames(MESSY_NAMES)).toEqual(NORMALIZED_NAMES);
  });

  it("keeps the operator's order rather than sorting the names", () => {
    expect(normalizeRosterNames(["Olga", "Anna", "Maxim"])).toEqual(["Olga", "Anna", "Maxim"]);
  });
});

describe("addRosterName", () => {
  it("appends the trimmed name to the end of the roster", () => {
    expect(addRosterName(roster("roster", ["Anna"]), "  Olga ")).toEqual(
      roster("roster", ["Anna", "Olga"]),
    );
  });

  it("ignores a blank name", () => {
    expect(addRosterName(roster("roster", ["Anna"]), "   ")).toEqual(roster("roster", ["Anna"]));
  });

  it("ignores a name already in the roster under a different casing", () => {
    expect(addRosterName(roster("roster", ["Anna"]), "anna")).toEqual(roster("roster", ["Anna"]));
  });

  it("does not mutate the roster it is given", () => {
    const original = roster("roster", ["Anna"]);

    addRosterName(original, "Olga");

    expect(original).toEqual(roster("roster", ["Anna"]));
  });
});

describe("removeRosterName", () => {
  it("drops exactly that name and leaves the rest in order", () => {
    expect(removeRosterName(roster("roster", ["Anna", "Maxim", "Olga"]), "Maxim")).toEqual(
      roster("roster", ["Anna", "Olga"]),
    );
  });
});

describe("mergeRosterNames", () => {
  it("appends only the names the roster does not already hold", () => {
    expect(mergeRosterNames(roster("roster", ["Maxim"]), ["maxim", "Olga"])).toEqual(
      roster("roster", ["Maxim", "Olga"]),
    );
  });

  it("changes nothing when every name is already listed", () => {
    expect(mergeRosterNames(roster("roster", ["Anna", "Maxim"]), ["Maxim", "anna"])).toEqual(
      roster("roster", ["Anna", "Maxim"]),
    );
  });
});

describe("pickerSource", () => {
  const siblingNames = ["Olga", "Pavel"];

  it("offers the sibling names as suggestions and no picker when the project has no roster", () => {
    expect(pickerSource(undefined, siblingNames)).toEqual({
      roster: undefined,
      suggestions: siblingNames,
    });
  });

  it("keeps free text in open mode even when the roster already holds names", () => {
    expect(pickerSource(roster("open", ["Anna", "Maxim"]), siblingNames)).toEqual({
      roster: undefined,
      suggestions: siblingNames,
    });
  });

  it("offers the roster instead of the sibling names in roster mode", () => {
    expect(pickerSource(roster("roster", ["Anna", "Maxim"]), siblingNames)).toEqual({
      roster: ["Anna", "Maxim"],
      suggestions: [],
    });
  });

  it("offers an empty picker rather than free text for an empty roster in roster mode", () => {
    expect(pickerSource(roster("roster", []), siblingNames)).toEqual({
      roster: [],
      suggestions: [],
    });
  });
});

describe("EMPTY_ROSTER", () => {
  it("is the open roster with no names — what a project without a roster file reads as", () => {
    expect(EMPTY_ROSTER).toEqual({ mode: "open", names: [] });
  });
});
