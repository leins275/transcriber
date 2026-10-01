import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingPage } from "./RecordingPage";
import type {
  NoteView,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
  VoiceStatusView,
} from "../types";

const entry: VaultMeetingView = {
  id: "v-1",
  project: "RDDM",
  meeting_name: "260709 - tech support 1",
  meeting_dir: "D:\\Meetings\\RDDM\\260709 - tech support 1",
  has_source: true,
  has_transcript: true,
};

const transcript: TranscriptView = {
  entry_id: "v-1",
  meeting_name: "260709 - tech support 1",
  language: "ru",
  created_at: "2026-07-09T10:00:00Z",
  duration_sec: 491,
  model: "large-v3",
  device: "cuda",
  text: "может еще запись включим",
  segments: [{ id: 0, start: 0, end: 4, text: " может еще запись включим" }],
  speakers: { "0": "Kirill" },
  transcript_path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\transcript.json",
};

const emptySummary: SummaryView = { entry_id: "v-1", path: "summary.md", markdown: null };
const emptyNote: NoteView = { entry_id: "v-1", path: "note.md", markdown: null };

function memory(overrides: Partial<VoiceStatusView> = {}): VoiceStatusView {
  return {
    project: "RDDM",
    roster_only: false,
    updated_at: 1_790_000_000,
    rescanned: [],
    rescanned_elsewhere: 0,
    voices: [
      { name: "Kirill", samples: 1, here: 1, other_projects: [], speech_sec: 180, set_aside: 0 },
    ],
    meetings: [
      {
        name: "260709 - tech support 1",
        state: "named",
        scanned_at: 1_790_000_000,
        voices: [
          {
            label: "Speaker 1",
            name: "Kirill",
            speech_sec: 180,
            quality: "unconfirmed",
            conflicts_with: null,
          },
        ],
      },
    ],
    ...overrides,
  };
}

function renderPage(props: Partial<React.ComponentProps<typeof RecordingPage>> = {}) {
  return render(
    <RecordingPage
      entry={entry}
      projects={["RDDM"]}
      projectSpeakers={[]}
      projectRoster={{ mode: "open", names: [] }}
      onSaveRoster={() => Promise.resolve()}
      onBack={() => {}}
      onReveal={() => {}}
      onReadTranscript={() => Promise.resolve(transcript)}
      onReadSummary={() => Promise.resolve(emptySummary)}
      onReadNote={() => Promise.resolve(emptyNote)}
      onSaveNote={() => Promise.resolve()}
      onSaveSpeakers={() => Promise.resolve()}
      onUpdate={() => Promise.resolve()}
      onDelete={() => Promise.resolve()}
      onTranscribe={() => Promise.resolve()}
      onSummarize={() => Promise.resolve()}
      onExportPdf={() => Promise.resolve()}
      onDiarize={() => Promise.resolve()}
      speakersReady
      activeLlmJobs={[]}
      summaryReloadToken={0}
      {...props}
    />,
  );
}

describe("RecordingPage voice memory", () => {
  it("shows the project's voice memory under the roster, read for that project", async () => {
    const user = userEvent.setup();
    const onLoadVoiceMemory = vi.fn().mockResolvedValue(memory());
    renderPage({ onLoadVoiceMemory });

    // Nothing is read until the operator opens the panel.
    expect(onLoadVoiceMemory).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    const panel = await screen.findByRole("region", { name: "Voice memory" });
    expect(onLoadVoiceMemory).toHaveBeenCalledWith("RDDM");
    expect(await within(panel).findByText("1 sample · 3m 0s of speech")).toBeInTheDocument();
  });

  it("confirms this meeting's machine-given names by entry id", async () => {
    const user = userEvent.setup();
    const onLoadVoiceMemory = vi.fn().mockResolvedValue(memory());
    const onConfirmSpeakers = vi.fn().mockResolvedValue(undefined);
    renderPage({ onLoadVoiceMemory, onConfirmSpeakers });
    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    await user.click(await screen.findByRole("button", { name: "Confirm these names" }));

    expect(onConfirmSpeakers).toHaveBeenCalledWith("v-1");
    await waitFor(() => expect(onLoadVoiceMemory).toHaveBeenCalledTimes(2));
  });

  it("reads the memory again when a speaker pass over this meeting finishes", async () => {
    const user = userEvent.setup();
    const onLoadVoiceMemory = vi.fn().mockResolvedValue(memory());
    const view = renderPage({ onLoadVoiceMemory, activeLlmJobs: ["diarize"] });
    await user.click(screen.getByRole("button", { name: "Project speakers" }));
    await screen.findByRole("region", { name: "Voice memory" });
    await waitFor(() => expect(onLoadVoiceMemory).toHaveBeenCalledTimes(1));

    view.rerender(
      <RecordingPage
        entry={entry}
        projects={["RDDM"]}
        projectSpeakers={[]}
        projectRoster={{ mode: "open", names: [] }}
        onSaveRoster={() => Promise.resolve()}
        onLoadVoiceMemory={onLoadVoiceMemory}
        onBack={() => {}}
        onReveal={() => {}}
        onReadTranscript={() => Promise.resolve(transcript)}
        onReadSummary={() => Promise.resolve(emptySummary)}
        onReadNote={() => Promise.resolve(emptyNote)}
        onSaveNote={() => Promise.resolve()}
        onSaveSpeakers={() => Promise.resolve()}
        onUpdate={() => Promise.resolve()}
        onDelete={() => Promise.resolve()}
        onTranscribe={() => Promise.resolve()}
        onSummarize={() => Promise.resolve()}
        onExportPdf={() => Promise.resolve()}
        onDiarize={() => Promise.resolve()}
        speakersReady
        activeLlmJobs={[]}
        summaryReloadToken={0}
      />,
    );

    await waitFor(() => expect(onLoadVoiceMemory).toHaveBeenCalledTimes(2));
  });

  it("renders no voice memory for an unfiled recording or without a loader", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    expect(screen.queryByRole("region", { name: "Voice memory" })).not.toBeInTheDocument();
  });
});
