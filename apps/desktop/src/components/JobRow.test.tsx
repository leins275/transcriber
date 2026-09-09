import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobRow } from "./JobRow";
import type { JobSnapshot, JobState } from "../types";

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\Meetings\\inbox\\file.mp4",
    job_type: "transcribe",
    file_name: "file.mp4",
    state: "pending",
    classification: null,
    meeting_dir: null,
    source_dest: null,
    transcript_path: null,
    progress: null,
    phase: null,
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
      file_name: "ELS - 260812 - Security issue.mp4",
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
    // A sorted job never gets the "filed · unsorted" pill -- "sorted" is the
    // absence of that pill, not a literal label (spec.md 5.6).
    expect(screen.queryByText(/filed . unsorted/i)).not.toBeInTheDocument();
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
    expect(screen.getByText(/filed . unsorted/i)).toBeInTheDocument();
  });

  it("renders the real progress field as a bar and percentage while running (spec.md 5.6)", () => {
    const job = buildJob({ state: "running", progress: 0.42 });
    const { container } = render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText(/42%/)).toBeInTheDocument();
    const fill = container.querySelector('[style*="width"]');
    expect(fill).toHaveStyle({ width: "42%" });
  });

  it("names the parsed project for a sorted job in progress", () => {
    const job = buildJob({
      file_name: "ELS - 260812 - Security issue.mp4",
      state: "running",
      classification: "sorted",
      progress: 0.5,
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText(/Transcribing · 50% · ELS/)).toBeInTheDocument();
  });

  it("names the phase between the verb and the percentage while running", () => {
    const job = buildJob({
      job_type: "diarize",
      state: "running",
      phase: "segmenting speech",
      progress: 0.42,
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText("Identifying speakers · segmenting speech · 42%")).toBeInTheDocument();
  });

  it("names the phase without a percentage when the phase has no measurable fraction", () => {
    const job = buildJob({
      job_type: "summarize",
      state: "running",
      phase: "writing summary · 812 tokens",
      progress: null,
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByText("Summarizing · writing summary · 812 tokens")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("shows a progress bar with no announced value while a phase has no measurable fraction", () => {
    const job = buildJob({
      job_type: "summarize",
      state: "running",
      phase: "writing summary · 812 tokens",
      progress: null,
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });

  it("announces the measured fraction on the progress bar and fills it to match", () => {
    const job = buildJob({
      job_type: "diarize",
      state: "running",
      phase: "segmenting speech",
      progress: 0.42,
    });
    render(<JobRow job={job} onReveal={() => {}} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(bar.querySelector('[style*="width"]')).toHaveStyle({ width: "42%" });
  });

  it.each(["queued", "done", "failed"] as JobState[])(
    "hides a stale phase left over on a %s job",
    (state) => {
      const job = buildJob({
        job_type: "summarize",
        state,
        phase: "writing summary · 812 tokens",
        progress: 0.5,
      });
      render(<JobRow job={job} onReveal={() => {}} />);
      expect(screen.queryByText(/writing summary/)).not.toBeInTheDocument();
    },
  );

  it.each(["queued", "done", "failed"] as JobState[])(
    "shows no progress bar on a %s job",
    (state) => {
      const job = buildJob({
        job_type: "summarize",
        state,
        phase: "writing summary · 812 tokens",
        progress: 0.5,
      });
      render(<JobRow job={job} onReveal={() => {}} />);
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    },
  );

  it("fills the bar no further than full when the service overshoots the fraction", () => {
    const job = buildJob({ state: "running", progress: 1.7 });
    const { container } = render(<JobRow job={job} onReveal={() => {}} />);
    expect(container.querySelector('[style*="width"]')).toHaveStyle({ width: "100%" });
  });
});
