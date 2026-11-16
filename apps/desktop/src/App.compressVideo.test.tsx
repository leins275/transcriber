/**
 * The manual "Compress video" action, end to end through the IPC seam: the
 * menu item invokes the command by entry id, and the marker flips once the
 * finished job has the vault listed again.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { emit } from "@tauri-apps/api/event";
import App from "./App";
import type { JobSnapshot, SettingsView } from "./types";

const MEETING_DIR = "D:\\Meetings\\ELS\\260812 - Recording 11 04 56";

const settings: SettingsView = {
  meetings_root: "D:\\Meetings",
  meetings_root_exists: true,
  service_base_url: null,
  supported_extensions: [".mp4", ".wav"],
  config_error: null,
  default_meetings_root: null,
  diarize: false,
  hf_token_present: false,
  compress_video: true,
  normalize_audio: true,
};

const entry = {
  id: "v-1",
  project: "ELS",
  meeting_name: "260812 - Recording 11 04 56",
  meeting_dir: MEETING_DIR,
  has_source: true,
  has_transcript: false,
  video_compressed: false,
};

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-7",
    source_path: MEETING_DIR,
    job_type: "compress",
    file_name: "260812 - Recording 11 04 56",
    state: "pending",
    classification: null,
    meeting_dir: MEETING_DIR,
    source_dest: null,
    transcript_path: null,
    progress: null,
    message: null,
    error_kind: null,
    created_at: "2026-08-21T00:00:00Z",
    ...overrides,
  };
}

type Call = { cmd: string; payload: unknown };

beforeEach(() => {
  mockWindows("main");
});

afterEach(async () => {
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 30));
  clearMocks();
});

describe("App: compress video on request", () => {
  it("submits the job by entry id and flips the marker when it finishes", async () => {
    const calls: Call[] = [];
    let compressed = false;
    mockIPC(
      (cmd, payload) => {
        calls.push({ cmd, payload });
        if (cmd === "get_settings") return settings;
        if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
        if (cmd === "list_vault") return [{ ...entry, video_compressed: compressed }];
        if (cmd === "compress_vault_entry") return buildJob();
        return null;
      },
      { shouldMockEvents: true },
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByText("Recording 11 04 56"));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: /^recording$/i })).toBeInTheDocument(),
    );
    expect(screen.getByText(/Video not compressed/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Compress video" }));

    await waitFor(() =>
      expect(
        calls.filter((call) => call.cmd === "compress_vault_entry").map((call) => call.payload),
      ).toEqual([{ entryId: "v-1" }]),
    );

    // While it runs, the action is busy.
    await emit("jobs://updated", buildJob({ state: "running", phase: "compressing video" }));
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    expect(await screen.findByRole("menuitem", { name: "Compressing video…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /close menu/i }));

    // The service swapped the recording; the finished job re-lists the vault.
    compressed = true;
    await emit("jobs://updated", buildJob({ state: "done" }));

    expect(await screen.findByText(/Video compressed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    expect(screen.getByRole("menuitem", { name: "Compress again" })).toBeEnabled();
  });
});
