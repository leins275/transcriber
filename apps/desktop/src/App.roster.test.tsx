import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import App from "./App";
import type { ProjectRosterView, SettingsView, TranscriptView, VaultMeetingView } from "./types";

/**
 * T10 — the whole roster chain as the operator meets it: `App` reads the
 * open recording's project roster over IPC, hands it down the page, and the
 * speaker controls in the transcript become a pick-list or stay free text
 * accordingly (FR-7, and FR-4/FR-5 as they are observable from the app).
 *
 * Everything below the IPC boundary is real — the same components,
 * hooks and state the app ships. The only test double is `mockIPC`, which
 * stands in for the Rust shell: the one out-of-process dependency this
 * suite cannot run in-process. Roster commands are asserted at that edge,
 * by payload and call count.
 */

function buildSettings(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    meetings_root: "D:\\Meetings",
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

/** A recording filed under a project — the one that has a roster. */
const FILED_ENTRY: VaultMeetingView = {
  id: "v-1",
  project: "RDDM",
  meeting_name: "260709 - tech support 1",
  meeting_dir: "D:\\Meetings\\RDDM\\260709 - tech support 1",
  has_source: true,
  has_transcript: true,
};

/** A recording nobody has filed — no project, therefore no roster. */
const UNFILED_ENTRY: VaultMeetingView = {
  id: "v-2",
  project: null,
  meeting_name: "260810 - loose file",
  meeting_dir: "D:\\Meetings\\unsorted\\260810 - loose file",
  has_source: true,
  has_transcript: true,
};

/** One turn nobody has attributed yet: its tag reads "Add speaker", which
 * is where the roster picker (or the free-text box) has to appear. */
function buildTranscript(entryId: string): TranscriptView {
  return {
    entry_id: entryId,
    meeting_name: "260709 - tech support 1",
    language: "ru",
    created_at: "2026-07-09T10:00:00Z",
    duration_sec: 491,
    model: "large-v3",
    device: "cuda",
    text: "может еще запись включим",
    segments: [{ id: 0, start: 0, end: 4, text: " может еще запись включим" }],
    speakers: {},
    transcript_path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\transcript.json",
  };
}

const OPEN_ROSTER: ProjectRosterView = { mode: "open", names: [] };

function flush(ms = 20) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Unmounts while the mocked IPC internals are still installed — the
 * teardown contract `App.test.tsx` documents for this harness. */
async function settle() {
  cleanup();
  await flush(30);
}

type RosterIpc = {
  /** Answers `read_project_roster`; throw to simulate a failed read. */
  onRead?: (project: string) => ProjectRosterView;
  /** Answers `save_project_roster` with the normalized view the command
   * would return; defaults to echoing the draft back. */
  onSave?: (project: string, roster: ProjectRosterView) => ProjectRosterView;
};

/** Boots the app over a vault holding one filed and one unfiled recording,
 * recording every roster call the app makes at the IPC edge. */
function mountApp(roster: RosterIpc = {}) {
  const rosterReads: unknown[] = [];
  const rosterSaves: unknown[] = [];
  mockIPC(
    (cmd, payload) => {
      const args = (payload ?? {}) as Record<string, unknown>;
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "list_vault") return [FILED_ENTRY, UNFILED_ENTRY];
      if (cmd === "read_transcript") return buildTranscript(args.entryId as string);
      if (cmd === "read_summary")
        return { entry_id: args.entryId as string, path: "D:\\summary.md", markdown: null };
      if (cmd === "read_note")
        return { entry_id: args.entryId as string, path: "D:\\note.md", markdown: null };
      if (cmd === "list_project_speaker_names") return [];
      if (cmd === "read_project_roster") {
        rosterReads.push(payload);
        return (roster.onRead ?? (() => OPEN_ROSTER))(args.project as string);
      }
      if (cmd === "save_project_roster") {
        rosterSaves.push(payload);
        const draft = args.roster as ProjectRosterView;
        return (roster.onSave ?? ((_project, saved) => saved))(args.project as string, draft);
      }
      return null;
    },
    { shouldMockEvents: true },
  );
  render(<App />);
  return { rosterReads, rosterSaves };
}

/** Opens a recording from the library and waits for its transcript. */
async function openRecording(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(await screen.findByText(title));
  await screen.findByText(/может еще/);
}

beforeEach(() => {
  mockWindows("main");
});

afterEach(async () => {
  // Unmount *before* `clearMocks()`: Tauri's `listen` teardown is
  // asynchronous under a synchronous public API, so a late unmount would
  // race this file's mock removal and reject unhandled. Doing it here
  // rather than at the end of every test keeps a failing test quiet too.
  await settle();
  clearMocks();
});

describe("App project roster loading", () => {
  it("reads the roster of the open recording's project, once", async () => {
    const user = userEvent.setup();
    const { rosterReads } = mountApp();

    await openRecording(user, "tech support 1");

    await waitFor(() => expect(rosterReads).toEqual([{ project: "RDDM" }]));
  });

  it("asks for no roster when the open recording is not filed under a project", async () => {
    const user = userEvent.setup();
    const { rosterReads } = mountApp();

    await openRecording(user, "loose file");

    await flush();
    expect(rosterReads).toHaveLength(0);
  });

  it("names a speaker from the project roster when the project is in roster mode", async () => {
    const user = userEvent.setup();
    mountApp({ onRead: () => ({ mode: "roster", names: ["Anna"] }) });
    await openRecording(user, "tech support 1");

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    const picker = await screen.findByRole("combobox", { name: "Name this speaker" });
    expect(within(picker).getByRole("option", { name: "Anna" })).toBeInTheDocument();
  });

  it("keeps the free-text name box on an unfiled recording, which has no project roster", async () => {
    const user = userEvent.setup();
    mountApp({ onRead: () => ({ mode: "roster", names: ["Anna"] }) });
    await openRecording(user, "loose file");

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(await screen.findByRole("textbox", { name: "Name this speaker" })).toBeInTheDocument();
  });

  it("opens the recording in open mode when the roster cannot be read", async () => {
    const user = userEvent.setup();
    mountApp({
      onRead: () => {
        throw { kind: "internal", message: "roster.json is unreadable" };
      },
    });
    await openRecording(user, "tech support 1");

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(await screen.findByRole("textbox", { name: "Name this speaker" })).toBeInTheDocument();
  });
});

describe("App project roster saving", () => {
  it("saves the edited roster under the open recording's project", async () => {
    const user = userEvent.setup();
    const { rosterSaves } = mountApp({ onRead: () => ({ mode: "open", names: ["Anna"] }) });
    await openRecording(user, "tech support 1");
    await user.click(await screen.findByRole("button", { name: "Project speakers" }));
    await user.click(await screen.findByRole("radio", { name: /only the roster below/i }));

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(rosterSaves).toEqual([
        { project: "RDDM", roster: { mode: "roster", names: ["Anna"] } },
      ]),
    );
  });

  it("offers the names the save command returned, not the ones that were edited", async () => {
    // The command owns normalization: whatever it answers with is what the
    // picker must offer next, or the operator is choosing from a list the
    // vault does not hold.
    const user = userEvent.setup();
    mountApp({
      onRead: () => ({ mode: "open", names: ["Anna"] }),
      onSave: () => ({ mode: "roster", names: ["Anna", "Maxim"] }),
    });
    await openRecording(user, "tech support 1");
    await user.click(await screen.findByRole("button", { name: "Project speakers" }));
    await user.click(await screen.findByRole("radio", { name: /only the roster below/i }));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await user.click(await screen.findByRole("button", { name: /add speaker/i }));

    const picker = await screen.findByRole("combobox", { name: "Name this speaker" });
    expect(within(picker).getByRole("option", { name: "Maxim" })).toBeInTheDocument();
  });
});
