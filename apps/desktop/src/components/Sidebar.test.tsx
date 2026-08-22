import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Sidebar } from "./Sidebar";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { ServiceStatusView, SettingsView } from "../types";

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\MeetingVault",
    meetings_root_exists: true,
    service_base_url: null,
    supported_extensions: ["mp4", "mkv", "mov", "webm", "avi", "m4a", "mp3", "wav", "flac", "ogg"],
    config_error: null,
    default_meetings_root: null,
    ...overrides,
  };
}

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

const readyStatus: ServiceStatusView = { state: "ready", base_url: null, detail: null };

describe("Sidebar", () => {
  it("in the setup variant, shows only the brand strapline and accepts list", () => {
    render(
      <Sidebar
        variant="setup"
        settings={buildSettings()}
        serviceStatus={readyStatus}
        modelStatus={null}
        onChangeRoot={() => {}}
      />,
    );
    expect(screen.getByText("Transcriber")).toBeInTheDocument();
    expect(screen.getByText(/filed and transcribed entirely on this machine/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /settings/i })).not.toBeInTheDocument();
    expect(screen.getByText(/mp4 mkv mov/i)).toBeInTheDocument();
  });

  it("in the full variant, shows the Vault region, model line, and Ready · GPU", () => {
    render(
      <Sidebar
        variant="full"
        settings={buildSettings()}
        serviceStatus={readyStatus}
        modelStatus={modelStatus({ cuda_runtime_present: true })}
        onChangeRoot={() => {}}
      />,
    );
    expect(screen.getByRole("region", { name: /settings/i })).toBeInTheDocument();
    expect(screen.getByText("D:\\MeetingVault")).toBeInTheDocument();
    expect(screen.getByText(/large-v3 installed/i)).toBeInTheDocument();
    expect(screen.getByText(/Ready . GPU/i)).toBeInTheDocument();
  });

  it("shows Ready · CPU when the cuda runtime is absent on a GPU-capable host", () => {
    render(
      <Sidebar
        variant="full"
        settings={buildSettings()}
        serviceStatus={readyStatus}
        modelStatus={modelStatus({ cuda_runtime_present: false })}
        onChangeRoot={() => {}}
      />,
    );
    expect(screen.getByText(/Ready . CPU/i)).toBeInTheDocument();
  });

  it("shows Unavailable with a reassuring note when the service is down", () => {
    render(
      <Sidebar
        variant="full"
        settings={buildSettings()}
        serviceStatus={{ state: "unavailable", base_url: null, detail: null }}
        modelStatus={modelStatus()}
        onChangeRoot={() => {}}
      />,
    );
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/filing still works/i)).toBeInTheDocument();
  });
});
