import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobRow } from "./JobRow";
import type { JobSnapshot, JobState } from "../types";

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\Meetings\\inbox\\file.mp4",
    file_name: "file.mp4",
    state: "pending",
    classification: null,
    meeting_dir: null,
    source_dest: null,
    transcript_path: null,
    progress: null,
    message: null,
    error_kind: null,
    created_at: "2026-08-21T00:00:00Z",
    ...overrides,
  };
}

const ALL_STATES: JobState[] = [
  "pending",
  "ingesting",
  "queued",
  "running",
  "done",
  "failed",
  "rejected",
];

describe("JobRow", () => {
  it.each(ALL_STATES)("renders the file name for state %s", (state) => {
    render(<JobRow job={buildJob({ state })} onReveal={() => {}} />);
    expect(screen.getByText("file.mp4")).toBeInTheDocument();
  });

  it("shows the full transcript path and an enabled Reveal control when done", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    const job = buildJob({
      state: "done",
      transcript_path: "D:\\Meetings\\ELS\\260812 - Security issue\\transcript.json",
    });
    render(<JobRow job={job} onReveal={onReveal} />);
    expect(
      screen.getByText("D:\\Meetings\\ELS\\260812 - Security issue\\transcript.json"),
    ).toBeInTheDocument();
    const reveal = screen.getByRole("button", { name: /reveal/i });
    expect(reveal).toBeEnabled();
    await user.click(reveal);
    expect(onReveal).toHaveBeenCalledWith("job-1");
  });

  it("does not render a Reveal control for a non-done job", () => {
    render(<JobRow job={buildJob({ state: "running" })} onReveal={() => {}} />);
    expect(screen.queryByRole("button", { name: /reveal/i })).not.toBeInTheDocument();
  });

  it("renders the backend failure message verbatim", () => {
    const job = buildJob({
      state: "failed",
      message: "F2: model failed to load: out of memory",
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText("F2: model failed to load: out of memory")).toBeInTheDocument();
  });

  it("names the file and its unsupported extension when rejected", () => {
    const job = buildJob({
      file_name: "notes.txt",
      state: "rejected",
      error_kind: "unsupported_extension",
      message: 'Unsupported extension ".txt" for "notes.txt"',
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText('Unsupported extension ".txt" for "notes.txt"')).toBeInTheDocument();
  });

  it("shows a busy indication while ingesting", () => {
    render(<JobRow job={buildJob({ state: "ingesting" })} onReveal={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent(/ingesting/i);
  });

  it("enables Reveal for a job filed but still awaiting/failed transcription (E14, FR-13)", async () => {
    // The recording is already filed (meeting_dir/source_dest set) even
    // though transcription itself failed -- e.g. the service was down.
    // The operator must still be able to see where it landed and reveal
    // it, not only once transcription also succeeds.
    const onReveal = vi.fn();
    const user = userEvent.setup();
    const job = buildJob({
      state: "failed",
      classification: "sorted",
      meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
      source_dest: "D:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
      message: "service unavailable",
    });
    render(<JobRow job={job} onReveal={onReveal} />);

    expect(
      screen.getByText("D:\\Meetings\\ELS\\260812 - Security issue\\source.mp4"),
    ).toBeInTheDocument();
    expect(screen.getByText("sorted")).toBeInTheDocument();
    const reveal = screen.getByRole("button", { name: /reveal/i });
    expect(reveal).toBeEnabled();
    await user.click(reveal);
    expect(onReveal).toHaveBeenCalledWith("job-1");
  });

  it("renders the destination as soon as it exists, not only once done (E14)", () => {
    const job = buildJob({
      state: "running",
      classification: "unsorted",
      meeting_dir: "D:\\Meetings\\unsorted\\260821 - file",
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText("D:\\Meetings\\unsorted\\260821 - file")).toBeInTheDocument();
    expect(screen.getByText("unsorted")).toBeInTheDocument();
  });
});
