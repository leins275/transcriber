/**
 * "Suggest title from summary", end to end through the IPC seam: the menu
 * item invokes the command by entry id, the finished job's snapshot opens
 * the rename form prefilled, and only the operator's Save renames.
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
};

const entry = {
  id: "v-1",
  project: "ELS",
  meeting_name: "260812 - Recording 11 04 56",
  meeting_dir: MEETING_DIR,
  has_source: true,
  has_transcript: true,
};

function llmModels(present: boolean) {
  return {
    active: "qwen3.5-9b",
    gpu_build_present: true,
    models: [
      {
        id: "qwen3.5-9b",
        label: "Qwen3.5 9B",
        file: "Qwen3.5-9B-Q5_K_M.gguf",
        size_bytes: 1,
        catalog: true,
        present,
        active: true,
        download: {
          state: "idle",
          downloaded_bytes: 0,
          total_bytes: 0,
          percent: 0,
          error_kind: null,
          error_message: null,
        },
      },
    ],
  };
}

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-7",
    source_path: MEETING_DIR,
    job_type: "suggest_title",
    file_name: "260812 - Recording 11 04 56",
    state: "pending",
    classification: null,
    meeting_dir: MEETING_DIR,
    source_dest: null,
    transcript_path: null,
    progress: null,
    message: null,
    error_kind: null,
    suggested_title: null,
    created_at: "2026-08-21T00:00:00Z",
    ...overrides,
  };
}

type Call = { cmd: string; payload: unknown };

function mockApp(options: { modelPresent?: boolean; summary?: string | null } = {}): Call[] {
  const { modelPresent = true, summary = "## Overview\n\nWe reviewed the budget." } = options;
  const calls: Call[] = [];
  mockIPC(
    (cmd, payload) => {
      calls.push({ cmd, payload });
      if (cmd === "get_settings") return settings;
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "list_vault") return [entry];
      if (cmd === "list_llm_models") return llmModels(modelPresent);
      if (cmd === "read_summary")
        return { entry_id: "v-1", path: `${MEETING_DIR}\\summary.md`, markdown: summary };
      if (cmd === "suggest_title_for_vault_entry") return buildJob();
      if (cmd === "update_vault_entry") return { ...entry, meeting_name: "260812 - Budget review" };
      return null;
    },
    { shouldMockEvents: true },
  );
  return calls;
}

function flush(ms = 30) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function settle() {
  cleanup();
  await flush();
}

async function openRecording(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await user.click(await screen.findByText("Recording 11 04 56"));
  await waitFor(() =>
    expect(screen.getByRole("region", { name: /^recording$/i })).toBeInTheDocument(),
  );
}

const callsTo = (calls: Call[], cmd: string) => calls.filter((call) => call.cmd === cmd);

beforeEach(() => {
  mockWindows("main");
});

afterEach(() => {
  clearMocks();
});

describe("App: suggest title from summary", () => {
  it("submits the job by entry id, then prefills the rename form and renames only on Save", async () => {
    const calls = mockApp();
    const user = userEvent.setup();
    await openRecording(user);

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: "Suggest title from summary" }));

    await waitFor(() =>
      expect(callsTo(calls, "suggest_title_for_vault_entry").map((call) => call.payload)).toEqual([
        { entryId: "v-1" },
      ]),
    );

    // While it runs, the action is busy and no form has opened.
    await emit("jobs://updated", buildJob({ state: "running", phase: "writing title" }));
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    expect(await screen.findByRole("menuitem", { name: "Suggesting title…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /close menu/i }));
    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();

    await emit("jobs://updated", buildJob({ state: "done", suggested_title: "Budget review" }));

    expect(await screen.findByRole("form", { name: /rename meeting/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/title/i)).toHaveValue("Budget review");
    expect(screen.getByLabelText(/date/i)).toHaveValue("260812");
    expect(callsTo(calls, "update_vault_entry")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(callsTo(calls, "update_vault_entry")).toHaveLength(1));
    // The existing rename command, with only the title changed: the
    // project, the date and the (absent) type are the meeting's own.
    expect(callsTo(calls, "update_vault_entry")[0].payload).toEqual({
      entryId: "v-1",
      project: "ELS",
      date: "260812",
      title: "Budget review",
      kind: null,
    });
    await settle();
  });

  it("shows a finished suggestion once: cancelled, it does not come back on a later visit", async () => {
    mockApp();
    const user = userEvent.setup();
    await openRecording(user);

    await emit("jobs://updated", buildJob({ state: "done", suggested_title: "Budget review" }));
    await user.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /recordings/i }));
    await user.click(await screen.findByText("Recording 11 04 56"));
    await waitFor(() =>
      expect(screen.getByRole("region", { name: /^recording$/i })).toBeInTheDocument(),
    );
    await flush();

    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();
    await settle();
  });

  it("holds a suggestion that finished while the operator was away until the recording is opened", async () => {
    mockApp();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Recording 11 04 56");

    await emit("jobs://updated", buildJob({ state: "done", suggested_title: "Budget review" }));
    await flush();
    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();

    await user.click(screen.getAllByText("Recording 11 04 56")[0]);

    expect(await screen.findByRole("form", { name: /rename meeting/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/title/i)).toHaveValue("Budget review");
    await settle();
  });

  it("opens nothing for a failed suggestion", async () => {
    mockApp();
    const user = userEvent.setup();
    await openRecording(user);

    await emit(
      "jobs://updated",
      buildJob({
        state: "failed",
        error_kind: "llm_output",
        message: "the model produced no usable title",
      }),
    );
    await flush();

    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();
    await settle();
  });

  it("offers no suggestion while no language model is installed", async () => {
    mockApp({ modelPresent: false });
    const user = userEvent.setup();
    await openRecording(user);
    await flush();

    await user.click(screen.getByRole("button", { name: /more actions/i }));

    expect(screen.getByRole("menuitem", { name: "Rename" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
    await settle();
  });

  it("offers no suggestion for a meeting without a summary", async () => {
    mockApp({ summary: null });
    const user = userEvent.setup();
    await openRecording(user);
    await flush();

    await user.click(screen.getByRole("button", { name: /more actions/i }));

    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
    await settle();
  });
});
