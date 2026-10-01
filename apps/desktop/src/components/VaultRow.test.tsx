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
  // The row is a `<tr>`, so it needs a table around it to be valid DOM.
  return render(
    <table>
      <tbody>
        <VaultRow {...defaults} {...props} />
      </tbody>
    </table>,
  );
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
    expect(screen.getByText(/no transcript yet/i)).toBeInTheDocument();
  });

  it("says when a meeting folder holds no recording at all", () => {
    renderRow({ entry: buildEntry({ has_transcript: false, has_source: false }) });
    expect(screen.getByText(/no recording/i)).toBeInTheDocument();
  });

  it("opens the recording by id, once, when its name is clicked", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onOpen });

    await user.click(screen.getByText("Security issue"));

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("v-42");
    expect(onOpen).not.toHaveBeenCalledWith(expect.stringContaining("D:\\Meetings"));
  });

  it("opens the recording from anywhere on the row", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onOpen });

    await user.click(screen.getByText(/transcript ready/i));

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("v-42");
  });

  it("names the row's project in its own column", () => {
    renderRow({ entry: buildEntry({ project: "GIS" }) });
    expect(screen.getByText("GIS")).toBeInTheDocument();
  });

  it("says Unsorted where an unsorted row has no project", () => {
    renderRow({ entry: buildEntry({ project: null }) });
    expect(screen.getByText("Unsorted")).toBeInTheDocument();
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
