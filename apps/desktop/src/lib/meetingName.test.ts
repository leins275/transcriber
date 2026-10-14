import { describe, expect, it } from "vitest";
import {
  formatMeetingDate,
  joinTags,
  meetingEditDefaults,
  parseEntryName,
  parseMeetingName,
  splitTags,
} from "./meetingName";

describe("parseMeetingName", () => {
  it("splits an untagged folder name into date and title", () => {
    expect(parseMeetingName("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: [],
    });
  });

  it("reads a third section as a tag", () => {
    expect(parseMeetingName("260812 - Security issue - Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup"],
    });
  });

  it("splits a comma-separated third section into several tags", () => {
    expect(parseMeetingName("260812 - Security issue - Standup, Q3")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup", "Q3"],
    });
    expect(parseMeetingName("260812-Security issue-Standup,Q3")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup", "Q3"],
    });
  });

  it("returns null when the tag section holds only commas", () => {
    expect(parseMeetingName("260812 - Title - , ,")).toBeNull();
  });

  it("reads the tag from a name written without spaces around the separators", () => {
    expect(parseMeetingName("260812-Security issue-Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup"],
    });
  });

  it("treats a hyphen inside what used to be the title as the tag section's separator", () => {
    expect(parseMeetingName("260812 - Q3 - review")).toEqual({
      date: "260812",
      title: "Q3",
      tags: ["review"],
    });
  });

  it("keeps non-ASCII text in both the title and a tag", () => {
    expect(parseMeetingName("260828 - Q&A Сессия с Дмитрием - поиск рабочего триггера")).toEqual({
      date: "260828",
      title: "Q&A Сессия с Дмитрием",
      tags: ["поиск рабочего триггера"],
    });
  });

  it("returns null for a folder name with more sections than the convention allows", () => {
    expect(parseMeetingName("260731 - Запись встречи 31.07.2026 11-04-56 - запись")).toBeNull();
  });

  it("returns null when the tag section is present but empty", () => {
    expect(parseMeetingName("260812 - Title -")).toBeNull();
  });

  it("returns null when the title section is empty between two separators", () => {
    expect(parseMeetingName("260812 - - K")).toBeNull();
  });

  it("keeps a tab beside a separator, so the name does not parse", () => {
    // Only ASCII spaces are trimmed, mirroring `vault::parse_meeting_folder_name`:
    // the tab stays inside the date section, which is then no longer six
    // digits. A `String.trim()` here would accept the name and put this
    // mirror out of step with Rust.
    expect(parseMeetingName("260812\t- Security issue")).toBeNull();
    expect(parseMeetingName("260812\t- Security issue - Standup")).toBeNull();
  });

  it("returns null for a folder that does not follow the convention", () => {
    expect(parseMeetingName("just a folder")).toBeNull();
    expect(parseMeetingName("26081 - Too short")).toBeNull();
    expect(parseMeetingName("260812 -")).toBeNull();
  });

  it("treats whitespace around the separator as optional", () => {
    expect(parseMeetingName("260812-Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: [],
    });
    expect(parseMeetingName("260812 -Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: [],
    });
  });
});

describe("parseEntryName", () => {
  it("reads a meeting filed under a project under the tagged grammar", () => {
    expect(parseEntryName("260812 - Security issue - Standup", "ELS")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup"],
    });
  });

  it("never reads tags out of an unsorted folder's verbatim stem", () => {
    // `unsorted/` is where a *non-conforming* name is kept as it was, so the
    // stem's own hyphens are not separators: `ELS - 260812.mp4` ingested on
    // 2026-09-10 must not display as title `ELS` with a `260812` tag.
    expect(parseEntryName("260910 - ELS - 260812", null)).toEqual({
      date: "260910",
      title: "ELS - 260812",
      tags: [],
    });
    expect(parseEntryName("260910 - just one - separator", null)).toEqual({
      date: "260910",
      title: "just one - separator",
      tags: [],
    });
  });

  it("keeps the ingest date and the whole stem of a many-hyphened unsorted folder", () => {
    expect(parseEntryName("260910 - Запись встречи 31.07.2026 11-04-56 - запись", null)).toEqual({
      date: "260910",
      title: "Запись встречи 31.07.2026 11-04-56 - запись",
      tags: [],
    });
  });

  it("reads a hyphen-free unsorted folder exactly as before", () => {
    expect(parseEntryName("260910 - recording_final(1)", null)).toEqual({
      date: "260910",
      title: "recording_final(1)",
      tags: [],
    });
    expect(parseEntryName("260910-recording_final(1)", null)).toEqual({
      date: "260910",
      title: "recording_final(1)",
      tags: [],
    });
  });

  it("returns null for an unsorted folder that carries no ingest date", () => {
    expect(parseEntryName("hand made folder", null)).toBeNull();
    expect(parseEntryName("26081 - Too short", null)).toBeNull();
    expect(parseEntryName("260910 -", null)).toBeNull();
    expect(parseEntryName("260910\t- tabbed", null)).toBeNull();
  });
});

describe("meetingEditDefaults", () => {
  it("seeds the form from an untagged name", () => {
    expect(meetingEditDefaults("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: [],
    });
  });

  it("seeds the tags from a tagged name", () => {
    expect(meetingEditDefaults("260812 - Security issue - Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      tags: ["Standup"],
    });
  });

  it("puts a non-conforming name entirely in the title, inventing no date", () => {
    expect(meetingEditDefaults("recording final v2")).toEqual({
      date: "",
      title: "recording final v2",
      tags: [],
    });
  });

  it("puts a name with too many sections entirely in the title", () => {
    expect(meetingEditDefaults("260731 - Запись встречи 31.07.2026 11-04-56 - запись")).toEqual({
      date: "",
      title: "260731 - Запись встречи 31.07.2026 11-04-56 - запись",
      tags: [],
    });
  });
});

describe("splitTags / joinTags", () => {
  it("splits on commas, trims spaces and drops empty pieces", () => {
    expect(splitTags(" Standup ,Q3,, Sprint review ,")).toEqual(["Standup", "Q3", "Sprint review"]);
    expect(splitTags("")).toEqual([]);
    expect(splitTags(" , ")).toEqual([]);
  });

  it("keeps a tab inside a tag rather than trimming it, like the Rust side", () => {
    expect(splitTags("Standup,\tQ3")).toEqual(["Standup", "\tQ3"]);
  });

  it("joins with the folder name's canonical ', '", () => {
    expect(joinTags(["Standup", "Q3"])).toBe("Standup, Q3");
    expect(joinTags([])).toBe("");
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
