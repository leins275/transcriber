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
  return {
    active: "qwen3.5-9b",
    gpu_build_present: null,
    models: [
      llmModel(),
      llmModel({
        id: "qwen3.6-35b-a3b",
        label: "Qwen3.6 35B A3B",
        file: "Qwen3.6-35B-A3B-Q4_K_M.gguf",
        size_bytes: 20_419_565_568,
        present: false,
        active: false,
      }),
    ],
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
    onDeleteLlmModel: () => {},
    onSelectLlmModel: () => {},
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
    expect(
      screen.getByText(/Summaries and action items run on this machine\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/project reports/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/facts/i)).not.toBeInTheDocument();
  });

  it("lists every curated model with its size, and badges the active one", () => {
    renderPage({ llmModels: llmCatalog() });
    expect(screen.getByText("Qwen3.5 9B")).toBeInTheDocument();
    expect(screen.getByText("Qwen3.6 35B A3B")).toBeInTheDocument();
    expect(screen.getByText("~6.6 GB")).toBeInTheDocument();
    expect(screen.getByText("~20.4 GB")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("offers Download for an absent model and passes its id through", async () => {
    const user = userEvent.setup();
    const onStartLlmModelDownload = vi.fn();
    renderPage({ llmModels: llmCatalog(), onStartLlmModelDownload });

    await user.click(screen.getByRole("button", { name: /download \(~20\.4 GB\)/i }));
    expect(onStartLlmModelDownload).toHaveBeenCalledWith("qwen3.6-35b-a3b");
  });

  it("offers Use this model and Delete on a present non-active model", async () => {
    const user = userEvent.setup();
    const onSelectLlmModel = vi.fn();
    const onDeleteLlmModel = vi.fn();
    const catalog = llmCatalog();
    catalog.models[1].present = true;
    renderPage({ llmModels: catalog, onSelectLlmModel, onDeleteLlmModel });

    await user.click(screen.getByRole("button", { name: /use this model/i }));
    expect(onSelectLlmModel).toHaveBeenCalledWith("qwen3.6-35b-a3b");

    await user.click(screen.getByRole("button", { name: /delete/i }));
    expect(onDeleteLlmModel).toHaveBeenCalledWith("qwen3.6-35b-a3b");
  });

  it("shows progress and Cancel while a model downloads, and no second Download", async () => {
    const user = userEvent.setup();
    const onCancelLlmModelDownload = vi.fn();
    const catalog = llmCatalog();
    catalog.models[1].download = {
      state: "downloading",
      downloaded_bytes: 5_000_000_000,
      total_bytes: 20_000_000_000,
      percent: 25,
      error_kind: null,
      error_message: null,
    };
    renderPage({ llmModels: catalog, onCancelLlmModelDownload });

    expect(screen.getByText(/Downloading · 25%/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancelLlmModelDownload).toHaveBeenCalledWith("qwen3.6-35b-a3b");
  });

  it("offers GPU acceleration only on the active row when the GPU build is missing", () => {
    const catalog = llmCatalog({ gpu_build_present: false });
    catalog.models[1].present = true;
    renderPage({ llmModels: catalog });

    const buttons = screen.getAllByRole("button", { name: /enable gpu acceleration/i });
    expect(buttons).toHaveLength(1);
    expect(screen.getByText(/Summaries currently run on CPU/)).toBeInTheDocument();
  });

  it("surfaces a failed download's message on its row", () => {
    const catalog = llmCatalog();
    catalog.models[1].download = {
      state: "error",
      downloaded_bytes: 0,
      total_bytes: 0,
      percent: 0,
      error_kind: "checksum_mismatch",
      error_message: "digest mismatch for Qwen3.6-35B-A3B-Q4_K_M.gguf",
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
});
