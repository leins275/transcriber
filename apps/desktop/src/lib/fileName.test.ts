import { describe, expect, it } from "vitest";
import { parseFileName } from "./fileName";

describe("parseFileName", () => {
  it("parses the Project - YYMMDD - Title convention", () => {
    expect(parseFileName("ELS - 260812 - Security issue.mp4")).toEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
    });
  });

  it("keeps trailing ' - ' segments in the title, not the project", () => {
    expect(parseFileName("ACME - 260731 - Weekly sync - part 2.m4a")).toEqual({
      project: "ACME",
      date: "260731",
      title: "Weekly sync - part 2",
    });
  });

  it("returns null for names that do not follow the convention", () => {
    expect(parseFileName("zoom_recording_2026_08_14.mp4")).toBeNull();
    expect(parseFileName("Screen Recording 2026-08-01 at 14.22.31.mov")).toBeNull();
  });
});
