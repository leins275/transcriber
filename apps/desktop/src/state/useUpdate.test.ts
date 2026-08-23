import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { UPDATE_ERROR_DISMISS_MS, useUpdate } from "./useUpdate";

// The plugin's `check()` talks to a real network endpoint; scripted here so
// each test decides what the manifest said. `api.ts` re-exports the plugin's
// own `Update` type, so a structural stand-in with `download`/`install` is
// all these tests need.
const checkMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/plugin-updater", () => ({
  check: checkMock,
}));

beforeEach(() => {
  mockWindows("main");
  checkMock.mockReset();
});

afterEach(() => {
  clearMocks();
  vi.useRealTimers();
});

describe("useUpdate install", () => {
  it("stops the sidecar between download and install (never install over a running pyenv)", async () => {
    // The regression this ordering prevents: the NSIS installer overwrites
    // pyenv\ in place, and installing while the bundled Python sidecar is
    // still running fails with "Error opening file for writing:
    // ...\pyenv\...". The sidecar must be stopped after the download has
    // succeeded (so a failed download costs nothing) and before install.
    const calls: string[] = [];
    mockIPC((cmd) => {
      calls.push(cmd);
      return null;
    });
    checkMock.mockResolvedValue({
      version: "9.9.9",
      body: undefined,
      date: undefined,
      download: vi.fn(async () => {
        calls.push("download");
      }),
      install: vi.fn(async () => {
        calls.push("install");
      }),
    });

    const { result } = renderHook(() => useUpdate());
    await waitFor(() => expect(result.current.state.status).toBe("available"));

    await act(async () => {
      await result.current.install();
    });

    expect(
      calls.filter((cmd) => cmd === "download" || cmd === "prepare_update" || cmd === "install"),
    ).toEqual(["download", "prepare_update", "install"]);
    expect(result.current.state.status).toBe("installed");
  });
});

describe("useUpdate error auto-dismiss", () => {
  it("dismisses a failed check on its own after the linger", async () => {
    vi.useFakeTimers();
    checkMock.mockRejectedValue(new Error("offline"));

    const { result } = renderHook(() => useUpdate());
    // Flush the rejected check's microtasks; no timers involved yet.
    await act(async () => {});
    expect(result.current.state.status).toBe("error");

    act(() => {
      vi.advanceTimersByTime(UPDATE_ERROR_DISMISS_MS);
    });
    expect(result.current.state.status).toBe("idle");
  });
});
