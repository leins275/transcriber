import { describe, expect, it } from "vitest";
import {
  formatMeetingDate,
  meetingEditDefaults,
  parseEntryName,
  parseMeetingName,
} from "./meetingName";

describe("parseMeetingName", () => {
  it("splits an untyped folder name into date and title", () => {
    expect(parseMeetingName("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: null,
    });
  });

  it("reads a third section as the meeting type", () => {
    expect(parseMeetingName("260812 - Security issue - Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });

  it("reads the type from a name written without spaces around the separators", () => {
    expect(parseMeetingName("260812-Security issue-Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });

  it("treats a hyphen inside what used to be the title as the type separator", () => {
    expect(parseMeetingName("260812 - Q3 - review")).toEqual({
      date: "260812",
      title: "Q3",
      kind: "review",
    });
  });

  it("keeps non-ASCII text in both the title and the type", () => {
    expect(parseMeetingName("260828 - Q&A Сессия с Дмитрием - поиск рабочего триггера")).toEqual({
      date: "260828",
      title: "Q&A Сессия с Дмитрием",
      kind: "поиск рабочего триггера",
    });
  });

  it("returns null for a folder name with more sections than the convention allows", () => {
    expect(parseMeetingName("260731 - Запись встречи 31.07.2026 11-04-56 - запись")).toBeNull();
  });

  it("returns null when the type section is present but empty", () => {
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
      kind: null,
    });
    expect(parseMeetingName("260812 -Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: null,
    });
  });
});

describe("parseEntryName", () => {
  it("reads a meeting filed under a project under the typed grammar", () => {
    expect(parseEntryName("260812 - Security issue - Standup", "ELS")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });

  it("never reads a type out of an unsorted folder's verbatim stem", () => {
    // `unsorted/` is where a *non-conforming* name is kept as it was, so the
    // stem's own hyphens are not separators: `ELS - 260812.mp4` ingested on
    // 2026-09-10 must not display as title `ELS` with a `260812` type.
    expect(parseEntryName("260910 - ELS - 260812", null)).toEqual({
      date: "260910",
      title: "ELS - 260812",
      kind: null,
    });
    expect(parseEntryName("260910 - just one - separator", null)).toEqual({
      date: "260910",
      title: "just one - separator",
      kind: null,
    });
  });

  it("keeps the ingest date and the whole stem of a many-hyphened unsorted folder", () => {
    expect(parseEntryName("260910 - Запись встречи 31.07.2026 11-04-56 - запись", null)).toEqual({
      date: "260910",
      title: "Запись встречи 31.07.2026 11-04-56 - запись",
      kind: null,
    });
  });

  it("reads a hyphen-free unsorted folder exactly as before", () => {
    expect(parseEntryName("260910 - recording_final(1)", null)).toEqual({
      date: "260910",
      title: "recording_final(1)",
      kind: null,
    });
    expect(parseEntryName("260910-recording_final(1)", null)).toEqual({
      date: "260910",
      title: "recording_final(1)",
      kind: null,
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
  it("seeds the form from an untyped name", () => {
    expect(meetingEditDefaults("260812 - Security issue")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: null,
    });
  });

  it("seeds the type from a typed name", () => {
    expect(meetingEditDefaults("260812 - Security issue - Standup")).toEqual({
      date: "260812",
      title: "Security issue",
      kind: "Standup",
    });
  });

  it("puts a non-conforming name entirely in the title, inventing no date", () => {
    expect(meetingEditDefaults("recording final v2")).toEqual({
      date: "",
      title: "recording final v2",
      kind: null,
    });
  });

  it("puts a name with too many sections entirely in the title", () => {
    expect(meetingEditDefaults("260731 - Запись встречи 31.07.2026 11-04-56 - запись")).toEqual({
      date: "",
      title: "260731 - Запись встречи 31.07.2026 11-04-56 - запись",
      kind: null,
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
