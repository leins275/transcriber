import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LedgerPanel } from "./LedgerPanel";
import type { LedgerJobView } from "../types";

function buildRow(overrides: Partial<LedgerJobView> = {}): LedgerJobView {
  return {
    job_id: "job-1",
    status: "succeeded",
    created_at: "2026-08-22T15:00:00Z",
    started_at: "2026-08-22T15:00:01Z",
    finished_at: "2026-08-22T15:02:01Z",
    provider: "local",
    model: "large-v3",
    device: "cuda",
    source_path: "D:\\Meetings\\unsorted\\260822 - source\\source.mp4",
    output_path: "D:\\Meetings\\unsorted\\260822 - source",
    // Defaults to a pre-feature row: nothing recorded the original name, so
    // every pre-existing test below exercises the FR-3 display fallback.
    original_file_name: null,
    audio_duration_sec: 3625.8,
    elapsed_sec: 120,
    realtime_factor: 0.03,
    language: "ru",
    segment_count: 1144,
    error_kind: null,
    error_message: null,
    service_version: "0.1.0",
    ...overrides,
  };
}

describe("LedgerPanel", () => {
  it("loads the ledger on mount and shows a row per job", async () => {
    const onLoad = vi.fn().mockResolvedValue([buildRow(), buildRow({ job_id: "job-2" })]);
    render(<LedgerPanel onLoad={onLoad} />);

    expect(await screen.findAllByRole("listitem")).toHaveLength(2);
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("shows the recording's file name, status and timings", async () => {
    render(<LedgerPanel onLoad={() => Promise.resolve([buildRow()])} />);

    expect(await screen.findByText("260822 - source.mp4")).toBeInTheDocument();
    expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/Audio 1h 0m/)).toBeInTheDocument();
    expect(screen.getByText(/Took 2m 0s/)).toBeInTheDocument();
    expect(screen.getByText(/1144 segments/)).toBeInTheDocument();
  });

  it("keeps cancelled distinct from failed", async () => {
    render(<LedgerPanel onLoad={() => Promise.resolve([buildRow({ status: "cancelled" })])} />);

    expect(await screen.findByText(/cancelled/i)).toBeInTheDocument();
    expect(screen.queryByText(/^failed$/i)).not.toBeInTheDocument();
  });

  it("shows a failed job's error kind and message", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              status: "failed",
              error_kind: "internal",
              error_message: "job was interrupted by a service restart",
            }),
          ])
        }
      />,
    );

    expect(await screen.findByText(/internal: job was interrupted/i)).toBeInTheDocument();
  });

  it("says so plainly when the ledger is empty", async () => {
    render(<LedgerPanel onLoad={() => Promise.resolve([])} />);
    expect(await screen.findByText(/no jobs recorded yet/i)).toBeInTheDocument();
  });

  it("surfaces an unreachable service instead of an empty list", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.reject({ kind: "service_unavailable", message: "service unavailable: refused" })
        }
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/service unavailable/i);
    expect(screen.queryByText(/no jobs recorded yet/i)).not.toBeInTheDocument();
  });

  it("shows the recorded original file name instead of source.<ext>", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              original_file_name: "ELS - 260812 - Security issue.mp4",
              source_path: "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
            }),
          ])
        }
      />,
    );

    expect(await screen.findByText("ELS - 260812 - Security issue.mp4")).toBeInTheDocument();
    expect(screen.queryByText("source.mp4")).not.toBeInTheDocument();
  });

  it("derives the meeting folder name for a row with no recorded name", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              original_file_name: null,
              source_path: "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
            }),
          ])
        }
      />,
    );

    expect(await screen.findByText("260812 - Security issue.mp4")).toBeInTheDocument();
  });

  it("derives the meeting folder name from a slash-separated path too", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              original_file_name: null,
              source_path: "/home/op/Meetings/ELS/260812 - Security issue/source.m4a",
            }),
          ])
        }
      />,
    );

    expect(await screen.findByText("260812 - Security issue.m4a")).toBeInTheDocument();
  });

  it("leaves a non-source base name exactly as it renders today", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              original_file_name: null,
              source_path: "D:\\Meetings\\ELS\\260812 - Security issue",
            }),
          ])
        }
      />,
    );

    expect(await screen.findByText("260812 - Security issue")).toBeInTheDocument();
  });

  it("renders rows with no recorded name without throwing, whatever the path", async () => {
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({ job_id: "job-1", original_file_name: null, source_path: null }),
            buildRow({ job_id: "job-2", original_file_name: null, source_path: "source.mp4" }),
            buildRow({ job_id: "job-3", original_file_name: null, source_path: "" }),
          ])
        }
      />,
    );

    expect(await screen.findAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("source.mp4")).toBeInTheDocument();
  });

  it("keeps the full source path in the row's tooltip", async () => {
    const recorded = "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4";
    const derived = "C:\\Meetings\\ELS\\260813 - Roadmap\\source.mp4";
    render(
      <LedgerPanel
        onLoad={() =>
          Promise.resolve([
            buildRow({
              job_id: "job-1",
              original_file_name: "ELS - 260812 - Security issue.mp4",
              source_path: recorded,
            }),
            buildRow({ job_id: "job-2", original_file_name: null, source_path: derived }),
          ])
        }
      />,
    );

    expect(await screen.findByText("ELS - 260812 - Security issue.mp4")).toHaveAttribute(
      "title",
      recorded,
    );
    expect(screen.getByText("260813 - Roadmap.mp4")).toHaveAttribute("title", derived);
  });

  it("re-reads the ledger on Refresh", async () => {
    const onLoad = vi.fn().mockResolvedValue([buildRow()]);
    const user = userEvent.setup();
    render(<LedgerPanel onLoad={onLoad} />);
    await screen.findByText("260822 - source.mp4");

    await user.click(screen.getByRole("button", { name: /refresh/i }));

    expect(onLoad).toHaveBeenCalledTimes(2);
  });
});
