import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import App from "./App";
import type {
  SettingsView,
  SpeakerDetailView,
  SpeakerView,
  TranscriptView,
  VaultMeetingView,
} from "./types";

/**
 * The speakers database as the operator meets it: the Speakers tab beside
 * Recordings, a speaker's page, and the way back. Everything below the IPC
 * boundary is real; `mockIPC` stands in for the Rust shell and keeps a tiny
 * registry so that a save is visible to the next read, the way the service
 * behaves.
 */

function buildSettings(): SettingsView {
  return {
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
}

const ENTRY: VaultMeetingView = {
  id: "v-1",
  project: "GIS",
  meeting_name: "260903 - Photos and flows",
  meeting_dir: "D:\\Meetings\\GIS\\260903 - Photos and flows",
  has_source: true,
  has_transcript: true,
};

function buildTranscript(): TranscriptView {
  return {
    entry_id: "v-1",
    meeting_name: ENTRY.meeting_name,
    language: "en",
    created_at: "2026-09-03T10:00:00Z",
    duration_sec: 120,
    model: "large-v3",
    device: "cuda",
    text: "the whole transcript text",
    segments: [{ id: 0, start: 0, end: 4, text: " the whole transcript text" }],
    speakers: {},
    transcript_path: "D:\\Meetings\\GIS\\260903 - Photos and flows\\transcript.json",
  };
}

type Person = { name: string; aliases: string[]; bio: string; registered: boolean };

function row(person: Person): SpeakerView {
  return {
    ...person,
    projects: ["GIS"],
    meetings: 1,
    labelled_segments: 3,
    hand_segments: 2,
    speech_sec: 30,
    voice_samples: 1,
    voice_set_aside: 0,
  };
}

function page(person: Person): SpeakerDetailView {
  return {
    ...person,
    projects: [{ project: "GIS", meetings: 1, speech_sec: 30, in_roster: false }],
    voice: { samples: 1, set_aside: 0, speech_sec: 30 },
    meetings: [
      {
        entry_id: "v-1",
        project: "GIS",
        meeting: "260903 - Photos and flows",
        labelled_segments: 3,
        hand_segments: 2,
        speech_sec: 30,
        voice_quality: "ok",
        segments: [{ id: 4, start: 12, end: 15, text: "a labelled line" }],
        segments_truncated: false,
      },
    ],
  };
}

function flush(ms = 20) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function mountApp() {
  const people: Person[] = [
    { name: "Nikita", aliases: ["Никита"], bio: "", registered: true },
    { name: "Anna", aliases: [], bio: "", registered: true },
  ];
  const find = (name: string) =>
    people.find((person) => person.name === name || person.aliases.includes(name));
  const calls: Array<{ cmd: string; payload: unknown }> = [];
  mockIPC(
    (cmd, payload) => {
      const args = (payload ?? {}) as Record<string, unknown>;
      if (cmd.endsWith("_speaker") || cmd.endsWith("_speakers") || cmd === "speaker_detail") {
        calls.push({ cmd, payload });
      }
      if (cmd === "get_settings") return buildSettings();
      if (cmd === "service_status") return { state: "ready", base_url: null, detail: null };
      if (cmd === "list_vault") return [ENTRY];
      if (cmd === "read_transcript") return buildTranscript();
      if (cmd === "read_summary")
        return { entry_id: "v-1", path: "D:\\summary.md", markdown: null };
      if (cmd === "read_note") return { entry_id: "v-1", path: "D:\\note.md", markdown: null };
      if (cmd === "list_project_speaker_names") return [];
      if (cmd === "read_project_roster") return { mode: "open", names: [] };
      if (cmd === "list_speakers") return people.map(row);
      if (cmd === "speaker_detail") {
        const person = find(args.name as string);
        if (!person) throw { kind: "service", message: `no such person: ${String(args.name)}` };
        return page(person);
      }
      if (cmd === "save_speaker") {
        let person = find(args.name as string);
        if (!person) {
          person = { name: args.name as string, aliases: [], bio: "", registered: true };
          people.push(person);
        }
        if (typeof args.newName === "string") {
          person.aliases.push(person.name);
          person.name = args.newName;
        }
        if (Array.isArray(args.aliases)) person.aliases = args.aliases as string[];
        if (typeof args.bio === "string") person.bio = args.bio;
        return { ...person };
      }
      if (cmd === "delete_speaker") {
        const index = people.findIndex((person) => person.name === args.name);
        if (index >= 0) people.splice(index, 1);
        return index >= 0;
      }
      return null;
    },
    { shouldMockEvents: true },
  );
  render(<App />);
  return { calls };
}

async function openSpeakersTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("tab", { name: "Speakers" }));
  return await screen.findByRole("tabpanel", { name: "Speakers" });
}

beforeEach(() => {
  mockWindows("main");
});

afterEach(async () => {
  // Unmount before `clearMocks()` — the teardown contract `App.test.tsx`
  // documents for this harness.
  cleanup();
  await flush(30);
  clearMocks();
});

describe("App speakers database", () => {
  it("reads the database only when the Speakers tab is opened", async () => {
    const user = userEvent.setup();
    const { calls } = mountApp();
    await screen.findByRole("tab", { name: "Speakers" });
    await flush();
    expect(calls).toHaveLength(0);

    const panel = await openSpeakersTab(user);

    expect(await within(panel).findByRole("button", { name: "Nikita" })).toBeInTheDocument();
    expect(calls.map((call) => call.cmd)).toEqual(["list_speakers"]);
  });

  it("adds a speaker from the tab and lists them", async () => {
    const user = userEvent.setup();
    const { calls } = mountApp();
    const panel = await openSpeakersTab(user);
    await within(panel).findByRole("button", { name: "Nikita" });

    await user.click(within(panel).getByRole("button", { name: "Add speaker" }));
    await user.type(within(panel).getByRole("textbox", { name: "Speaker name" }), "Olga{Enter}");

    expect(await within(panel).findByRole("button", { name: "Olga" })).toBeInTheDocument();
    expect(calls).toContainEqual({
      cmd: "save_speaker",
      payload: { name: "Olga", newName: null, aliases: null, bio: null },
    });
  });

  it("opens a speaker's page and comes back to the Speakers tab", async () => {
    const user = userEvent.setup();
    mountApp();
    const panel = await openSpeakersTab(user);

    await user.click(await within(panel).findByRole("button", { name: "Nikita" }));

    expect(await screen.findByRole("heading", { name: "Nikita" })).toBeInTheDocument();
    expect(await screen.findByText("a labelled line")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "← Speakers" }));

    expect(await screen.findByRole("tab", { name: "Speakers" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByRole("button", { name: "Nikita" })).toBeInTheDocument();
  });

  it("keeps the page open under the new name after a rename", async () => {
    const user = userEvent.setup();
    const { calls } = mountApp();
    const panel = await openSpeakersTab(user);
    await user.click(await within(panel).findByRole("button", { name: "Anna" }));
    await screen.findByText("a labelled line");

    await user.click(screen.getByRole("button", { name: "Rename speaker" }));
    const input = screen.getByRole("textbox", { name: "Name" });
    await user.clear(input);
    await user.type(input, "Anna K{Enter}");

    expect(await screen.findByRole("heading", { name: "Anna K" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Remove alias Anna" })).toBeInTheDocument();
    expect(calls).toContainEqual({
      cmd: "save_speaker",
      payload: { name: "Anna", newName: "Anna K", aliases: null, bio: null },
    });
    await waitFor(() =>
      expect(calls[calls.length - 1]).toEqual({
        cmd: "speaker_detail",
        payload: { name: "Anna K" },
      }),
    );
  });

  it("opens a recording from the speaker's page and returns to the speaker", async () => {
    const user = userEvent.setup();
    mountApp();
    const panel = await openSpeakersTab(user);
    await user.click(await within(panel).findByRole("button", { name: "Nikita" }));

    await user.click(await screen.findByRole("button", { name: /Photos and flows/ }));

    expect(await screen.findByText(/the whole transcript text/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "← Recordings" }));

    expect(await screen.findByRole("heading", { name: "Nikita" })).toBeInTheDocument();
  });

  it("removes a speaker and lands back on the table without them", async () => {
    const user = userEvent.setup();
    const { calls } = mountApp();
    const panel = await openSpeakersTab(user);
    await user.click(await within(panel).findByRole("button", { name: "Anna" }));

    await user.click(await screen.findByRole("button", { name: "Remove from database" }));
    await user.click(screen.getByRole("button", { name: "Remove" }));

    expect(await screen.findByRole("button", { name: "Nikita" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Anna" })).not.toBeInTheDocument();
    expect(calls).toContainEqual({ cmd: "delete_speaker", payload: { name: "Anna" } });
  });

  it("offers the database's people in the project roster editor", async () => {
    const user = userEvent.setup();
    mountApp();
    await user.click(await screen.findByText("Photos and flows"));
    await screen.findByText(/the whole transcript text/);

    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    const picker = await screen.findByRole("combobox", { name: "From the speakers database" });
    expect(within(picker).getByRole("option", { name: "Nikita" })).toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: "Anna" })).toBeInTheDocument();
  });
});
