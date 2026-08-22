import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { VaultList } from "./VaultList";
import type { VaultMeetingView } from "../types";

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

const actions = {
  onOpen: () => {},
  onReveal: () => {},
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
      expect.stringContaining("One"),
      expect.stringContaining("Two"),
      expect.stringContaining("Three"),
    ]);
  });

  it("renders an empty list without error when there are no entries", () => {
    render(<VaultList entries={[]} {...actions} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
