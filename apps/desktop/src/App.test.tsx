import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import App from "./App";
import type { AppError, JobSnapshot, SettingsView } from "./types";

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\Meetings",
    meetings_root_exists: true,
    service_base_url: null,
    supported_extensions: [".mp4", ".wav"],
    config_error: null,
    default_meetings_root: null,
    ...overrides,
  };
}

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\x\\good.mp4",
    file_name: "good.mp4",
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

const notConfigured: AppError = { kind: "not_configured", message: "no meetings root configured" };

function flush(ms = 20) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Unmounts and flushes pending microtasks before the test returns. Tauri's
 * `onDragDropEvent`/`listen` teardown is asynchronous under a synchronous
 * public API; unmounting here (rather than relying on the global RTL
 * `afterEach`) guarantees that teardown settles while the mocked IPC
 * internals are still installed, instead of racing this file's
 * `clearMocks()`.
 */
async function settle() {
  cleanup();
  await flush(30);
}

beforeEach(() => {
  mockWindows("main");
});

afterEach(() => {
  clearMocks();
});

describe("App first-run", () => {
  it("renders the first-run picker when meetings_root is null and refuses drops", async () => {
    const enqueueCalls: unknown[] = [];
    mockIPC(
      (cmd, payload) => {
        if (cmd === "get_settings")
          return buildSettings({ meetings_root: null, meetings_root_exists: false });
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "enqueue_paths") {
          enqueueCalls.push(payload);
          throw notConfigured;
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);

    await waitFor(() =>
      expect(screen.getByText(/choose where meetings live/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("region", { name: /drop/i })).not.toBeInTheDocument();

    await flush();
    await emit("tauri://drag-drop", { paths: ["C:\\x\\file.mp4"], position: { x: 0, y: 0 } });
    await flush();

    expect(enqueueCalls).toHaveLength(0);
    await settle();
  });
});

describe("App drag-drop wiring", () => {
  it("tracks hovering/idle from over and leave events, and invokes enqueue_paths with the exact dropped path", async () => {
    const enqueueCalls: unknown[] = [];
    mockIPC(
      (cmd, payload) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "enqueue_paths") {
          enqueueCalls.push(payload);
          return [buildJob({ source_path: (payload as { paths: string[] }).paths[0] })];
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);
    const dropRegion = () => screen.getByRole("region", { name: /drop/i });
    await waitFor(() => expect(dropRegion()).toHaveAttribute("data-state", "idle"));

    await waitFor(async () => {
      await emit("tauri://drag-over", { position: { x: 1, y: 1 } });
      expect(dropRegion()).toHaveAttribute("data-state", "hovering");
    });

    await emit("tauri://drag-leave");
    await waitFor(() => expect(dropRegion()).toHaveAttribute("data-state", "idle"));

    const droppedPath = "C:\\x\\ELS - 260812 - Security issue.mp4";
    await emit("tauri://drag-drop", { paths: [droppedPath], position: { x: 2, y: 2 } });

    await waitFor(() => expect(enqueueCalls).toContainEqual({ paths: [droppedPath] }));
    await settle();
  });

  it("renders one accepted and one rejected row from a mixed drop", async () => {
    mockIPC(
      (cmd, payload) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "enqueue_paths") {
          const paths = (payload as { paths: string[] }).paths;
          return [
            buildJob({
              id: "good",
              source_path: paths[0],
              file_name: "good.mp4",
              state: "pending",
            }),
            buildJob({
              id: "bad",
              source_path: paths[1],
              file_name: "bad.txt",
              state: "rejected",
              error_kind: "unsupported_extension",
              message: "bad.txt: unsupported extension",
            }),
          ];
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);
    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());

    await emit("tauri://drag-drop", {
      paths: ["C:\\x\\good.mp4", "C:\\x\\bad.txt"],
      position: { x: 0, y: 0 },
    });

    await waitFor(() => expect(screen.getByText("good.mp4")).toBeInTheDocument());
    expect(screen.getByText("bad.txt")).toBeInTheDocument();
    expect(screen.getByText(/unsupported extension/i)).toBeInTheDocument();
    await settle();
  });
});

describe("App choose-file fallback", () => {
  it("reaches the same enqueue_paths call as dropping the same path", async () => {
    const enqueueCalls: unknown[] = [];
    const chosenPath = "C:\\x\\chosen.mp4";
    mockIPC((cmd, payload) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "plugin:dialog|open") return chosenPath;
      if (cmd === "enqueue_paths") {
        enqueueCalls.push(payload);
        return [buildJob({ source_path: chosenPath, file_name: "chosen.mp4" })];
      }
      return null;
    });

    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /choose file/i }));

    await waitFor(() => expect(enqueueCalls).toContainEqual({ paths: [chosenPath] }));
    await settle();
  });
});

describe("App reveal", () => {
  it("invokes reveal_job with the job id, never with a path string", async () => {
    const revealCalls: unknown[] = [];
    mockIPC(
      (cmd, payload) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "reveal_job") {
          revealCalls.push(payload);
          return null;
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());

    await emit(
      "jobs://updated",
      buildJob({
        id: "job-done",
        state: "done",
        transcript_path: "D:\\Meetings\\ELS\\260812\\transcript.json",
      }),
    );

    const revealButton = await screen.findByRole("button", { name: /reveal/i });
    await user.click(revealButton);

    expect(revealCalls).toEqual([{ jobId: "job-done" }]);
    await settle();
  });
});

describe("App vault browser", () => {
  function buildVaultEntry(overrides: Partial<Record<string, unknown>> = {}) {
    return {
      id: "v-1",
      project: "ELS",
      meeting_name: "260812 - Security issue",
      meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
      has_source: true,
      has_transcript: true,
      ...overrides,
    };
  }

  it("loads the vault listing once settings resolve with an existing meetings root", async () => {
    mockIPC(
      (cmd) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "list_vault") return [buildVaultEntry()];
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);

    await waitFor(() => expect(screen.getByRole("region", { name: /vault/i })).toBeInTheDocument());
    expect(screen.getByText("260812 - Security issue")).toBeInTheDocument();
    await settle();
  });

  it("does not fetch the vault listing before a meetings root is configured (first-run)", async () => {
    const listVaultCalls: unknown[] = [];
    mockIPC(
      (cmd) => {
        if (cmd === "get_settings")
          return buildSettings({ meetings_root: null, meetings_root_exists: false });
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "list_vault") {
          listVaultCalls.push(cmd);
          return [];
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);

    await waitFor(() =>
      expect(screen.getByText(/choose where meetings live/i)).toBeInTheDocument(),
    );
    await flush();
    expect(listVaultCalls).toHaveLength(0);
    await settle();
  });

  it("refetches the vault listing after a job reaches a terminal filed state", async () => {
    let listVaultCallCount = 0;
    mockIPC(
      (cmd) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "list_vault") {
          listVaultCallCount += 1;
          return listVaultCallCount === 1 ? [] : [buildVaultEntry()];
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    render(<App />);
    await waitFor(() => expect(listVaultCallCount).toBeGreaterThanOrEqual(1));
    // The Vault panel itself always mounts (its Service log tab has to stay
    // reachable with an empty vault) -- what the first listing lacks is the
    // meeting.
    expect(screen.queryByText("260812 - Security issue")).not.toBeInTheDocument();

    await emit(
      "jobs://updated",
      buildJob({
        id: "job-done",
        state: "done",
        meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
        transcript_path: "D:\\Meetings\\ELS\\260812 - Security issue\\transcript.json",
      }),
    );

    await waitFor(() => expect(screen.getByText("260812 - Security issue")).toBeInTheDocument());
    await settle();
  });

  it("invokes reveal_vault_entry with the entry id when a vault row's Reveal is clicked", async () => {
    const revealCalls: unknown[] = [];
    mockIPC(
      (cmd, payload) => {
        if (cmd === "get_settings") return buildSettings();
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "list_vault") return [buildVaultEntry({ id: "v-9" })];
        if (cmd === "reveal_vault_entry") {
          revealCalls.push(payload);
          return null;
        }
        return null;
      },
      { shouldMockEvents: true },
    );

    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(screen.getByRole("region", { name: /vault/i })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /reveal/i }));

    expect(revealCalls).toEqual([{ entryId: "v-9" }]);
    await settle();
  });
});

describe("App settings", () => {
  it("persists the meetings-root via set_meetings_root and re-renders the new root", async () => {
    const newRoot = "E:\\NewRoot";
    mockIPC((cmd, payload) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "plugin:dialog|open") return newRoot;
      if (cmd === "set_meetings_root") {
        expect(payload).toEqual({ path: newRoot });
        return buildSettings({ meetings_root: newRoot });
      }
      return null;
    });

    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(screen.getByText("D:\\Meetings")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /change/i }));

    await waitFor(() => expect(screen.getByText(newRoot)).toBeInTheDocument());
    await settle();
  });

  it("renders an actionable banner when config.json failed to load and settings fell back to defaults (E3)", async () => {
    mockIPC((cmd) => {
      if (cmd === "get_settings")
        return buildSettings({
          meetings_root: null,
          meetings_root_exists: false,
          config_error: "malformed settings file config.json: expected value at line 1 column 1",
        });
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      return null;
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/could not be read/i)).toBeInTheDocument());
    expect(screen.getByText(/expected value at line 1 column 1/i)).toBeInTheDocument();
    // The app still opens into the (first-run) functional state -- never a
    // blank window and never a crash.
    expect(screen.getByText(/choose where meetings live/i)).toBeInTheDocument();
    await settle();
  });
});

describe("App service banner", () => {
  it("does not crash and surfaces settings load failures without an unhandled rejection", async () => {
    const configError: AppError = { kind: "config", message: "config.json is malformed" };
    mockIPC((cmd) => {
      if (cmd === "get_settings") throw configError;
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      return null;
    });

    expect(() => render(<App />)).not.toThrow();
    await waitFor(() => expect(screen.getByText(/config\.json is malformed/i)).toBeInTheDocument());
    await settle();
  });
});

describe("App model download wiring (T13)", () => {
  function modelStatus(overrides: Record<string, unknown> = {}) {
    return {
      state: "idle",
      downloaded_bytes: 0,
      total_bytes: 0,
      percent: 0,
      error_kind: null,
      error_message: null,
      model_present: false,
      ...overrides,
    };
  }

  it("shows the setup card's model step once a folder is chosen but the model is reported missing (FR-17)", async () => {
    mockIPC((cmd) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "model_download_status") return modelStatus();
      return null;
    });

    render(<App />);

    // The setup card takes over the main pane until the model is present or
    // skipped (spec.md 2a) -- the drop zone does not coexist with it.
    await waitFor(() => expect(screen.getByText(/model is missing/i)).toBeInTheDocument());
    expect(screen.getByText(/choose where meetings live/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /drop/i })).not.toBeInTheDocument();
    await settle();
  });

  it("does not render the download step once the model is already present (FR-16, upgrade must not re-download)", async () => {
    mockIPC((cmd) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "model_download_status")
        return modelStatus({ state: "complete", percent: 100, model_present: true });
      return null;
    });

    render(<App />);

    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());
    await flush();
    expect(screen.queryByText(/model is missing/i)).not.toBeInTheDocument();
    await settle();
  });

  it("shows a persistent GPU-setup notice when the model is present but a cuda_warning is set (E13)", async () => {
    mockIPC((cmd) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "model_download_status")
        return modelStatus({
          state: "complete",
          percent: 100,
          model_present: true,
          cuda_warning: "digest mismatch for nvidia_cublas_cu12.whl",
        });
      return null;
    });

    render(<App />);

    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("digest mismatch for nvidia_cublas_cu12.whl");
    expect(screen.getByRole("button", { name: /retry gpu setup/i })).toBeInTheDocument();
    await settle();
  });

  it("shows the persistent GPU-setup notice on a fresh app start even with no in-session cuda_warning, from health's durable cuda_runtime_present (E13 durable)", async () => {
    // Mirrors a fresh sidecar process after an earlier run's failed CUDA
    // download: no `SetupDownload` instance survives a restart, so
    // `cuda_warning` is `null`, but `/health.cuda_runtime_present` still
    // durably reports the runtime missing.
    mockIPC((cmd) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "model_download_status")
        return modelStatus({
          state: "complete",
          percent: 100,
          model_present: true,
          cuda_warning: null,
          cuda_runtime_present: false,
        });
      return null;
    });

    render(<App />);

    await waitFor(() => expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/GPU acceleration is not installed/i);
    expect(screen.getByRole("button", { name: /retry gpu setup/i })).toBeInTheDocument();
    await settle();
  });

  it("Skip for now leaves the drop zone usable with a persistent, non-modal missing-model notice (FR-17)", async () => {
    mockIPC((cmd) => {
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "model_download_status") return modelStatus();
      return null;
    });

    const user = userEvent.setup();
    render(<App />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /skip for now/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /skip for now/i }));

    expect(screen.getByRole("region", { name: /drop/i })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/model/i);
    await settle();
  });
});
