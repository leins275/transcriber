import { describe, expect, it } from "vitest";
import { parseFileName } from "./fileName";

describe("parseFileName", () => {
  it("parses the Project - YYMMDD - Title convention as an untyped meeting", () => {
    expect(parseFileName("ELS - 260812 - Security issue.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: null,
    });
  });

  it("reads a fourth section as the meeting type", () => {
    expect(parseFileName("ELS - 260812 - Security issue - Standup.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });

  it("keeps spaces inside a section, so a multi-word type stays one type", () => {
    expect(parseFileName("GIS - 260724 - Weekly sync - Sprint review.mp4")).toEqual({
      project: "GIS",
      date: "260724",
      title: "Weekly sync",
      kind: "Sprint review",
    });
  });

  it("gives a trailing ' - ' segment to the type instead of the title", () => {
    expect(parseFileName("ACME - 260731 - Weekly sync - part 2.m4a")).toEqual({
      project: "ACME",
      date: "260731",
      title: "Weekly sync",
      kind: "part 2",
    });
  });

  it("treats a hyphen inside a compact name as a separator, not part of the title", () => {
    expect(parseFileName("ELS-260812-follow-up call.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "follow",
      kind: "up call",
    });
  });

  it("returns null for a name with more than four sections", () => {
    expect(parseFileName("ELS - 260812 - a - b - c.mp4")).toBeNull();
  });

  it("returns null when the fourth section is present but empty", () => {
    expect(parseFileName("ELS - 260812 - Title - .mp4")).toBeNull();
    expect(parseFileName("ELS - 260812 - Title -.mp4")).toBeNull();
  });

  it("returns null for names that do not follow the convention", () => {
    expect(parseFileName("zoom_recording_2026_08_14.mp4")).toBeNull();
    expect(parseFileName("Screen Recording 2026-08-01 at 14.22.31.mov")).toBeNull();
  });

  it("treats whitespace around the separators as optional", () => {
    expect(parseFileName("ELS-260812-Security issue.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: null,
    });
    expect(parseFileName("ELS -260812- Security issue.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: null,
    });
  });

  it("keeps a tab beside a separator, so the name does not parse", () => {
    // Only ASCII spaces are trimmed, mirroring the Rust parser: the tab
    // stays inside the date section, which is then no longer six digits.
    // A `String.trim()` here would accept the name and put this mirror out
    // of step with `vault::classify_filename`.
    expect(parseFileName("ELS -\t260812 - T.mp4")).toBeNull();
  });

  it("trims the spaces around a separator that precedes a type", () => {
    expect(parseFileName("ELS - 260812 - Security issue -Standup.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });
});
