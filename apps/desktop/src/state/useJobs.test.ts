import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import { useJobs } from "./useJobs";
import type { JobSnapshot } from "../types";

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\Meetings\\in\\file.mp4",
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

beforeEach(() => {
  mockWindows("main");
  mockIPC(() => null, { shouldMockEvents: true });
});

afterEach(() => {
  clearMocks();
});

describe("useJobs", () => {
  it("upserts by id from jobs://updated so a job transitions queued -> running -> done with no user action", async () => {
    const { result } = renderHook(() => useJobs());

    await act(async () => {
      await emit("jobs://updated", buildJob({ state: "queued" }));
    });
    await waitFor(() => expect(result.current.jobs).toHaveLength(1));
    expect(result.current.jobs[0].state).toBe("queued");

    await act(async () => {
      await emit("jobs://updated", buildJob({ state: "running", progress: 0.5 }));
    });
    await waitFor(() => expect(result.current.jobs[0].state).toBe("running"));

    await act(async () => {
      await emit(
        "jobs://updated",
        buildJob({ state: "done", transcript_path: "D:\\m\\transcript.json" }),
      );
    });
    await waitFor(() => expect(result.current.jobs[0].state).toBe("done"));
    expect(result.current.jobs).toHaveLength(1);
  });

  it("appends rather than drops an event for an unknown job id", async () => {
    const { result } = renderHook(() => useJobs());

    await act(async () => {
      await emit("jobs://updated", buildJob({ id: "job-1" }));
    });
    await waitFor(() => expect(result.current.jobs).toHaveLength(1));

    await act(async () => {
      await emit("jobs://updated", buildJob({ id: "job-2", file_name: "other.mp4" }));
    });
    await waitFor(() => expect(result.current.jobs).toHaveLength(2));
    expect(result.current.jobs.map((j) => j.id)).toEqual(["job-1", "job-2"]);
  });

  it("does not revert an already-advanced job to pending when enqueue's own response arrives late (E9)", async () => {
    mockIPC(
      (cmd) => {
        if (cmd === "enqueue_paths") {
          return [buildJob({ id: "job-1", state: "pending" })];
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    const { result } = renderHook(() => useJobs());

    // The pipeline has already raced ahead of the `enqueue_paths` response
    // -- a `jobs://updated` event for the same id arrives first, exactly
    // as it would for a small/fast job.
    await act(async () => {
      await emit("jobs://updated", buildJob({ id: "job-1", state: "running", progress: 0.5 }));
    });
    await waitFor(() => expect(result.current.jobs[0]?.state).toBe("running"));

    await act(async () => {
      await result.current.enqueue(["C:\\x\\file.mp4"]);
    });

    // The late-arriving `Pending` snapshot from `enqueue`'s own response
    // must not revert the row, and must not duplicate it either.
    expect(result.current.jobs).toHaveLength(1);
    expect(result.current.jobs[0].state).toBe("running");
  });
});
