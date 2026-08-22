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
    onReveal: () => {},
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

  it("offers Transcript for a transcribed recording and Open otherwise", () => {
    const { unmount } = renderRow();
    expect(screen.getByRole("button", { name: /^transcript$/i })).toBeInTheDocument();
    unmount();

    renderRow({ entry: buildEntry({ has_transcript: false }) });
    expect(screen.getByRole("button", { name: /^open$/i })).toBeInTheDocument();
  });

  it("calls onReveal with the entry's id, never a raw path", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-9" }), onReveal });

    await user.click(screen.getByRole("button", { name: /reveal/i }));

    expect(onReveal).toHaveBeenCalledWith("v-9");
  });
});
