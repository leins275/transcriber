import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultRow } from "./VaultRow";
import type { TranscriptView, VaultMeetingView } from "../types";

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

function buildTranscript(overrides: Partial<TranscriptView> = {}): TranscriptView {
  return {
    entry_id: "v-1",
    meeting_name: "260812 - Security issue",
    language: "ru",
    created_at: "2026-08-22T15:29:58Z",
    duration_sec: 3625.8,
    model: "large-v3",
    device: "cuda",
    text: "Да, ребят, всем привет.",
    segments: [{ id: 0, start: 0, end: 2.5, text: " Да, ребят," }],
    ...overrides,
  };
}

function renderRow(props: Partial<React.ComponentProps<typeof VaultRow>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["ELS", "GIS"],
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(buildTranscript()),
    onUpdate: () => Promise.resolve(),
    onDelete: () => Promise.resolve(),
  };
  return render(<VaultRow {...defaults} {...props} />);
}

describe("VaultRow", () => {
  it("renders the meeting name and its full path in monospace", () => {
    renderRow();
    expect(screen.getByText("260812 - Security issue")).toBeInTheDocument();
    expect(screen.getByText("D:\\Meetings\\ELS\\260812 - Security issue")).toBeInTheDocument();
  });

  it("shows the project as a pill when sorted", () => {
    renderRow({ entry: buildEntry({ project: "ELS" }) });
    expect(screen.getByText("ELS")).toBeInTheDocument();
  });

  it("shows an unsorted pill when the entry has no project", () => {
    renderRow({ entry: buildEntry({ project: null }) });
    expect(screen.getByText(/unsorted/i)).toBeInTheDocument();
  });

  it("reports transcript-ready text when has_transcript is true", () => {
    renderRow({ entry: buildEntry({ has_transcript: true }) });
    expect(screen.getByText(/transcript ready/i)).toBeInTheDocument();
  });

  it("reports 'no transcript yet' when has_transcript is false", () => {
    renderRow({ entry: buildEntry({ has_transcript: false }) });
    expect(screen.getByText(/no transcript yet/i)).toBeInTheDocument();
  });

  it("renders the meeting's date in readable form from its folder name", () => {
    renderRow({ entry: buildEntry({ meeting_name: "260812 - Security issue" }) });
    // Locale-dependent month name, so match on the year the folder name
    // alone does not spell out.
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("calls onReveal with the entry's id, never a raw path, when Reveal is clicked", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onReveal });

    await user.click(screen.getByRole("button", { name: /reveal/i }));

    expect(onReveal).toHaveBeenCalledWith("v-42");
    expect(onReveal).not.toHaveBeenCalledWith(expect.stringContaining("D:\\Meetings"));
  });

  it("loads and shows the transcript by entry id when Transcript is clicked", async () => {
    const onReadTranscript = vi.fn().mockResolvedValue(buildTranscript());
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onReadTranscript });

    await user.click(screen.getByRole("button", { name: /transcript/i }));

    expect(onReadTranscript).toHaveBeenCalledWith("v-42");
    expect(await screen.findByText(/Да, ребят,/)).toBeInTheDocument();
  });

  it("does not re-read a transcript that is already loaded when reopened", async () => {
    const onReadTranscript = vi.fn().mockResolvedValue(buildTranscript());
    const user = userEvent.setup();
    renderRow({ onReadTranscript });

    const button = screen.getByRole("button", { name: /transcript/i });
    await user.click(button);
    await screen.findByText(/Да, ребят,/);
    await user.click(button); // collapse
    await user.click(button); // reopen

    expect(onReadTranscript).toHaveBeenCalledTimes(1);
  });

  it("surfaces a transcript read failure in the row instead of throwing", async () => {
    const onReadTranscript = vi
      .fn()
      .mockRejectedValue({ kind: "vault", message: "transcript.json could not be read" });
    const user = userEvent.setup();
    renderRow({ onReadTranscript });

    await user.click(screen.getByRole("button", { name: /transcript/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/i);
  });

  it("disables Transcript for a meeting that has none", () => {
    renderRow({ entry: buildEntry({ has_transcript: false }) });
    expect(screen.getByRole("button", { name: /transcript/i })).toBeDisabled();
  });

  it("offers Summary as a disabled, not-yet-built action", () => {
    renderRow();
    expect(screen.getByRole("button", { name: /summary/i })).toBeDisabled();
  });

  it("opens the rename form and saves by entry id", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onUpdate });

    await user.click(screen.getByRole("button", { name: /rename/i }));
    const title = screen.getByLabelText(/title/i);
    await user.clear(title);
    await user.type(title, "Renamed meeting");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledWith("v-42", {
      project: "ELS",
      date: "260812",
      title: "Renamed meeting",
    });
  });

  it("asks for confirmation before deleting, and deletes by entry id", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderRow({ entry: buildEntry({ id: "v-42" }), onDelete });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.getByText(/you can restore it from there/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /move to recycle bin/i }));
    expect(onDelete).toHaveBeenCalledWith("v-42");
  });

  it("cancelling the delete confirmation deletes nothing", async () => {
    const onDelete = vi.fn();
    const user = userEvent.setup();
    renderRow({ onDelete });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /move to recycle bin/i })).not.toBeInTheDocument();
  });

  it("shows a failed delete in the row and keeps the confirmation open", async () => {
    const onDelete = vi.fn().mockRejectedValue({ kind: "io", message: "recycle bin unavailable" });
    const user = userEvent.setup();
    renderRow({ onDelete });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    await user.click(screen.getByRole("button", { name: /move to recycle bin/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/recycle bin unavailable/i);
    expect(screen.getByRole("button", { name: /move to recycle bin/i })).toBeInTheDocument();
  });

  it("opens only one panel at a time", async () => {
    const user = userEvent.setup();
    renderRow();

    await user.click(screen.getByRole("button", { name: /rename/i }));
    expect(screen.getByLabelText(/title/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(screen.queryByLabelText(/title/i)).not.toBeInTheDocument();
    expect(screen.getByText(/you can restore it from there/i)).toBeInTheDocument();
  });
});
