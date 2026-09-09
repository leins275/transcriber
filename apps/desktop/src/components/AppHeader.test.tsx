import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppHeader } from "./AppHeader";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import { activeJobView } from "../lib/activeJob";
import type { JobSnapshot, ServiceStatusView } from "../types";

const readyStatus: ServiceStatusView = { state: "ready", base_url: null, detail: null };

function modelStatus(overrides: Partial<ModelDownloadStatus> = {}): ModelDownloadStatus {
  return {
    state: "complete",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 100,
    error_kind: null,
    error_message: null,
    model_present: true,
    cuda_warning: null,
    cuda_runtime_present: null,
    ...overrides,
  };
}

/** A job snapshot as `useJobs` hands it over -- the header narrates whatever
 * `activeJobView` derives from these, so the tests below go through the real
 * derivation rather than hand-built view objects. */
function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:/vault/ELS/260825 - Weekly sync",
    file_name: "260825 - Weekly sync",
    job_type: "export",
    state: "running",
    classification: "sorted",
    meeting_dir: null,
    source_dest: null,
    transcript_path: null,
    progress: null,
    phase: null,
    message: null,
    error_kind: null,
    created_at: "2026-08-25T00:00:00Z",
    ...overrides,
  };
}

describe("AppHeader", () => {
  it("shows the brand and a one-line status chip with service, device and model", () => {
    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={modelStatus({ cuda_runtime_present: true })}
        settingsOpen={false}
        onToggleSettings={() => {}}
      />,
    );
    expect(screen.getByText("Transcriber")).toBeInTheDocument();
    expect(screen.getByText(/Ready · GPU · large-v3/)).toBeInTheDocument();
  });

  it("omits the model suffix while the model is not installed", () => {
    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={modelStatus({ model_present: false, cuda_runtime_present: false })}
        settingsOpen={false}
        onToggleSettings={() => {}}
      />,
    );
    expect(screen.getByText("Ready · CPU")).toBeInTheDocument();
    expect(screen.queryByText(/large-v3/)).not.toBeInTheDocument();
  });

  it("replaces the status chip with the in-flight job's progress, and the chip returns to Recordings", async () => {
    const user = userEvent.setup();
    const onShowRecordings = vi.fn();
    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={modelStatus({ cuda_runtime_present: true })}
        settingsOpen={false}
        onToggleSettings={() => {}}
        activeJob={{ label: "Transcribing “ELS - Incident review”", percent: 42, phase: null }}
        onShowRecordings={onShowRecordings}
      />,
    );

    expect(screen.queryByText(/Ready · GPU/)).not.toBeInTheDocument();
    const chip = screen.getByRole("button", { name: /Transcribing “ELS - Incident review”/ });
    expect(chip).toHaveTextContent("· 42%");
    await user.click(chip);
    expect(onShowRecordings).toHaveBeenCalledTimes(1);
  });

  it("omits the percent from the progress chip while it is unreported", () => {
    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={false}
        onToggleSettings={() => {}}
        activeJob={{ label: "Summarizing “Weekly sync”", percent: null, phase: null }}
        onShowRecordings={() => {}}
      />,
    );
    expect(screen.getByText("Summarizing “Weekly sync”")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("fires onToggleSettings from the gear, which reflects the open state", async () => {
    const user = userEvent.setup();
    const onToggleSettings = vi.fn();
    const { rerender } = render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={false}
        onToggleSettings={onToggleSettings}
      />,
    );

    const gear = screen.getByRole("button", { name: /settings/i });
    expect(gear).toHaveAttribute("aria-pressed", "false");
    await user.click(gear);
    expect(onToggleSettings).toHaveBeenCalledTimes(1);

    rerender(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={true}
        onToggleSettings={onToggleSettings}
      />,
    );
    expect(screen.getByRole("button", { name: /settings/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("narrates the running job's phase when it reports no percent", () => {
    const activeJob = activeJobView([buildJob({ progress: null, phase: "rendering PDF" })]);

    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={false}
        onToggleSettings={() => {}}
        activeJob={activeJob}
        onShowRecordings={() => {}}
      />,
    );

    const chip = screen.getByRole("button", { name: /Exporting PDF “Weekly sync”/ });
    expect(chip).toHaveTextContent("· rendering PDF");
    expect(chip).not.toHaveTextContent("%");
  });

  it("shows the percent instead of the phase when the running job reports both", () => {
    const activeJob = activeJobView([
      buildJob({ job_type: "diarize", progress: 0.42, phase: "segmenting speech" }),
    ]);

    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={false}
        onToggleSettings={() => {}}
        activeJob={activeJob}
        onShowRecordings={() => {}}
      />,
    );

    const chip = screen.getByRole("button", {
      name: /Identifying speakers in “Weekly sync”/,
    });
    expect(chip).toHaveTextContent("· 42%");
    expect(chip).not.toHaveTextContent("segmenting speech");
  });

  it("shows neither percent nor phase while the job is still queued", () => {
    const activeJob = activeJobView([
      buildJob({ state: "queued", progress: 0, phase: "rendering PDF" }),
    ]);

    render(
      <AppHeader
        serviceStatus={readyStatus}
        modelStatus={null}
        settingsOpen={false}
        onToggleSettings={() => {}}
        activeJob={activeJob}
        onShowRecordings={() => {}}
      />,
    );

    const chip = screen.getByRole("button", { name: /Exporting PDF “Weekly sync”/ });
    expect(chip).not.toHaveTextContent("rendering PDF");
    expect(chip).not.toHaveTextContent("%");
  });
});
