/**
 * The compressed-video marker and the manual "Compress video" action on
 * the recording page: what the meta line says for each `video_compressed`
 * value, and when the menu offers which label.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingPage } from "./RecordingPage";
import type { NoteView, SummaryView, TranscriptView, VaultMeetingView } from "../types";

const MEETING_DIR = "D:\\Meetings\\ELS\\260812 - Demo";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: "ELS",
    meeting_name: "260812 - Demo",
    meeting_dir: MEETING_DIR,
    has_source: true,
    has_transcript: true,
    video_compressed: false,
    ...overrides,
  };
}

const transcript: TranscriptView = {
  entry_id: "v-1",
  meeting_name: "260812 - Demo",
  language: "en",
  created_at: "2026-08-12T11:04:56Z",
  duration_sec: 491,
  model: "large-v3",
  device: "cuda",
  text: "we reviewed the budget",
  segments: [{ id: 0, start: 0, end: 4, text: " we reviewed the budget" }],
  speakers: {},
  transcript_path: `${MEETING_DIR}\\transcript.json`,
};

const summary: SummaryView = {
  entry_id: "v-1",
  path: `${MEETING_DIR}\\summary.md`,
  markdown: null,
};

const note: NoteView = { entry_id: "v-1", path: `${MEETING_DIR}\\note.md`, markdown: null };

type PageProps = React.ComponentProps<typeof RecordingPage>;

function renderPage(props: Partial<PageProps> = {}) {
  return render(
    <RecordingPage
      entry={buildEntry()}
      projects={["ELS"]}
      projectSpeakers={[]}
      onBack={() => {}}
      onReveal={() => {}}
      onReadTranscript={() => Promise.resolve(transcript)}
      onReadSummary={() => Promise.resolve(summary)}
      onReadNote={() => Promise.resolve(note)}
      onSaveNote={() => Promise.resolve()}
      onSaveSpeakers={() => Promise.resolve()}
      onUpdate={() => Promise.resolve()}
      onDelete={() => Promise.resolve()}
      onTranscribe={() => Promise.resolve()}
      onSummarize={() => Promise.resolve()}
      onExportPdf={() => Promise.resolve()}
      onCompressVideo={() => Promise.resolve()}
      onDiarize={() => Promise.resolve()}
      speakersReady
      activeLlmJobs={[]}
      summaryReloadToken={0}
      {...props}
    />,
  );
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /more actions/i }));
}

describe("RecordingPage: the compressed-video marker", () => {
  it("says the video is not compressed", async () => {
    renderPage({ entry: buildEntry({ video_compressed: false }) });
    expect(await screen.findByText(/Video not compressed/)).toBeInTheDocument();
  });

  it("says the video is compressed", async () => {
    renderPage({ entry: buildEntry({ video_compressed: true }) });
    expect(await screen.findByText(/Video compressed/)).toBeInTheDocument();
    expect(screen.queryByText(/not compressed/)).not.toBeInTheDocument();
  });

  it("shows the marker for a video that has no transcript yet", () => {
    renderPage({ entry: buildEntry({ has_transcript: false, video_compressed: false }) });
    expect(screen.getByText(/Video not compressed/)).toBeInTheDocument();
  });

  it.each([null, undefined])("says nothing without a video recording (%s)", async (value) => {
    renderPage({ entry: buildEntry({ video_compressed: value }) });
    await screen.findByText(/we reviewed the budget/);
    expect(screen.queryByText(/compressed/i)).not.toBeInTheDocument();
  });
});

describe("RecordingPage: the Compress video action", () => {
  it("is offered as 'Compress video' while the video is not compressed", async () => {
    const onCompressVideo = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9", video_compressed: false }), onCompressVideo });

    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: "Compress video" }));

    expect(onCompressVideo).toHaveBeenCalledWith("v-9");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("is offered as 'Compress again' once the video is compressed", async () => {
    const onCompressVideo = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ video_compressed: true }), onCompressVideo });

    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: "Compress video" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Compress again" }));

    expect(onCompressVideo).toHaveBeenCalledWith("v-1");
  });

  it("does not need a transcript", async () => {
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ has_transcript: false, video_compressed: false }) });

    await openMenu(user);
    expect(screen.getByRole("menuitem", { name: "Compress video" })).toBeEnabled();
  });

  it("is hidden without a video recording", async () => {
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ video_compressed: null }) });

    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: /compress/i })).not.toBeInTheDocument();
  });

  it("is hidden when the app wires no handler", async () => {
    const user = userEvent.setup();
    renderPage({ onCompressVideo: undefined });

    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: /compress/i })).not.toBeInTheDocument();
  });

  it("is busy while a compress job runs for the recording", async () => {
    const onCompressVideo = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ activeLlmJobs: ["compress"], onCompressVideo });

    await openMenu(user);
    expect(screen.getByRole("menuitem", { name: "Compressing video…" })).toBeDisabled();
  });

  it("shows the refusal beside the page when the request fails", async () => {
    const onCompressVideo = vi
      .fn()
      .mockRejectedValue({ kind: "invalid_argument", message: "no video recording to compress" });
    const user = userEvent.setup();
    renderPage({ onCompressVideo });

    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: "Compress video" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("no video recording to compress");
  });
});
