import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingPage } from "./RecordingPage";
import type {
  NoteView,
  ProjectRosterView,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
} from "../types";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: "RDDM",
    meeting_name: "260709 - tech support 1",
    meeting_dir: "D:\\Meetings\\RDDM\\260709 - tech support 1",
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

/** One unattributed turn: the speaker tag opens as "Add speaker", which is
 * where the roster (or the free-text box) has to appear. */
function buildTranscript(overrides: Partial<TranscriptView> = {}): TranscriptView {
  return {
    entry_id: "v-1",
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
    ...overrides,
  };
}

const emptySummary: SummaryView = {
  entry_id: "v-1",
  path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\summary.md",
  markdown: null,
};

const emptyNote: NoteView = {
  entry_id: "v-1",
  path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\note.md",
  markdown: null,
};

const openRoster: ProjectRosterView = { mode: "open", names: [] };

function renderPage(props: Partial<React.ComponentProps<typeof RecordingPage>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["RDDM", "ELS"],
    projectSpeakers: [],
    projectRoster: openRoster,
    onSaveRoster: () => Promise.resolve(),
    onBack: () => {},
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(buildTranscript()),
    onReadSummary: () => Promise.resolve(emptySummary),
    onReadNote: () => Promise.resolve(emptyNote),
    onSaveNote: () => Promise.resolve(),
    onSaveSpeakers: () => Promise.resolve(),
    onUpdate: () => Promise.resolve(),
    onDelete: () => Promise.resolve(),
    onTranscribe: () => Promise.resolve(),
    onSummarize: () => Promise.resolve(),
    onExportPdf: () => Promise.resolve(),
    onDiarize: () => Promise.resolve(),
    speakersReady: true,
    activeLlmJobs: [],
    summaryReloadToken: 0,
  };
  return render(<RecordingPage {...defaults} {...props} />);
}

describe("RecordingPage project roster editor", () => {
  it("offers the roster editor for a meeting filed under a project", () => {
    renderPage({ entry: buildEntry({ project: "RDDM" }) });

    expect(screen.getByRole("button", { name: "Project speakers" })).toBeInTheDocument();
  });

  it("offers no roster editor for an unfiled recording", () => {
    renderPage({ entry: buildEntry({ project: null }) });

    expect(screen.queryByRole("button", { name: "Project speakers" })).not.toBeInTheDocument();
  });

  it("opens the roster panel from the breadcrumb button", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    expect(screen.getByRole("radio", { name: /only the roster below/i })).toBeInTheDocument();
  });

  it("closes the roster panel when the button is pressed again", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    expect(screen.queryByRole("radio", { name: /only the roster below/i })).not.toBeInTheDocument();
  });

  it("saves the edited roster for the meeting's project and closes the panel", async () => {
    const onSaveRoster = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({
      entry: buildEntry({ project: "RDDM" }),
      projectRoster: { mode: "open", names: ["Anna"] },
      onSaveRoster,
    });
    await user.click(screen.getByRole("button", { name: "Project speakers" }));
    await user.click(screen.getByRole("radio", { name: /only the roster below/i }));

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSaveRoster).toHaveBeenCalledWith("RDDM", { mode: "roster", names: ["Anna"] });
    expect(screen.queryByRole("radio", { name: /only the roster below/i })).not.toBeInTheDocument();
  });

  it("seeds the roster panel with the names already used in the project", async () => {
    const user = userEvent.setup();
    renderPage({
      projectSpeakers: ["Olga"],
      projectRoster: { mode: "roster", names: [] },
    });
    await user.click(screen.getByRole("button", { name: "Project speakers" }));

    await user.click(
      screen.getByRole("button", { name: /add names already used in this project/i }),
    );

    expect(screen.getByRole("button", { name: "Remove Olga" })).toBeInTheDocument();
  });
});

describe("RecordingPage speaker naming under a roster", () => {
  it("names an unattributed turn from the project roster in roster mode", async () => {
    const user = userEvent.setup();
    renderPage({
      entry: buildEntry({ project: "RDDM" }),
      projectRoster: { mode: "roster", names: ["Anna"] },
    });
    await screen.findByText(/может еще/);

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    const picker = screen.getByRole("combobox", { name: "Name this speaker" });
    expect(within(picker).getByRole("option", { name: "Anna" })).toBeInTheDocument();
  });

  it("keeps the free-text name box on an unfiled recording, which has no project roster", async () => {
    const user = userEvent.setup();
    renderPage({
      entry: buildEntry({ project: null }),
      projectRoster: { mode: "roster", names: ["Anna"] },
    });
    await screen.findByText(/может еще/);

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(screen.getByRole("textbox", { name: "Name this speaker" })).toBeInTheDocument();
  });
});
