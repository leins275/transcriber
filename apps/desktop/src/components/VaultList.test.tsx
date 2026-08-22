import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { VaultList } from "./VaultList";
import type { TranscriptView, VaultMeetingView } from "../types";

function buildEntry(id: string, meeting_name: string): VaultMeetingView {
  return {
    id,
    project: "ELS",
    meeting_name,
    meeting_dir: `D:\\Meetings\\ELS\\${meeting_name}`,
    has_source: true,
    has_transcript: true,
  };
}

const transcript: TranscriptView = {
  entry_id: "a",
  meeting_name: "260812 - One",
  language: null,
  created_at: null,
  duration_sec: null,
  model: null,
  device: null,
  text: "",
  segments: [],
};

const actions = {
  projects: ["ELS"],
  onReveal: () => {},
  onReadTranscript: () => Promise.resolve(transcript),
  onUpdate: () => Promise.resolve(),
  onDelete: () => Promise.resolve(),
};

describe("VaultList", () => {
  it("renders every entry in the order given, keyed by id, none lost", () => {
    const entries = [
      buildEntry("a", "260812 - One"),
      buildEntry("b", "260811 - Two"),
      buildEntry("c", "260810 - Three"),
    ];
    render(<VaultList entries={entries} {...actions} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("260812 - One"),
      expect.stringContaining("260811 - Two"),
      expect.stringContaining("260810 - Three"),
    ]);
  });

  it("renders an empty list without error when there are no entries", () => {
    render(<VaultList entries={[]} {...actions} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
