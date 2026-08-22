import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelDownloadStep, type ModelDownloadCommands } from "./ModelDownloadStep";
import type { ModelDownloadStatus } from "../lib/modelDownload";

function status(overrides: Partial<ModelDownloadStatus> = {}): ModelDownloadStatus {
  return {
    state: "idle",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 0,
    error_kind: null,
    error_message: null,
    model_present: false,
    cuda_warning: null,
    cuda_runtime_present: null,
    ...overrides,
  };
}

/** A fake command layer (per plan.md) -- no `@tauri-apps/api` anywhere in
 * this test, matching the rest of this app's T6 components. */
function fakeCommands(overrides: Partial<ModelDownloadCommands> = {}): ModelDownloadCommands {
  return {
    start: vi.fn(() => Promise.resolve(status({ state: "downloading" }))),
    cancel: vi.fn(() => Promise.resolve(status({ state: "cancelled" }))),
    status: vi.fn(() => Promise.resolve(status({ state: "downloading" }))),
    ...overrides,
  };
}

describe("ModelDownloadStep", () => {
  it("states in plain language that the model is missing (FR-17)", () => {
    render(
      <ModelDownloadStep commands={fakeCommands()} initialStatus={status()} pollIntervalMs={10} />,
    );
    expect(screen.getByText(/model is missing/i)).toBeInTheDocument();
  });

  it("with the model already present the step is skipped entirely (FR-16)", () => {
    const { container } = render(
      <ModelDownloadStep
        commands={fakeCommands()}
        initialStatus={status({ state: "complete", model_present: true })}
        pollIntervalMs={10}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("with the model present but a cuda_warning set, shows a persistent notice with a GPU-setup retry (E13)", async () => {
    const commands = fakeCommands({
      start: vi.fn(() => Promise.resolve(status({ state: "downloading", model_present: true }))),
    });
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={commands}
        initialStatus={status({
          state: "complete",
          model_present: true,
          cuda_warning: "digest mismatch for nvidia_cublas_cu12.whl",
        })}
        pollIntervalMs={10}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("digest mismatch for nvidia_cublas_cu12.whl");

    await user.click(screen.getByRole("button", { name: /retry gpu setup/i }));
    expect(commands.start).toHaveBeenCalledTimes(1);
  });

  it("begins polling even when the start response itself reports idle (E16 -- production race)", async () => {
    let statusCalls = 0;
    const commands = fakeCommands({
      // Mirrors the real `SetupDownload`/GPU-path race: the POST resolves
      // before the worker thread flips `state` off `idle`.
      start: vi.fn(() => Promise.resolve(status({ state: "idle" }))),
      status: vi.fn(() => {
        statusCalls += 1;
        return Promise.resolve(
          status({ state: "downloading", downloaded_bytes: 512, total_bytes: 1024, percent: 50 }),
        );
      }),
    });
    const user = userEvent.setup();
    render(<ModelDownloadStep commands={commands} initialStatus={status()} pollIntervalMs={10} />);

    await user.click(screen.getByRole("button", { name: /start download/i }));

    await waitFor(() => expect(statusCalls).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText(/512 B \/ 1\.0 KB \(50%\)/)).toBeInTheDocument());
  });

  it("with the model present and cuda_runtime_present false but no in-session cuda_warning, still shows the persistent GPU notice (E13 durable)", () => {
    // A fresh sidecar process (no `SetupDownload` instance) never carries an
    // in-session `cuda_warning`, but `/health.cuda_runtime_present` still
    // durably reports the runtime missing.
    render(
      <ModelDownloadStep
        commands={fakeCommands()}
        initialStatus={status({
          state: "complete",
          model_present: true,
          cuda_warning: null,
          cuda_runtime_present: false,
        })}
        pollIntervalMs={10}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/GPU acceleration is not installed/i);
    expect(screen.getByRole("button", { name: /retry gpu setup/i })).toBeInTheDocument();
  });

  it("with the model present and cuda_runtime_present null (no GPU on this host), renders nothing", () => {
    const { container } = render(
      <ModelDownloadStep
        commands={fakeCommands()}
        initialStatus={status({
          state: "complete",
          model_present: true,
          cuda_warning: null,
          cuda_runtime_present: null,
        })}
        pollIntervalMs={10}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("Retry GPU setup shows progress while the retry runs, then re-renders the warning on a second failure (E17)", async () => {
    let pollCount = 0;
    const commands = fakeCommands({
      start: vi.fn(() =>
        Promise.resolve(
          status({
            state: "downloading",
            model_present: true,
            cuda_warning: null,
            cuda_runtime_present: false,
            downloaded_bytes: 0,
            total_bytes: 1000,
          }),
        ),
      ),
      status: vi.fn(() => {
        pollCount += 1;
        if (pollCount < 2) {
          return Promise.resolve(
            status({
              state: "downloading",
              model_present: true,
              cuda_warning: null,
              cuda_runtime_present: false,
              downloaded_bytes: 500,
              total_bytes: 1000,
              percent: 50,
            }),
          );
        }
        return Promise.resolve(
          status({
            state: "error",
            model_present: true,
            cuda_warning: "digest mismatch for nvidia_cublas_cu12.whl (retry)",
            cuda_runtime_present: false,
          }),
        );
      }),
    });
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={commands}
        initialStatus={status({
          state: "complete",
          model_present: true,
          cuda_warning: "digest mismatch for nvidia_cublas_cu12.whl",
          cuda_runtime_present: false,
        })}
        pollIntervalMs={10}
      />,
    );

    await user.click(screen.getByRole("button", { name: /retry gpu setup/i }));

    // The notice must not go blank while the retry is in flight.
    await waitFor(() => expect(screen.getByText(/downloading gpu runtime/i)).toBeInTheDocument());

    await waitFor(() =>
      expect(
        screen.getByText(/digest mismatch for nvidia_cublas_cu12\.whl \(retry\)/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /retry gpu setup/i })).toBeInTheDocument();
  });

  it("Retry GPU setup clears the notice once the runtime installs successfully (E17)", async () => {
    const commands = fakeCommands({
      start: vi.fn(() =>
        Promise.resolve(
          status({
            state: "downloading",
            model_present: true,
            cuda_warning: null,
            cuda_runtime_present: false,
          }),
        ),
      ),
      status: vi.fn(() =>
        Promise.resolve(
          status({
            state: "complete",
            model_present: true,
            cuda_warning: null,
            cuda_runtime_present: true,
          }),
        ),
      ),
    });
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={commands}
        initialStatus={status({
          state: "complete",
          model_present: true,
          cuda_warning: "digest mismatch for nvidia_cublas_cu12.whl",
          cuda_runtime_present: false,
        })}
        pollIntervalMs={10}
      />,
    );

    await user.click(screen.getByRole("button", { name: /retry gpu setup/i }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("starting a download renders bytes and percent, and updates as status polls arrive (FR-12)", async () => {
    let pollCount = 0;
    const commands = fakeCommands({
      start: vi.fn(() =>
        Promise.resolve(
          status({ state: "downloading", downloaded_bytes: 0, total_bytes: 1024, percent: 0 }),
        ),
      ),
      status: vi.fn(() => {
        pollCount += 1;
        return Promise.resolve(
          status({
            state: "downloading",
            downloaded_bytes: 512,
            total_bytes: 1024,
            percent: 50,
          }),
        );
      }),
    });
    const user = userEvent.setup();
    render(<ModelDownloadStep commands={commands} initialStatus={status()} pollIntervalMs={10} />);

    await user.click(screen.getByRole("button", { name: /start download/i }));

    await waitFor(() => expect(pollCount).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText(/512 B \/ 1\.0 KB \(50%\)/)).toBeInTheDocument());
  });

  it("cancel returns the step to an idle, retryable state and says so (FR-12)", async () => {
    const commands = fakeCommands({
      start: vi.fn(() => Promise.resolve(status({ state: "downloading", total_bytes: 1000 }))),
      cancel: vi.fn(() => Promise.resolve(status({ state: "cancelled" }))),
    });
    const user = userEvent.setup();
    render(<ModelDownloadStep commands={commands} initialStatus={status()} pollIntervalMs={10} />);

    await user.click(screen.getByRole("button", { name: /start download/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    await waitFor(() => expect(commands.cancel).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/cancelled/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("a failed download shows the service's error message verbatim plus a Retry control, and stays usable (FR-17)", async () => {
    const commands = fakeCommands({
      start: vi.fn(() => Promise.resolve(status({ state: "downloading", total_bytes: 1000 }))),
      status: vi.fn(() =>
        Promise.resolve(
          status({
            state: "error",
            error_kind: "checksum_mismatch",
            error_message: "digest mismatch for model.bin",
          }),
        ),
      ),
    });
    const user = userEvent.setup();
    render(<ModelDownloadStep commands={commands} initialStatus={status()} pollIntervalMs={10} />);

    await user.click(screen.getByRole("button", { name: /start download/i }));

    await waitFor(() =>
      expect(screen.getByText("digest mismatch for model.bin")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // The app remains usable: a "Skip for now" escape hatch is still offered
    // (FR-17: "a failed or skipped download never leaves a broken install").
    expect(screen.getByRole("button", { name: /skip for now/i })).toBeInTheDocument();
  });

  it("clicking Skip for now calls onSkip so the parent can unblock the app", async () => {
    const onSkip = vi.fn();
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={fakeCommands()}
        initialStatus={status()}
        onSkip={onSkip}
        pollIntervalMs={10}
      />,
    );

    await user.click(screen.getByRole("button", { name: /skip for now/i }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });

  it("in compact mode renders a persistent, non-modal missing-model notice exposing the same retry (FR-17)", async () => {
    const commands = fakeCommands();
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={commands}
        initialStatus={status()}
        compact
        pollIntervalMs={10}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/model/i);
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(commands.start).toHaveBeenCalledTimes(1);
  });

  it("calls onModelPresent once a poll reports the model present", async () => {
    const onModelPresent = vi.fn();
    const commands = fakeCommands({
      start: vi.fn(() => Promise.resolve(status({ state: "downloading", total_bytes: 1000 }))),
      status: vi.fn(() =>
        Promise.resolve(status({ state: "complete", percent: 100, model_present: true })),
      ),
    });
    const user = userEvent.setup();
    render(
      <ModelDownloadStep
        commands={commands}
        initialStatus={status()}
        onModelPresent={onModelPresent}
        pollIntervalMs={10}
      />,
    );

    await user.click(screen.getByRole("button", { name: /start download/i }));
    await waitFor(() => expect(onModelPresent).toHaveBeenCalledTimes(1));
  });
});
