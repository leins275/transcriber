import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobsPanel } from "./JobsPanel";
import type { JobSnapshot } from "../types";

function buildJob(id: string, file_name: string): JobSnapshot {
  return {
    id,
    source_path: `C:\\Meetings\\inbox\\${file_name}`,
    file_name,
    state: "pending",
    classification: null,
    meeting_dir: null,
    source_dest: null,
    transcript_path: null,
    progress: null,
    message: null,
    error_kind: null,
    created_at: "2026-08-21T00:00:00Z",
  };
}

describe("JobsPanel", () => {
  it("exposes a Jobs region and the session count", () => {
    const jobs = [buildJob("a", "one.mp4"), buildJob("b", "two.mp4")];
    render(<JobsPanel jobs={jobs} onReveal={() => {}} />);
    expect(screen.getByRole("region", { name: /jobs/i })).toBeInTheDocument();
    expect(screen.getByText(/2 this session/i)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("shows a zero count with no jobs", () => {
    render(<JobsPanel jobs={[]} onReveal={() => {}} />);
    expect(screen.getByText(/0 this session/i)).toBeInTheDocument();
  });
});
