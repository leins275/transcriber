import { describe, expect, it } from "vitest";
import { activeJobView } from "./activeJob";
import type { JobSnapshot } from "../types";

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\x\\ELS - 260825 - Incident review.mp4",
    file_name: "ELS - 260825 - Incident review.mp4",
    job_type: "transcribe",
    state: "running",
    classification: "sorted",
    meeting_dir: null,
    source_dest: null,
    transcript_path: null,
    progress: null,
    message: null,
    error_kind: null,
    created_at: "2026-08-25T00:00:00Z",
    ...overrides,
  };
}

describe("activeJobView", () => {
  it("returns null when nothing is in flight", () => {
    expect(activeJobView([])).toBeNull();
    expect(
      activeJobView([
        buildJob({ state: "done" }),
        buildJob({ id: "job-2", state: "failed" }),
        buildJob({ id: "job-3", state: "rejected" }),
      ]),
    ).toBeNull();
  });

  it("narrates a running transcription with the project, title and percent", () => {
    const view = activeJobView([buildJob({ progress: 0.42 })]);
    expect(view).toEqual({ label: "Transcribing “ELS - Incident review”", percent: 42 });
  });

  it("omits the percent while progress is unreported", () => {
    expect(activeJobView([buildJob()])?.percent).toBeNull();
  });

  it("prefers the running job over queued ones", () => {
    const view = activeJobView([
      buildJob({ id: "job-q", state: "queued" }),
      buildJob({
        id: "job-r",
        state: "running",
        job_type: "summarize",
        file_name: "260825 - Weekly sync",
        progress: 0.1,
      }),
    ]);
    expect(view?.label).toBe("Summarizing “Weekly sync”");
    expect(view?.percent).toBe(10);
  });

  it("falls back to a queued job so the chip survives between chain stages", () => {
    const view = activeJobView([
      buildJob({ state: "queued", job_type: "action_items", file_name: "260825 - Weekly sync" }),
    ]);
    expect(view).toEqual({ label: "Extracting action items “Weekly sync”", percent: null });
  });

  it("says Filing during ingest, before transcription starts", () => {
    const view = activeJobView([buildJob({ state: "ingesting" })]);
    expect(view?.label).toBe("Filing “ELS - Incident review”");
  });

  it("shows an unconventional file name as-is, minus its extension", () => {
    const view = activeJobView([
      buildJob({ file_name: "recording.mp3", classification: "unsorted" }),
    ]);
    expect(view?.label).toBe("Transcribing “recording”");
  });
});
