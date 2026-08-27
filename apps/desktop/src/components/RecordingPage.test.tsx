import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingPage } from "./RecordingPage";
import type { ActionItemsView, SummaryView, TranscriptView, VaultMeetingView } from "../types";

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

const emptyActionItems: ActionItemsView = {
  entry_id: "v-1",
  dir: "D:\\Meetings\\RDDM\\260709 - tech support 1\\action items",
  items: [],
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
    onReadActionItems: () => Promise.resolve(emptyActionItems),
    onCaptureItemScreenshots: () => Promise.resolve({ written: [], screenshots: [] }),
    onReadItemScreenshots: () => Promise.resolve([]),
    activeLlmJobs: [],
    summaryReloadToken: 0,
    actionItemsReloadToken: 0,
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

  it("names the language a transcript was decoded in, first on the meta line", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "en" })) });
    await screen.findByText(/может еще/);

    expect(screen.getByText(/^English · /)).toBeInTheDocument();
  });

  it("names a Russian transcript as Russian", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "ru" })) });
    await screen.findByText(/может еще/);

    expect(screen.getByText(/^Russian · /)).toBeInTheDocument();
  });

  it("shows no language name and no placeholder for a legacy transcript that recorded none", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: null })) });
    await screen.findByText(/может еще/);

    expect(screen.queryByText(/^Russian · |^English · /)).not.toBeInTheDocument();
    expect(screen.queryByText(/unknown|—/i)).not.toBeInTheDocument();
  });

  it("shows no language name for a transcript in a language the app does not name", async () => {
    renderPage({ onReadTranscript: () => Promise.resolve(buildTranscript({ language: "de" })) });
    await screen.findByText(/может еще/);

    expect(screen.queryByText(/^Russian · |^English · /)).not.toBeInTheDocument();
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

  it("offers Transcribe in the empty transcript panel, sending no language override", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9", has_transcript: false }), onTranscribe });

    await user.click(screen.getByRole("button", { name: "Transcribe" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", null);
  });

  it("offers no transcription for a meeting whose recording is gone", async () => {
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ has_source: false }) });

    expect(screen.queryByRole("button", { name: "Transcribe" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    expect(screen.queryByRole("menuitem", { name: /transcribe/i })).not.toBeInTheDocument();
  });

  it("re-transcribes from the overflow menu in Auto, sending no language override", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Re-transcribe (Auto)" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", null);
    // The menu closed on the click.
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("re-transcribes in English when the operator picks English in the menu", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Re-transcribe in English" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", "en");
  });

  it("re-transcribes in Russian when the operator picks Russian in the menu", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onTranscribe });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Re-transcribe in Russian" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", "ru");
  });

  it("labels the menu's transcribe section Transcribe while no transcript exists", async () => {
    const onTranscribe = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9", has_transcript: false }), onTranscribe });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Transcribe in Russian" }));

    expect(onTranscribe).toHaveBeenCalledWith("v-9", "ru");
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

  it("deletes via the overflow menu, asking first, then deleting by id", async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onDelete });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: /delete recording/i }));
    expect(onDelete).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /move to recycle bin/i }));
    expect(onDelete).toHaveBeenCalledWith("v-9");
  });

  it("reveals in Explorer from the toolbar", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onReveal });

    await user.click(screen.getByRole("button", { name: /reveal in explorer/i }));

    expect(onReveal).toHaveBeenCalledWith("v-9");
  });

  it("exports a PDF from the overflow menu", async () => {
    const onExportPdf = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onExportPdf });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: /export pdf/i }));

    expect(onExportPdf).toHaveBeenCalledWith("v-9");
  });

  it("extracts action items from the empty tab's own Generate button, with no Facts anywhere", async () => {
    const onExtract = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9", project: null }), onExtract });

    // The facts extraction was retired: the summary carries the notable
    // facts, so no Facts control may exist anywhere on the page.
    expect(screen.queryByRole("tab", { name: /facts/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Action items" }));
    await user.click(await screen.findByRole("button", { name: "Extract action items" }));

    expect(onExtract).toHaveBeenCalledWith("v-9", "action_items");
  });

  it("generates a summary from the empty tab's own Generate button", async () => {
    const onSummarize = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onSummarize });

    await user.click(screen.getByRole("tab", { name: "Summary" }));
    await user.click(await screen.findByRole("button", { name: "Generate summary" }));

    expect(onSummarize).toHaveBeenCalledWith("v-9");
  });

  it("regenerates from the overflow menu even when content already exists", async () => {
    const onSummarize = vi.fn().mockResolvedValue(undefined);
    const onExtract = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onSummarize, onExtract });

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Regenerate summary" }));
    expect(onSummarize).toHaveBeenCalledWith("v-9");

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("menuitem", { name: "Re-extract action items" }));
    expect(onExtract).toHaveBeenCalledWith("v-9", "action_items");
  });

  it("no longer tells an unfiled recording to be filed under a project first", () => {
    renderPage({ entry: buildEntry({ project: null }) });

    expect(
      screen.queryByTitle(/file this recording under a project first/i),
    ).not.toBeInTheDocument();
  });

  it("renders the empty tab's Generate button busy while its own job is in flight", async () => {
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ project: null }), activeLlmJobs: ["action_items"] });

    await user.click(screen.getByRole("tab", { name: "Action items" }));

    expect(await screen.findByRole("button", { name: "Extracting…" })).toBeDisabled();
  });

  it("copies the visible tab: enabled on a loaded transcript, disabled on an empty summary", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/может еще/);

    expect(screen.getByRole("button", { name: "Copy" })).toBeEnabled();

    await user.click(screen.getByRole("tab", { name: "Summary" }));
    await screen.findByText(/no summary for this meeting yet/i);

    expect(screen.getByRole("button", { name: "Copy" })).toBeDisabled();
  });

  it("shows the extracted action items in their own tab beside the summary", async () => {
    const user = userEvent.setup();
    const onReadActionItems = vi.fn().mockResolvedValue({
      entry_id: "v-1",
      dir: "D:\\Meetings\\RDDM\\260709 - tech support 1\\action items",
      items: [
        {
          dir_name: "fix-login",
          title: "Fix login",
          item_type: "task",
          body_md: "The login flow breaks on refresh.",
          timestamps: [65],
          archived: false,
          screenshot_names: [],
        },
      ],
    });
    renderPage({ onReadActionItems });

    await user.click(screen.getByRole("tab", { name: "Action items" }));

    expect(await screen.findByRole("heading", { name: "Fix login" })).toBeInTheDocument();
    expect(screen.getByText("task")).toBeInTheDocument();
    expect(screen.getByText(/login flow breaks/)).toBeInTheDocument();
    expect(screen.getByText("1:05")).toBeInTheDocument();
    expect(onReadActionItems).toHaveBeenCalledWith("v-1");
  });

  it("captures screenshots for one item on demand", async () => {
    const user = userEvent.setup();
    const onReadActionItems = vi.fn().mockResolvedValue({
      entry_id: "v-1",
      dir: "dir",
      items: [
        {
          dir_name: "fix-login",
          title: "Fix login",
          item_type: "task",
          body_md: "",
          timestamps: [10],
          archived: false,
          screenshot_names: [],
        },
      ],
    });
    const onCaptureItemScreenshots = vi.fn().mockResolvedValue({
      written: ["screenshot-0010.png"],
      screenshots: ["screenshot-0010.png"],
    });
    const onReadItemScreenshots = vi
      .fn()
      .mockResolvedValue([{ name: "screenshot-0010.png", data_url: "data:image/png;base64,AA==" }]);
    renderPage({ onReadActionItems, onCaptureItemScreenshots, onReadItemScreenshots });

    await user.click(screen.getByRole("tab", { name: "Action items" }));
    await user.click(await screen.findByRole("button", { name: "Capture screenshots" }));

    expect(onCaptureItemScreenshots).toHaveBeenCalledWith("v-1", "fix-login");
    expect(await screen.findByAltText("Screenshot screenshot-0010.png")).toBeInTheDocument();
  });

  it("surfaces a transcript read failure on the page", async () => {
    renderPage({
      onReadTranscript: () => Promise.reject({ kind: "vault", message: "transcript unreadable" }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/transcript unreadable/i);
  });
});
