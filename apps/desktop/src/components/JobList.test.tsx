import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobList } from "./JobList";
import type { JobSnapshot } from "../types";

function buildJob(id: string, file_name: string): JobSnapshot {
  return {
    id,
    source_path: `C:\\Meetings\\inbox\\${file_name}`,
    job_type: "transcribe",
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

describe("JobList", () => {
  it("renders three jobs in submission order, keyed by id, none lost", () => {
    const jobs = [buildJob("a", "one.mp4"), buildJob("b", "two.mp4"), buildJob("c", "three.mp4")];
    render(<JobList jobs={jobs} onReveal={() => {}} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("one.mp4"),
      expect.stringContaining("two.mp4"),
      expect.stringContaining("three.mp4"),
    ]);
  });

  it("renders an empty list without error when there are no jobs", () => {
    render(<JobList jobs={[]} onReveal={() => {}} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
