import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsPage } from "./SettingsPage";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type {
  DiarizationDownloadStatus,
  DiarizationStatusView,
  EmbeddingModelDownloadStatus,
  LlmCatalogModel,
  LlmModelsView,
  ServiceStatusView,
  SettingsView,
} from "../types";

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\MeetingVault",
    meetings_root_exists: true,
    service_base_url: null,
    supported_extensions: [".mp4", ".wav"],
    config_error: null,
    default_meetings_root: null,
    diarize: false,
    hf_token_present: false,
    ...overrides,
  };
}

function diarizationStatus(overrides: Partial<DiarizationStatusView> = {}): DiarizationStatusView {
  return {
    runtime_present: false,
    model_present: false,
    token_present: false,
    enabled: false,
    gpu_present: true,
    runtime_total_bytes: 2_690_000_000,
    ...overrides,
  };
}

function diarizationDownload(
  overrides: Partial<DiarizationDownloadStatus> = {},
): DiarizationDownloadStatus {
  return {
    state: "idle",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 0,
    error_kind: null,
    error_message: null,
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

function embeddingStatus(
  overrides: Partial<EmbeddingModelDownloadStatus> = {},
): EmbeddingModelDownloadStatus {
  return {
    state: "idle",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 0,
    error_kind: null,
    error_message: null,
    model_present: true,
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
    embeddingStatus: null as EmbeddingModelDownloadStatus | null,
    onStartEmbeddingModelDownload: () => {},
    onCancelEmbeddingModelDownload: () => {},
    onReindex: () => Promise.resolve(),
    diarization: null as DiarizationStatusView | null,
    diarizationRuntimeDownload: null as DiarizationDownloadStatus | null,
    diarizationModelDownload: null as DiarizationDownloadStatus | null,
    onStartDiarizationRuntimeDownload: () => {},
    onCancelDiarizationRuntimeDownload: () => {},
    onStartDiarizationModelDownload: () => {},
    onCancelDiarizationModelDownload: () => {},
    onSaveHfToken: () => Promise.resolve(),
    onSetDiarizeEnabled: () => Promise.resolve(),
    onDiarizeLabelledMeetings: () => Promise.resolve(0),
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

  it("offers Enable vector search when the embedding model is absent", async () => {
    const user = userEvent.setup();
    const onStartEmbeddingModelDownload = vi.fn();
    renderPage({
      embeddingStatus: embeddingStatus({ model_present: false }),
      onStartEmbeddingModelDownload,
    });

    expect(screen.getByText(/search matches words only/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /enable vector search/i }));
    expect(onStartEmbeddingModelDownload).toHaveBeenCalledTimes(1);
  });

  it("shows progress and Cancel while the embedding model downloads", async () => {
    const user = userEvent.setup();
    const onCancelEmbeddingModelDownload = vi.fn();
    renderPage({
      embeddingStatus: embeddingStatus({
        state: "downloading",
        model_present: false,
        percent: 40,
      }),
      onCancelEmbeddingModelDownload,
    });

    expect(screen.getByText(/Downloading · 40%/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /enable vector search/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancelEmbeddingModelDownload).toHaveBeenCalledTimes(1);
  });

  it("shows the installed embedding model without a download button", () => {
    renderPage({ embeddingStatus: embeddingStatus({ model_present: true }) });

    expect(screen.getByText("Vector search (BGE-M3)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /enable vector search/i })).not.toBeInTheDocument();
  });

  it("surfaces a failed embedding download's message", () => {
    renderPage({
      embeddingStatus: embeddingStatus({
        state: "error",
        model_present: false,
        error_kind: "network",
        error_message: "connection reset while fetching bge-m3-Q8_0.gguf",
      }),
    });

    expect(screen.getByText(/connection reset/)).toBeInTheDocument();
  });
});

describe("SettingsPage speakers row", () => {
  it("offers the runtime fetch, sized, when speaker identification is not set up", async () => {
    const user = userEvent.setup();
    const onStartDiarizationRuntimeDownload = vi.fn();
    renderPage({ diarization: diarizationStatus(), onStartDiarizationRuntimeDownload });

    await user.click(
      screen.getByRole("button", { name: /enable speaker identification \(~2\.7 GB\)/i }),
    );
    expect(onStartDiarizationRuntimeDownload).toHaveBeenCalledTimes(1);
    // The later steps wait on their prerequisites.
    expect(screen.getByRole("button", { name: /download speaker models/i })).toBeDisabled();
    expect(
      screen.getByRole("checkbox", { name: /identify speakers in new recordings/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /identify speakers in labelled meetings/i }),
    ).toBeDisabled();
  });

  it("does not offer the feature on a machine without an NVIDIA GPU", () => {
    renderPage({ diarization: diarizationStatus({ gpu_present: false }) });

    expect(screen.getByText(/needs an nvidia gpu/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /enable speaker identification/i }),
    ).not.toBeInTheDocument();
  });

  it("shows progress and Cancel while the runtime downloads", async () => {
    const user = userEvent.setup();
    const onCancelDiarizationRuntimeDownload = vi.fn();
    renderPage({
      diarization: diarizationStatus(),
      diarizationRuntimeDownload: diarizationDownload({ state: "downloading", percent: 12 }),
      onCancelDiarizationRuntimeDownload,
    });

    expect(screen.getByText(/Downloading · 12%/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancelDiarizationRuntimeDownload).toHaveBeenCalledTimes(1);
  });

  it("saves a pasted Hugging Face token and then enables the model fetch", async () => {
    const user = userEvent.setup();
    const onSaveHfToken = vi.fn().mockResolvedValue(undefined);
    const onStartDiarizationModelDownload = vi.fn();
    const { rerender } = renderPage({
      diarization: diarizationStatus({ runtime_present: true }),
      onSaveHfToken,
      onStartDiarizationModelDownload,
    });

    expect(screen.getByRole("button", { name: /save token/i })).toBeDisabled();
    await user.type(screen.getByLabelText(/hugging face token/i), "  hf_abc123 ");
    await user.click(screen.getByRole("button", { name: /save token/i }));
    expect(onSaveHfToken).toHaveBeenCalledWith("hf_abc123");
    expect(await screen.findByText("Saved")).toBeInTheDocument();

    // App re-reads the status once the service is back with the token.
    rerender(
      <SettingsPage
        {...{
          settings: buildSettings(),
          serviceStatus: readyStatus,
          modelStatus: modelStatus(),
          llmModels: null,
          appVersion: null,
          onBack: () => {},
          onChangeRoot: () => {},
          onStartLlmModelDownload: () => {},
          onCancelLlmModelDownload: () => {},
          embeddingStatus: null,
          onStartEmbeddingModelDownload: () => {},
          onCancelEmbeddingModelDownload: () => {},
          onReindex: () => Promise.resolve(),
          diarization: diarizationStatus({ runtime_present: true, token_present: true }),
          diarizationRuntimeDownload: null,
          diarizationModelDownload: null,
          onStartDiarizationRuntimeDownload: () => {},
          onCancelDiarizationRuntimeDownload: () => {},
          onStartDiarizationModelDownload,
          onCancelDiarizationModelDownload: () => {},
          onSaveHfToken,
          onSetDiarizeEnabled: () => Promise.resolve(),
          onDiarizeLabelledMeetings: () => Promise.resolve(0),
        }}
      />,
    );
    await user.click(screen.getByRole("button", { name: /download speaker models/i }));
    expect(onStartDiarizationModelDownload).toHaveBeenCalledTimes(1);
  });

  it("surfaces a gated-model refusal verbatim", () => {
    renderPage({
      diarization: diarizationStatus({ runtime_present: true, token_present: true }),
      diarizationModelDownload: diarizationDownload({
        state: "error",
        error_kind: "model_load",
        error_message:
          "pyannote/segmentation-3.0 is gated on Hugging Face: accept its terms at https://huggingface.co/pyannote/segmentation-3.0",
      }),
    });

    expect(screen.getByText(/accept its terms at/)).toBeInTheDocument();
  });

  it("flips the switch and queues the backfill once everything is in place", async () => {
    const user = userEvent.setup();
    const onSetDiarizeEnabled = vi.fn().mockResolvedValue(undefined);
    const onDiarizeLabelledMeetings = vi.fn().mockResolvedValue(3);
    renderPage({
      settings: buildSettings({ hf_token_present: true }),
      diarization: diarizationStatus({
        runtime_present: true,
        model_present: true,
        token_present: true,
      }),
      onSetDiarizeEnabled,
      onDiarizeLabelledMeetings,
    });

    await user.click(
      screen.getByRole("checkbox", { name: /identify speakers in new recordings/i }),
    );
    expect(onSetDiarizeEnabled).toHaveBeenCalledWith(true);

    await user.click(
      screen.getByRole("button", { name: /identify speakers in labelled meetings/i }),
    );
    expect(onDiarizeLabelledMeetings).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Queued 3 meetings/)).toBeInTheDocument();
  });

  it("says so when the backfill finds nothing to do", async () => {
    const user = userEvent.setup();
    renderPage({
      diarization: diarizationStatus({
        runtime_present: true,
        model_present: true,
        token_present: true,
      }),
      onDiarizeLabelledMeetings: () => Promise.resolve(0),
    });

    await user.click(
      screen.getByRole("button", { name: /identify speakers in labelled meetings/i }),
    );
    expect(await screen.findByText(/nothing to do/i)).toBeInTheDocument();
  });
});

describe("SettingsPage token walkthrough", () => {
  it("spells out the Hugging Face steps with copyable links, since the webview cannot open a browser", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage({ diarization: diarizationStatus({ runtime_present: true }) });

    const steps = screen.getByRole("list", { name: /token setup steps/i });
    expect(steps).toHaveTextContent(/agree and access repository/i);
    expect(steps).toHaveTextContent(/create new token/i);
    expect(
      screen.getByText("https://huggingface.co/pyannote/segmentation-3.0"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Copy https://huggingface.co/settings/tokens" }),
    );
    expect(writeText).toHaveBeenCalledWith("https://huggingface.co/settings/tokens");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });
});
