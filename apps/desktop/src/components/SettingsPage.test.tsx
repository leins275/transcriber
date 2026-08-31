import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsPage } from "./SettingsPage";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type { LlmCatalogModel, LlmModelsView, ServiceStatusView, SettingsView } from "../types";

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

function llmModel(overrides: Partial<LlmCatalogModel> = {}): LlmCatalogModel {
  return {
    id: "qwen3.5-9b",
    label: "Qwen3.5 9B",
    file: "Qwen3.5-9B-Q5_K_M.gguf",
    size_bytes: 6_577_841_376,
    catalog: true,
    present: true,
    active: true,
    download: {
      state: "idle",
      downloaded_bytes: 0,
      total_bytes: 0,
      percent: 0,
      error_kind: null,
      error_message: null,
    },
    ...overrides,
  };
}

function llmCatalog(overrides: Partial<LlmModelsView> = {}): LlmModelsView {
  // Deliberately a single model: there is no switching.
  return {
    active: "qwen3.5-9b",
    gpu_build_present: null,
    models: [llmModel()],
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
    llmModels: null as LlmModelsView | null,
    appVersion: "0.3.0" as string | null,
    onBack: () => {},
    onChangeRoot: () => {},
    onStartLlmModelDownload: () => {},
    onCancelLlmModelDownload: () => {},
    onReindex: () => Promise.resolve(),
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

  // FR-7: the project-report job is gone, so no copy may promise one; the
  // facts extraction followed it (the summary carries the notable facts).
  it("describes the assistant as summaries and action items — not reports or facts", () => {
    renderPage({ llmModels: llmCatalog() });
    expect(screen.getByText(/Summaries and action items run on this machine/)).toBeInTheDocument();
    expect(screen.queryByText(/project reports/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/facts/i)).not.toBeInTheDocument();
  });

  it("lists the one built-in model with its size, and no switching controls", () => {
    renderPage({ llmModels: llmCatalog() });
    expect(screen.getByText("Qwen3.5 9B")).toBeInTheDocument();
    expect(screen.getByText("~6.6 GB")).toBeInTheDocument();
    // Rigid on purpose: one model, so nothing to badge, select or delete.
    expect(screen.queryByText("Active")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /use this model/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
  });

  it("offers Download when the model is absent and passes its id through", async () => {
    const user = userEvent.setup();
    const onStartLlmModelDownload = vi.fn();
    const catalog = llmCatalog();
    catalog.models[0].present = false;
    renderPage({ llmModels: catalog, onStartLlmModelDownload });

    await user.click(screen.getByRole("button", { name: /download \(~6\.6 GB\)/i }));
    expect(onStartLlmModelDownload).toHaveBeenCalledWith("qwen3.5-9b");
  });

  it("shows progress and Cancel while the model downloads", async () => {
    const user = userEvent.setup();
    const onCancelLlmModelDownload = vi.fn();
    const catalog = llmCatalog();
    catalog.models[0].present = false;
    catalog.models[0].download = {
      state: "downloading",
      downloaded_bytes: 1_650_000_000,
      total_bytes: 6_600_000_000,
      percent: 25,
      error_kind: null,
      error_message: null,
    };
    renderPage({ llmModels: catalog, onCancelLlmModelDownload });

    expect(screen.getByText(/Downloading · 25%/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download \(/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancelLlmModelDownload).toHaveBeenCalledWith("qwen3.5-9b");
  });

  it("offers GPU acceleration when the GPU build is missing", () => {
    const catalog = llmCatalog({ gpu_build_present: false });
    renderPage({ llmModels: catalog });

    const buttons = screen.getAllByRole("button", { name: /enable gpu acceleration/i });
    expect(buttons).toHaveLength(1);
    expect(screen.getByText(/Summaries currently run on CPU/)).toBeInTheDocument();
  });

  it("surfaces a failed download's message on its row", () => {
    const catalog = llmCatalog();
    catalog.models[0].present = false;
    catalog.models[0].download = {
      state: "error",
      downloaded_bytes: 0,
      total_bytes: 0,
      percent: 0,
      error_kind: "checksum_mismatch",
      error_message: "digest mismatch for Qwen3.5-9B-Q5_K_M.gguf",
    };
    renderPage({ llmModels: catalog });
    expect(screen.getByText(/digest mismatch/)).toBeInTheDocument();
  });

  it("says filing still works when the service is unavailable", () => {
    renderPage({
      serviceStatus: { state: "unavailable", base_url: null, detail: "spawn failed" },
    });
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/filing still works/i)).toBeInTheDocument();
  });

  it("queues a search-index rebuild and confirms it", async () => {
    const onReindex = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ onReindex });

    await user.click(screen.getByRole("button", { name: /rebuild search index/i }));

    expect(onReindex).toHaveBeenCalled();
    expect(await screen.findByText(/queued/i)).toBeInTheDocument();
  });

  it("surfaces a refused reindex instead of a silent no-op", async () => {
    const onReindex = vi.fn().mockRejectedValue({ kind: "service", message: "service is down" });
    const user = userEvent.setup();
    renderPage({ onReindex });

    await user.click(screen.getByRole("button", { name: /rebuild search index/i }));

    expect(await screen.findByText(/service is down/i)).toBeInTheDocument();
  });

  it("disables the rebuild button while the service is not ready", () => {
    renderPage({
      serviceStatus: { state: "unavailable", base_url: null, detail: "spawn failed" },
    });

    expect(screen.getByRole("button", { name: /rebuild search index/i })).toBeDisabled();
  });
});
