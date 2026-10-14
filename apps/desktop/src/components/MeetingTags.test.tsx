import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MeetingEditor } from "./MeetingEditor";
import { RecordingPage } from "./RecordingPage";
import { VaultRow } from "./VaultRow";
import type { NoteView, SummaryView, TranscriptView, VaultMeetingView } from "../types";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: "ELS",
    meeting_name: "260812 - Security issue - Standup, Q3",
    meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue - Standup, Q3",
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

function renderEditor(props: Partial<React.ComponentProps<typeof MeetingEditor>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["ELS", "GIS"],
    onSave: () => Promise.resolve(),
    onCancel: () => {},
  };
  return render(<MeetingEditor {...defaults} {...props} />);
}

function buildTranscript(overrides: Partial<TranscriptView> = {}): TranscriptView {
  return {
    entry_id: "v-1",
    meeting_name: "260812 - Security issue - Standup, Q3",
    language: "ru",
    created_at: "2026-08-12T10:00:00Z",
    duration_sec: 491,
    model: "large-v3",
    device: "cuda",
    text: "может еще запись включим",
    segments: [{ id: 0, start: 0, end: 4, text: " может еще запись включим" }],
    speakers: { "0": "Maxim" },
    transcript_path: "D:\\Meetings\\ELS\\260812 - Security issue - Standup, Q3\\transcript.json",
    ...overrides,
  };
}

function buildSummary(): SummaryView {
  return { entry_id: "v-1", path: "D:\\Meetings\\ELS\\summary.md", markdown: null };
}

function buildNote(): NoteView {
  return { entry_id: "v-1", path: "D:\\Meetings\\ELS\\note.md", markdown: null };
}

function renderPage(props: Partial<React.ComponentProps<typeof RecordingPage>> = {}) {
  const defaults = {
    entry: buildEntry(),
    projects: ["ELS", "GIS"],
    projectSpeakers: [],
    onBack: () => {},
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(buildTranscript()),
    onReadSummary: () => Promise.resolve(buildSummary()),
    onReadNote: () => Promise.resolve(buildNote()),
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

function renderRow(props: Partial<React.ComponentProps<typeof VaultRow>> = {}) {
  const defaults = {
    entry: buildEntry(),
    onOpen: () => {},
  };
  return render(
    <table>
      <tbody>
        <VaultRow {...defaults} {...props} />
      </tbody>
    </table>,
  );
}

describe("the meeting tags in the rename form", () => {
  it("seeds the tags, comma-separated, from a meeting filed with them", () => {
    renderEditor();

    expect(screen.getByLabelText(/date/i)).toHaveValue("260812");
    expect(screen.getByLabelText(/title/i)).toHaveValue("Security issue");
    expect(screen.getByLabelText(/tags/i)).toHaveValue("Standup, Q3");
  });

  it("leaves the tags empty for a meeting filed without any", () => {
    renderEditor({ entry: buildEntry({ meeting_name: "260822 - source" }) });

    expect(screen.getByLabelText(/tags/i)).toHaveValue("");
  });

  it("saves a meeting with no tags as an empty list", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ entry: buildEntry({ meeting_name: "260822 - source" }), onSave });

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    // The key is absent, not undefined: the rename payload is compared with
    // exact equality elsewhere in the suite.
    expect(onSave.mock.calls[0][0]).toStrictEqual({
      project: "ELS",
      date: "260822",
      title: "source",
      tags: [],
    });
  });

  it("saves the tags the operator typed, split on commas", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ entry: buildEntry({ meeting_name: "260812 - Security issue" }), onSave });

    await user.type(screen.getByLabelText(/tags/i), "Retro, Q3,Planning");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      tags: ["Retro", "Q3", "Planning"],
    });
  });

  it("trims the spaces around a typed tag", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ entry: buildEntry({ meeting_name: "260812 - Security issue" }), onSave });

    await user.type(screen.getByLabelText(/tags/i), "  Retro  ");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      tags: ["Retro"],
    });
  });

  it("strips the existing tags when their field is cleared", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ onSave });

    await user.clear(screen.getByLabelText(/tags/i));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave.mock.calls[0][0]).toStrictEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      tags: [],
    });
  });

  it("previews the tagged folder the save would produce", () => {
    renderEditor();

    expect(screen.getByText("ELS\\260812 - Security issue - Standup, Q3")).toBeInTheDocument();
  });

  it("drops the tags from the preview once their field is cleared", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.clear(screen.getByLabelText(/tags/i));

    expect(screen.getByText("ELS\\260812 - Security issue")).toBeInTheDocument();
  });

  it("refuses a title carrying the reserved separator", async () => {
    const user = userEvent.setup();
    renderEditor();

    const title = screen.getByLabelText(/title/i);
    await user.clear(title);
    await user.type(title, "Q3 - review");

    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/separator/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("refuses a tag carrying the reserved separator", async () => {
    const user = userEvent.setup();
    renderEditor();

    const tagsField = screen.getByLabelText(/tags/i);
    await user.clear(tagsField);
    await user.type(tagsField, "Re-tro");

    expect(tagsField).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/separator/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("allows the save again once the separator is taken back out", async () => {
    const user = userEvent.setup();
    renderEditor();
    const tagsField = screen.getByLabelText(/tags/i);
    await user.clear(tagsField);
    await user.type(tagsField, "Re-tro");

    await user.clear(tagsField);
    await user.type(tagsField, "Retro");

    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });
});

describe("the meeting tags on the recording page", () => {
  it("shows every tag beside the meeting's title", async () => {
    renderPage();
    await screen.findByText(/может еще/);

    expect(screen.getByRole("heading", { name: "Security issue" })).toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("Q3")).toBeInTheDocument();
  });

  it("shows nothing beside the title for a meeting filed without tags", async () => {
    renderPage({ entry: buildEntry({ meeting_name: "260812 - Security issue" }) });
    await screen.findByText(/может еще/);

    expect(screen.getByRole("heading", { name: "Security issue" })).toBeInTheDocument();
    expect(screen.queryByText("Standup")).not.toBeInTheDocument();
  });

  it("titles a folder the convention no longer covers with its whole name", async () => {
    renderPage({
      entry: buildEntry({ meeting_name: "260731 - Запись встречи 31.07.2026 11-04-56 - запись" }),
    });
    await screen.findByText(/может еще/);

    expect(
      screen.getByRole("heading", { name: "260731 - Запись встречи 31.07.2026 11-04-56 - запись" }),
    ).toBeInTheDocument();
    // The formatted date sits between the language and the duration when a
    // folder name parses; this one has none to show.
    expect(screen.getByText(/^Russian · 8m 11s/)).toBeInTheDocument();
  });

  it("reads no tags out of an unsorted folder's verbatim stem", async () => {
    // `unsorted/260910 - ELS - 260812` is a `MissingSeparator` drop of
    // `ELS - 260812.mp4`: the stem is kept verbatim precisely because the
    // name did not conform, so its hyphen is not a separator and there is
    // tags to show.
    renderPage({
      entry: buildEntry({ project: null, meeting_name: "260910 - ELS - 260812" }),
    });
    await screen.findByText(/может еще/);

    expect(screen.getByRole("heading", { name: "ELS - 260812" })).toBeInTheDocument();
    expect(screen.queryByText("260812")).not.toBeInTheDocument();
    // The ingest date the unsorted folder does carry is still shown
    // (rendered in the host's locale, so assert only that it is there).
    expect(screen.getByText(/^Russian · [^·]*2026[^·]* · 8m 11s/)).toBeInTheDocument();
  });
});

describe("the meeting tags in the library row", () => {
  it("tags the row with each of the meeting's tags", () => {
    renderRow();

    expect(screen.getByText("Security issue")).toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("Q3")).toBeInTheDocument();
  });

  it("carries no tag for a meeting filed without any", () => {
    renderRow({ entry: buildEntry({ meeting_name: "260812 - Security issue" }) });

    expect(screen.getByText("Security issue")).toBeInTheDocument();
    expect(screen.queryByText("Standup")).not.toBeInTheDocument();
  });

  it("carries no tag for an unsorted row, whose stem keeps its hyphens", () => {
    renderRow({ entry: buildEntry({ project: null, meeting_name: "260910 - ELS - 260812" }) });

    expect(screen.getByText("ELS - 260812")).toBeInTheDocument();
    expect(screen.queryByText("260812")).not.toBeInTheDocument();
  });
});
