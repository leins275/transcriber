import { describe, expect, it } from "vitest";
import { formatMeetingDate, meetingEditDefaults, parseMeetingName } from "./meetingName";

describe("parseMeetingName", () => {
  it("splits a conforming folder name into date and title", () => {
    expect(parseMeetingName("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
    });
  });

  it("keeps a separator that appears inside the title", () => {
    expect(parseMeetingName("260812 - Q3 - review")?.title).toBe("Q3 - review");
  });

  it("returns null for a folder that does not follow the convention", () => {
    expect(parseMeetingName("just a folder")).toBeNull();
    expect(parseMeetingName("26081 - Too short")).toBeNull();
    expect(parseMeetingName("260812 -")).toBeNull();
  });
});

describe("meetingEditDefaults", () => {
  it("seeds the form from a conforming name", () => {
    expect(meetingEditDefaults("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
    });
  });

  it("puts a non-conforming name entirely in the title, inventing no date", () => {
    expect(meetingEditDefaults("recording final v2")).toEqual({
      date: "",
      title: "recording final v2",
    });
  });
});

describe("formatMeetingDate", () => {
  it("renders a YYMMDD date readably", () => {
    // Rendered in the operator's own locale, so assert the parts rather
    // than an English month name the test host may not use.
    const rendered = formatMeetingDate("260812");
    expect(rendered).toContain("2026");
    expect(rendered).toContain("12");
    expect(rendered).not.toBe("260812");
  });

  it("returns the input unchanged when it is not six digits", () => {
    expect(formatMeetingDate("nope")).toBe("nope");
    expect(formatMeetingDate("")).toBe("");
  });

  it("returns the input unchanged for six digits that are not a real date", () => {
    expect(formatMeetingDate("260230")).toBe("260230");
    expect(formatMeetingDate("261332")).toBe("261332");
  });
});
