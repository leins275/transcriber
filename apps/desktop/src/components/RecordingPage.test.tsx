import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingPage } from "./RecordingPage";
import type { SummaryView, TranscriptView, VaultMeetingView } from "../types";

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
    speakers: { "0": "Maxim" },
    transcript_path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\transcript.json",
    ...overrides,
  };
}

const emptySummary: SummaryView = {
  entry_id: "v-1",
  path: "D:\\Meetings\\RDDM\\260709 - tech support 1\\summary.md",
  markdown: null,
};

function renderPage(props: Partial<React.ComponentProps<typeof RecordingPage>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["RDDM", "ELS"],
    onBack: () => {},
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(buildTranscript()),
    onReadSummary: () => Promise.resolve(emptySummary),
    onSaveSpeakers: () => Promise.resolve(),
    onUpdate: () => Promise.resolve(),
    onDelete: () => Promise.resolve(),
    onTranscribe: () => Promise.resolve(),
    onSummarize: () => Promise.resolve(),
    onExtract: () => Promise.resolve(),
    onExportPdf: () => Promise.resolve(),
    activeLlmJobs: [],
    summaryReloadToken: 0,
  };
  return render(<RecordingPage {...defaults} {...props} />);
}

describe("RecordingPage", () => {
  it("titles the page with the meeting's title and shows its project", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "tech support 1" })).toBeInTheDocument();
    expect(screen.getByText("RDDM")).toBeInTheDocument();
    await screen.findByText(/может еще/);
  });

  it("loads the transcript on open and lists its provenance", async () => {
    renderPage();

    expect(await screen.findByText(/может еще/)).toBeInTheDocument();
    // Date, duration, speaker count, model and device on one line; the
    // language gets its own badge (below).
    expect(screen.getByText(/8m 11s/)).toBeInTheDocument();
    expect(screen.getByText(/1 speaker/)).toBeInTheDocument();
    expect(screen.getByText(/large-v3/)).toBeInTheDocument();
  });

  it("names the language a transcript was decoded in", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "en" })) });

    expect(await screen.findByLabelText("Language: English")).toHaveTextContent("English");
  });

  it("names a Russian transcript as Russian", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "ru" })) });

    expect(await screen.findByLabelText("Language: Russian")).toHaveTextContent("Russian");
  });

  it("shows no language indicator and no placeholder for a legacy transcript that recorded none", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: null })) });
    await screen.findByText(/может еще/);

    expect(screen.queryByLabelText(/^Language:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/unknown|—/i)).not.toBeInTheDocument();
  });

  it("shows no indicator for a transcript in a language the app does not name", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "de" })) });
    await screen.findByText(/может еще/);

    expect(screen.queryByLabelText(/^Language:/)).not.toBeInTheDocument();
  });

  it("goes back to the library", async () => {
    const onBack = vi.fn();
    const user = userEvent.setup();
    renderPage({ onBack });

    await user.click(screen.getByRole("button", { name: /recordings/i }));

    expect(onBack).toHaveBeenCalled();
  });

  it("shows the transcript's own path in the footer", async () => {
    renderPage();
    expect(
      await screen.findByText("D:\\Meetings\\RDDM\\260709 - tech support 1\\transcript.json"),
    ).toBeInTheDocument();
  });

  it("does not read a transcript for a recording that has none", () => {
    const onReadTranscript = vi.fn();
    renderPage({ entry: buildEntry({ has_transcript: false }), onReadTranscript });

    expect(onReadTranscript).not.toHaveBeenCalled();
    expect(screen.getByText(/no transcript yet/i)).toBeInTheDocument();
  });

  it("offers Transcribe for a recording with no transcript and Re-transcribe for one with", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    const { unmount } = renderPage();
    expect(screen.getByRole("button", { name: /re-transcribe/i })).toBeInTheDocument();
    unmount();

    renderPage({ entry: buildEntry({ id: "v-9", has_transcript: false }), onTranscribe });
    await user.click(screen.getByRole("button", { name: "Transcribe" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", null);
  });

  it("offers no transcription for a meeting whose recording is gone", () => {
    renderPage({ entry: buildEntry({ has_source: false }) });
    expect(screen.queryByRole("button", { name: /transcribe/i })).not.toBeInTheDocument();
  });

  it("transcribes in Auto by default, sending no language override", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    const control = screen.getByLabelText(/transcript language/i);
    expect(control).toHaveValue("auto");

    await user.click(screen.getByRole("button", { name: /re-transcribe/i }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", null);
  });

  it("re-transcribes in English when the operator picks English", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    await user.selectOptions(screen.getByLabelText(/transcript language/i), "en");
    await user.click(screen.getByRole("button", { name: /re-transcribe/i }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", "en");
  });

  it("re-transcribes in Russian when the operator picks Russian", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    await user.selectOptions(screen.getByLabelText(/transcript language/i), "ru");
    await user.click(screen.getByRole("button", { name: /re-transcribe/i }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", "ru");
  });

  it("offers no language control for a meeting whose recording is gone", () => {
    renderPage({ entry: buildEntry({ has_source: false }) });
    expect(screen.queryByLabelText(/transcript language/i)).not.toBeInTheDocument();
  });

  it("switches to the summary tab and reports there is none", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/может еще/);

    await user.click(screen.getByRole("tab", { name: /summary/i }));

    expect(await screen.findByText(/no summary for this meeting yet/i)).toBeInTheDocument();
  });

  it("saves speaker labels by entry id", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onSaveSpeakers });
    await screen.findByText(/может еще/);

    await user.click(screen.getByRole("button", { name: "Maxim" }));
    const input = screen.getByLabelText(/rename maxim/i);
    await user.clear(input);
    await user.type(input, "Дмитрий{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith("v-9", { "0": "Дмитрий" });
  });

  it("renames the meeting from the page", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onUpdate });

    await user.click(screen.getByRole("button", { name: /rename/i }));
    const title = screen.getByLabelText(/title/i);
    await user.clear(title);
    await user.type(title, "Tech support");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledWith("v-9", {
      project: "RDDM",
      date: "260709",
      title: "Tech support",
    });
  });

  it("asks before deleting, then deletes by id", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onDelete });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(onDelete).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /move to recycle bin/i }));
    expect(onDelete).toHaveBeenCalledWith("v-9");
  });

  it("extracts action items and facts from an unfiled recording", async () => {
    const onExtract = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9", project: null }), onExtract });

    const actionItems = screen.getByRole("button", { name: "Action items" });
    const facts = screen.getByRole("button", { name: "Facts" });
    expect(actionItems).toBeEnabled();
    expect(facts).toBeEnabled();

    await user.click(actionItems);
    await user.click(facts);

    expect(onExtract).toHaveBeenNthCalledWith(1, "v-9", "action_items");
    expect(onExtract).toHaveBeenNthCalledWith(2, "v-9", "facts");
  });

  it("no longer tells an unfiled recording to be filed under a project first", () => {
    renderPage({ entry: buildEntry({ project: null }) });

    expect(screen.getByRole("button", { name: "Action items" })).not.toHaveAttribute("title");
    expect(screen.getByRole("button", { name: "Facts" })).not.toHaveAttribute("title");
    expect(
      screen.queryByTitle(/file this recording under a project first/i),
    ).not.toBeInTheDocument();
  });

  it("disables an extraction button while its own job is in flight", () => {
    renderPage({ entry: buildEntry({ project: null }), activeLlmJobs: ["action_items"] });

    expect(screen.getByRole("button", { name: "Extracting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Facts" })).toBeEnabled();
  });

  it("surfaces a transcript read failure on the page", async () => {
    renderPage({
      onReadTranscript: () => Promise.reject({ kind: "vault", message: "transcript unreadable" }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/transcript unreadable/i);
  });
});
