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

function renderRow(props: Partial<React.ComponentProps<typeof VaultRow>> = {}) {
  const defaults = {
    entry: buildEntry(),
    onOpen: () => {},
  };
  return render(<VaultRow {...defaults} {...props} />);
}

describe("VaultRow", () => {
  it("shows the meeting's title, not its folder name", () => {
    renderRow();
    expect(screen.getByText("Security issue")).toBeInTheDocument();
  });

  it("falls back to the whole folder name when it does not follow the convention", () => {
    renderRow({ entry: buildEntry({ meeting_name: "recording final v2" }) });
    expect(screen.getByText("recording final v2")).toBeInTheDocument();
  });

  it("renders the meeting's date readably", () => {
    renderRow();
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("reports transcript state", () => {
    renderRow({ entry: buildEntry({ has_transcript: true }) });
    expect(screen.getByText(/transcript ready/i)).toBeInTheDocument();
  });

  it("says a filed recording is awaiting transcription", () => {
    renderRow({ entry: buildEntry({ has_transcript: false, has_source: true }) });
    expect(screen.getByText(/filed, no transcript yet/i)).toBeInTheDocument();
  });

  it("opens the recording by id when its name is clicked", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onOpen });

    await user.click(screen.getByText("Security issue"));

    expect(onOpen).toHaveBeenCalledWith("v-42");
    expect(onOpen).not.toHaveBeenCalledWith(expect.stringContaining("D:\\Meetings"));
  });

  it("names the row's project as a tag", () => {
    renderRow({ entry: buildEntry({ project: "GIS" }) });
    expect(screen.getByText("GIS")).toBeInTheDocument();
  });

  it("omits the project tag when told to (grouped view) and for unsorted rows", () => {
    const { rerender } = renderRow({ entry: buildEntry({ project: "GIS" }), showProject: false });
    expect(screen.queryByText("GIS")).not.toBeInTheDocument();

    rerender(<VaultRow entry={buildEntry({ project: null })} onOpen={() => {}} />);
    expect(screen.queryByText("GIS")).not.toBeInTheDocument();
  });

  it("carries no per-row action buttons — opening the recording is the row", () => {
    // Transcript/Reveal used to sit on every row; they now live on the
    // recording's own page, so the row's single button is its content.
    renderRow();
    expect(screen.queryByRole("button", { name: /^transcript$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reveal/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
});
