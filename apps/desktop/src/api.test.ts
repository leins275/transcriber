import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import {
  api,
  appVersion,
  chooseFile,
  chooseMeetingsFolder,
  onDragDrop,
  onJobsUpdated,
  onServiceStatus,
} from "./api";
import type { AppError, JobSnapshot, ServiceStatusView } from "./types";

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\Meetings\\in\\file.mp4",
    file_name: "file.mp4",
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

beforeEach(() => {
  mockWindows("main");
});

afterEach(() => {
  clearMocks();
});

describe("api.enqueuePaths", () => {
  it("invokes enqueue_paths with { paths } and returns the snapshots", async () => {
    const snapshots = [buildJob()];
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "enqueue_paths") return snapshots;
      return null;
    });

    const result = await api.enqueuePaths(["C:\\Meetings\\in\\file.mp4"]);

    expect(result).toEqual(snapshots);
    expect(seen).toContainEqual({
      cmd: "enqueue_paths",
      payload: { paths: ["C:\\Meetings\\in\\file.mp4"] },
    });
  });
});

describe("api.prepareUpdate", () => {
  it("invokes prepare_update with no arguments", async () => {
    const seen: string[] = [];
    mockIPC((cmd) => {
      seen.push(cmd);
      return null;
    });

    await api.prepareUpdate();

    expect(seen).toContain("prepare_update");
  });
});

describe("appVersion", () => {
  it("resolves the version the app plugin reports", async () => {
    mockIPC((cmd) => {
      if (cmd === "plugin:app|version") return "9.9.9";
      return null;
    });

    await expect(appVersion()).resolves.toBe("9.9.9");
  });
});

describe("api error handling", () => {
  it("surfaces a rejected {kind, message} payload as a typed AppError, never as an unhandled rejection", async () => {
    const backendError: AppError = { kind: "not_configured", message: "no meetings root set" };
    mockIPC(() => {
      throw backendError;
    });

    await expect(api.enqueuePaths(["x.mp4"])).rejects.toEqual(backendError);
  });

  it("wraps a non-AppError-shaped rejection into an internal AppError", async () => {
    mockIPC(() => {
      throw new Error("boom");
    });

    let rejection: AppError | undefined;
    try {
      await api.listJobs();
    } catch (error) {
      rejection = error as AppError;
    }
    expect(rejection?.kind).toBe("internal");
    expect(rejection?.message).toContain("boom");
  });
});

describe("api dialog wrappers", () => {
  it("chooseFile resolves to an array of selected paths", async () => {
    mockIPC((cmd) => {
      if (cmd === "plugin:dialog|open") return "C:\\Meetings\\in\\a.mp4";
      return null;
    });
    await expect(chooseFile()).resolves.toEqual(["C:\\Meetings\\in\\a.mp4"]);
  });

  it("chooseFile resolves to an empty array when the dialog is cancelled", async () => {
    mockIPC((cmd) => {
      if (cmd === "plugin:dialog|open") return null;
      return null;
    });
    await expect(chooseFile()).resolves.toEqual([]);
  });

  it("chooseMeetingsFolder resolves to the selected directory", async () => {
    mockIPC((cmd) => {
      if (cmd === "plugin:dialog|open") return "D:\\Meetings";
      return null;
    });
    await expect(chooseMeetingsFolder()).resolves.toBe("D:\\Meetings");
  });

  it("chooseMeetingsFolder forwards a sane default path to the dialog (E2/FR-10)", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "plugin:dialog|open") return "D:\\Meetings";
      return null;
    });
    await chooseMeetingsFolder("C:\\Users\\op\\Meetings");
    const openCall = seen.find((call) => call.cmd === "plugin:dialog|open");
    expect(
      (openCall?.payload as { options?: { defaultPath?: string } })?.options?.defaultPath,
    ).toBe("C:\\Users\\op\\Meetings");
  });
});

describe("api.revealJob", () => {
  it("invokes reveal_job with the job id", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      return null;
    });
    await api.revealJob("job-1");
    expect(seen).toContainEqual({ cmd: "reveal_job", payload: { jobId: "job-1" } });
  });
});

describe("api vault-browser trio", () => {
  it("listVault invokes list_vault with no arguments and returns the entries", async () => {
    const entries = [
      {
        id: "v1",
        project: "ELS",
        meeting_name: "260812 - Security issue",
        meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
        has_source: true,
        has_transcript: true,
      },
    ];
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "list_vault") return entries;
      return null;
    });

    const result = await api.listVault();

    expect(result).toEqual(entries);
    expect(seen).toContainEqual({ cmd: "list_vault", payload: {} });
  });

  it("revealVaultEntry invokes reveal_vault_entry with the entry id", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      return null;
    });

    await api.revealVaultEntry("v1");

    expect(seen).toContainEqual({ cmd: "reveal_vault_entry", payload: { entryId: "v1" } });
  });
});

describe("api model-download trio (T13)", () => {
  it("modelDownloadStatus invokes model_download_status with no arguments", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "model_download_status") {
        return {
          state: "idle",
          downloaded_bytes: 0,
          total_bytes: 0,
          percent: 0,
          error_kind: null,
          error_message: null,
          model_present: false,
        };
      }
      return null;
    });

    const status = await api.modelDownloadStatus();

    expect(status.state).toBe("idle");
    expect(seen).toContainEqual({ cmd: "model_download_status", payload: {} });
  });

  it("startModelDownload invokes start_model_download with no arguments", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "start_model_download") {
        return {
          state: "downloading",
          downloaded_bytes: 0,
          total_bytes: 100,
          percent: 0,
          error_kind: null,
          error_message: null,
          model_present: false,
        };
      }
      return null;
    });

    const status = await api.startModelDownload();

    expect(status.state).toBe("downloading");
    expect(seen).toContainEqual({ cmd: "start_model_download", payload: {} });
  });

  it("cancelModelDownload invokes cancel_model_download with no arguments", async () => {
    const seen: Array<{ cmd: string; payload: unknown }> = [];
    mockIPC((cmd, payload) => {
      seen.push({ cmd, payload });
      if (cmd === "cancel_model_download") {
        return {
          state: "cancelled",
          downloaded_bytes: 0,
          total_bytes: 100,
          percent: 0,
          error_kind: null,
          error_message: null,
          model_present: false,
        };
      }
      return null;
    });

    const status = await api.cancelModelDownload();

    expect(status.state).toBe("cancelled");
    expect(seen).toContainEqual({ cmd: "cancel_model_download", payload: {} });
  });
});

describe("event subscriptions", () => {
  it("onJobsUpdated invokes the handler with the JobSnapshot payload from jobs://updated", async () => {
    mockIPC(() => null, { shouldMockEvents: true });
    const handler = vi.fn();
    const unlisten = await onJobsUpdated(handler);
    const job = buildJob({ state: "running" });

    await emit("jobs://updated", job);

    expect(handler).toHaveBeenCalledWith(job);
    unlisten();
  });

  it("onServiceStatus invokes the handler with the ServiceStatusView payload from service://status", async () => {
    mockIPC(() => null, { shouldMockEvents: true });
    const handler = vi.fn();
    const unlisten = await onServiceStatus(handler);
    const status: ServiceStatusView = {
      state: "unavailable",
      base_url: "http://127.0.0.1:1",
      detail: null,
    };

    await emit("service://status", status);

    expect(handler).toHaveBeenCalledWith(status);
    unlisten();
  });

  it("onDragDrop invokes the handler with a typed drop event carrying real paths", async () => {
    mockIPC(() => null, { shouldMockEvents: true });
    const handler = vi.fn();
    const unlisten = await onDragDrop(handler);

    await emit("tauri://drag-drop", { paths: ["C:\\x\\a.mp4"], position: { x: 0, y: 0 } });

    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ type: "drop", paths: ["C:\\x\\a.mp4"] }),
    );
    unlisten();
  });
});
