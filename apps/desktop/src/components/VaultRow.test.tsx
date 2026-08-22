import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultRow } from "./VaultRow";
import type { VaultMeetingView } from "../types";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: "ELS",
    meeting_name: "260812 - Security issue",
    meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

describe("VaultRow", () => {
  it("renders the meeting name and its full path in monospace", () => {
    render(<VaultRow entry={buildEntry()} onReveal={() => {}} />);
    expect(screen.getByText("260812 - Security issue")).toBeInTheDocument();
    expect(screen.getByText("D:\\Meetings\\ELS\\260812 - Security issue")).toBeInTheDocument();
  });

  it("shows the project as a pill when sorted", () => {
    render(<VaultRow entry={buildEntry({ project: "ELS" })} onReveal={() => {}} />);
    expect(screen.getByText("ELS")).toBeInTheDocument();
  });

  it("shows an unsorted pill when the entry has no project", () => {
    render(<VaultRow entry={buildEntry({ project: null })} onReveal={() => {}} />);
    expect(screen.getByText(/unsorted/i)).toBeInTheDocument();
  });

  it("reports transcript-ready text when has_transcript is true", () => {
    render(<VaultRow entry={buildEntry({ has_transcript: true })} onReveal={() => {}} />);
    expect(screen.getByText(/transcript ready/i)).toBeInTheDocument();
  });

  it("reports 'no transcript yet' when has_transcript is false", () => {
    render(<VaultRow entry={buildEntry({ has_transcript: false })} onReveal={() => {}} />);
    expect(screen.getByText(/no transcript yet/i)).toBeInTheDocument();
  });

  it("calls onReveal with the entry's id, never a raw path, when Reveal is clicked", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    render(<VaultRow entry={buildEntry({ id: "v-42" })} onReveal={onReveal} />);

    await user.click(screen.getByRole("button", { name: /reveal/i }));

    expect(onReveal).toHaveBeenCalledWith("v-42");
    expect(onReveal).not.toHaveBeenCalledWith(expect.stringContaining("D:\\Meetings"));
  });
});
