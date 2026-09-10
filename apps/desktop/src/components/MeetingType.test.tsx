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
    meeting_name: "260812 - Security issue - Standup",
    meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue - Standup",
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
    meeting_name: "260812 - Security issue - Standup",
    language: "ru",
    created_at: "2026-08-12T10:00:00Z",
    duration_sec: 491,
    model: "large-v3",
    device: "cuda",
    text: "может еще запись включим",
    segments: [{ id: 0, start: 0, end: 4, text: " может еще запись включим" }],
    speakers: { "0": "Maxim" },
    transcript_path: "D:\\Meetings\\ELS\\260812 - Security issue - Standup\\transcript.json",
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
  return render(<VaultRow {...defaults} {...props} />);
}

describe("the meeting type in the rename form", () => {
  it("seeds the type from a meeting filed with one", () => {
    renderEditor();

    expect(screen.getByLabelText(/date/i)).toHaveValue("260812");
    expect(screen.getByLabelText(/title/i)).toHaveValue("Security issue");
    expect(screen.getByLabelText(/type/i)).toHaveValue("Standup");
  });

  it("leaves the type empty for a meeting filed without one", () => {
    renderEditor({ entry: buildEntry({ meeting_name: "260822 - source" }) });

    expect(screen.getByLabelText(/type/i)).toHaveValue("");
  });

  it("saves a meeting with no type without mentioning one", async () => {
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
    });
  });

  it("saves the type the operator typed", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ entry: buildEntry({ meeting_name: "260812 - Security issue" }), onSave });

    await user.type(screen.getByLabelText(/type/i), "Retro");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: "Retro",
    });
  });

  it("trims the spaces around a typed type", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ entry: buildEntry({ meeting_name: "260812 - Security issue" }), onSave });

    await user.type(screen.getByLabelText(/type/i), "  Retro  ");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave).toHaveBeenCalledWith({
      project: "ELS",
      date: "260812",
      title: "Security issue",
      kind: "Retro",
    });
  });

  it("strips an existing type when its field is cleared", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderEditor({ onSave });

    await user.clear(screen.getByLabelText(/type/i));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onSave.mock.calls[0][0]).toStrictEqual({
      project: "ELS",
      date: "260812",
      title: "Security issue",
    });
  });

  it("previews the typed folder the save would produce", () => {
    renderEditor();

    expect(screen.getByText("ELS\\260812 - Security issue - Standup")).toBeInTheDocument();
  });

  it("drops the type from the preview once its field is cleared", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.clear(screen.getByLabelText(/type/i));

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

  it("refuses a type carrying the reserved separator", async () => {
    const user = userEvent.setup();
    renderEditor();

    const type = screen.getByLabelText(/type/i);
    await user.clear(type);
    await user.type(type, "Re-tro");

    expect(type).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/separator/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("allows the save again once the separator is taken back out", async () => {
    const user = userEvent.setup();
    renderEditor();
    const type = screen.getByLabelText(/type/i);
    await user.clear(type);
    await user.type(type, "Re-tro");

    await user.clear(type);
    await user.type(type, "Retro");

    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });
});

describe("the meeting type on the recording page", () => {
  it("shows the type beside the meeting's title", async () => {
    renderPage();
    await screen.findByText(/может еще/);

    expect(screen.getByRole("heading", { name: "Security issue" })).toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
  });

  it("shows nothing beside the title for a meeting filed without a type", async () => {
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

  it("reads no type out of an unsorted folder's verbatim stem", async () => {
    // `unsorted/260910 - ELS - 260812` is a `MissingSeparator` drop of
    // `ELS - 260812.mp4`: the stem is kept verbatim precisely because the
    // name did not conform, so its hyphen is not a separator and there is
    // no type to show.
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

describe("the meeting type in the library row", () => {
  it("tags the row with the meeting's type", () => {
    renderRow();

    expect(screen.getByText("Security issue")).toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
  });

  it("carries no type tag for a meeting filed without one", () => {
    renderRow({ entry: buildEntry({ meeting_name: "260812 - Security issue" }) });

    expect(screen.getByText("Security issue")).toBeInTheDocument();
    expect(screen.queryByText("Standup")).not.toBeInTheDocument();
  });

  it("carries no type tag for an unsorted row, whose stem keeps its hyphens", () => {
    renderRow({ entry: buildEntry({ project: null, meeting_name: "260910 - ELS - 260812" }) });

    expect(screen.getByText("ELS - 260812")).toBeInTheDocument();
    expect(screen.queryByText("260812")).not.toBeInTheDocument();
  });
});
