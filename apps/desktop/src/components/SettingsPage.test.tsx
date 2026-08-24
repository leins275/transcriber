import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsPage } from "./SettingsPage";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { LlmModelDownloadStatus, ServiceStatusView, SettingsView } from "../types";

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\MeetingVault",
    meetings_root_exists: true,
    service_base_url: null,
    supported_extensions: [".mp4", ".wav"],
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

function llmStatus(overrides: Partial<LlmModelDownloadStatus> = {}): LlmModelDownloadStatus {
  return {
    state: "complete",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 100,
    error_kind: null,
    error_message: null,
    model_present: true,
    gpu_build_present: null,
    ...overrides,
  };
}

const readyStatus: ServiceStatusView = {
  state: "ready",
  base_url: "http://127.0.0.1:8734",
  detail: null,
};

function renderPage(overrides: Partial<ComponentProps<typeof SettingsPage>> = {}) {
  const props = {
    settings: buildSettings(),
    serviceStatus: readyStatus,
    modelStatus: modelStatus(),
    llmModelStatus: null,
    appVersion: "0.3.0" as string | null,
    onBack: () => {},
    onChangeRoot: () => {},
    onStartLlmDownload: () => {},
    onCancelLlmDownload: () => {},
    ...overrides,
  };
  return render(<SettingsPage {...props} />);
}

describe("SettingsPage", () => {
  it("shows the vault path, model state, service line, formats and version", () => {
    renderPage();
    expect(screen.getByText("D:\\MeetingVault")).toBeInTheDocument();
    expect(screen.getByText("large-v3 installed")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("http://127.0.0.1:8734")).toBeInTheDocument();
    expect(screen.getByText("mp4 · wav")).toBeInTheDocument();
    expect(screen.getByText("Transcriber v0.3.0")).toBeInTheDocument();
  });

  it("fires onChangeRoot from Change… and onBack from the back link", async () => {
    const user = userEvent.setup();
    const onChangeRoot = vi.fn();
    const onBack = vi.fn();
    renderPage({ onChangeRoot, onBack });

    await user.click(screen.getByRole("button", { name: /change/i }));
    expect(onChangeRoot).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /recordings/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("warns when the configured vault folder no longer exists", () => {
    renderPage({ settings: buildSettings({ meetings_root_exists: false }) });
    expect(screen.getByText(/no longer exists/i)).toBeInTheDocument();
  });

  it("shows '(not set)' before a vault is chosen and omits the version row while unknown", () => {
    renderPage({
      settings: buildSettings({ meetings_root: null, meetings_root_exists: false }),
      appVersion: null,
    });
    expect(screen.getByText("(not set)")).toBeInTheDocument();
    expect(screen.queryByText(/^Transcriber v/)).not.toBeInTheDocument();
    expect(screen.queryByText(/no longer exists/i)).not.toBeInTheDocument();
  });

  // FR-7: the project-report job is gone, so no copy may promise one.
  it("describes the installed assistant as summaries, action items and facts — not project reports", () => {
    renderPage({ llmModelStatus: llmStatus() });
    expect(
      screen.getByText(/Summaries, action items and facts run on this machine\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/project reports/i)).not.toBeInTheDocument();
  });

  it("describes what the missing assistant is needed for without promising project reports", () => {
    renderPage({ llmModelStatus: llmStatus({ state: "idle", model_present: false, percent: 0 }) });
    expect(screen.getByText(/Needed for summaries, action items and facts\./)).toBeInTheDocument();
    expect(screen.queryByText(/project reports/i)).not.toBeInTheDocument();
  });

  it("says filing still works when the service is unavailable", () => {
    renderPage({
      serviceStatus: { state: "unavailable", base_url: null, detail: "spawn failed" },
    });
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/filing still works/i)).toBeInTheDocument();
  });
});
