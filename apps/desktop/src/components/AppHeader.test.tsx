import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppHeader } from "./AppHeader";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { ServiceStatusView } from "../types";

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
});
