/**
 * "Suggest title from summary": the language model proposes a short name
 * for a meeting, and the operator confirms it in the existing rename form.
 * These tests pin the UI half — where the action is offered, how a finished
 * suggestion reaches the form, and that nothing is ever renamed without the
 * operator saving.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobRow } from "./JobRow";
import { MeetingEditor } from "./MeetingEditor";
import { RecordingPage } from "./RecordingPage";
import { activeJobView } from "../lib/activeJob";
import type {
  JobSnapshot,
  NoteView,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
} from "../types";

const MEETING_DIR = "D:\\Meetings\\RDDM\\260731 - Запись встречи 31.07.2026 11 04 56";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "v-1",
    project: "RDDM",
    meeting_name: "260731 - Запись встречи 31.07.2026 11 04 56",
    meeting_dir: MEETING_DIR,
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

const transcript: TranscriptView = {
  entry_id: "v-1",
  meeting_name: "260731 - Запись встречи 31.07.2026 11 04 56",
  language: "ru",
  created_at: "2026-07-31T11:04:56Z",
  duration_sec: 491,
  model: "large-v3",
  device: "cuda",
  text: "обсудили бюджет",
  segments: [{ id: 0, start: 0, end: 4, text: " обсудили бюджет" }],
  speakers: {},
  transcript_path: `${MEETING_DIR}\\transcript.json`,
};

const summary: SummaryView = {
  entry_id: "v-1",
  path: `${MEETING_DIR}\\summary.md`,
  markdown: "## Overview\n\nWe reviewed the budget.",
};

const noSummary: SummaryView = { ...summary, markdown: null };

const emptyNote: NoteView = {
  entry_id: "v-1",
  path: `${MEETING_DIR}\\note.md`,
  markdown: null,
};

type PageProps = React.ComponentProps<typeof RecordingPage>;

function pageProps(props: Partial<PageProps> = {}): PageProps {
  return {
    entry: buildEntry(),
    projects: ["RDDM", "ELS"],
    projectSpeakers: [],
    onBack: () => {},
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(transcript),
    onReadSummary: () => Promise.resolve(summary),
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
    onSuggestTitle: () => Promise.resolve(),
    llmReady: true,
    ...props,
  };
}

function renderPage(props: Partial<PageProps> = {}) {
  return render(<RecordingPage {...pageProps(props)} />);
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText(/обсудили бюджет/);
  await user.click(screen.getByRole("button", { name: /more actions/i }));
}

const ITEM = "Suggest title from summary";

describe("RecordingPage: the Suggest title action", () => {
  it("is offered when the meeting has a summary and a language model is installed", async () => {
    const user = userEvent.setup();
    renderPage();
    // The summary is looked up in the background; wait for the answer.
    await waitFor(async () => {
      await openMenu(user);
      expect(screen.getByRole("menuitem", { name: ITEM })).toBeEnabled();
    });
  });

  it("asks for a suggestion by entry id and closes the menu", async () => {
    const onSuggestTitle = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onSuggestTitle });
    await screen.findByText(/обсудили бюджет/);
    await waitFor(() => expect(onSuggestTitle).not.toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: ITEM }));

    expect(onSuggestTitle).toHaveBeenCalledWith("v-9");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("is not offered for a meeting without a summary", async () => {
    const onReadSummary = vi.fn().mockResolvedValue(noSummary);
    const user = userEvent.setup();
    renderPage({ onReadSummary });
    await waitFor(() => expect(onReadSummary).toHaveBeenCalledWith("v-1"));

    await openMenu(user);

    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
  });

  it("is not offered for a summary that is only whitespace", async () => {
    const onReadSummary = vi.fn().mockResolvedValue({ ...summary, markdown: "  \n\n" });
    const user = userEvent.setup();
    renderPage({ onReadSummary });
    await waitFor(() => expect(onReadSummary).toHaveBeenCalled());

    await openMenu(user);

    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
  });

  it("is not offered — and the summary is not even looked up — without a language model", async () => {
    const onReadSummary = vi.fn().mockResolvedValue(summary);
    const user = userEvent.setup();
    renderPage({ llmReady: false, onReadSummary });

    await openMenu(user);

    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
    expect(onReadSummary).not.toHaveBeenCalled();
  });

  it("is not offered when the summary cannot be read", async () => {
    const onReadSummary = vi.fn().mockRejectedValue({ kind: "vault", message: "unreadable" });
    const user = userEvent.setup();
    renderPage({ onReadSummary });
    await waitFor(() => expect(onReadSummary).toHaveBeenCalled());

    await openMenu(user);

    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();
  });

  it("appears once a summarize job has written the summary", async () => {
    let current = noSummary;
    const onReadSummary = vi.fn(() => Promise.resolve(current));
    const user = userEvent.setup();
    const view = renderPage({ onReadSummary });
    await openMenu(user);
    expect(screen.queryByRole("menuitem", { name: /suggest/i })).not.toBeInTheDocument();

    current = summary;
    view.rerender(<RecordingPage {...pageProps({ onReadSummary, summaryReloadToken: 1 })} />);

    expect(await screen.findByRole("menuitem", { name: ITEM })).toBeEnabled();
  });

  it("renders busy, with a running label, while its job is in flight", async () => {
    const onSuggestTitle = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ activeLlmJobs: ["suggest_title"], onSuggestTitle });
    await openMenu(user);

    const item = await screen.findByRole("menuitem", { name: "Suggesting title…" });
    expect(item).toBeDisabled();
    await user.click(item);
    expect(onSuggestTitle).not.toHaveBeenCalled();
  });

  it("surfaces a refused request on the page", async () => {
    const onSuggestTitle = vi
      .fn()
      .mockRejectedValue({ kind: "invalid_argument", message: "has no summary yet" });
    const user = userEvent.setup();
    renderPage({ onSuggestTitle });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: ITEM }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/has no summary yet/);
  });
});

describe("RecordingPage: a finished title suggestion", () => {
  const suggestion = { jobId: "job-7", title: "Обзор бюджета на квартал" };

  it("opens the rename form with the title prefilled and renames nothing by itself", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const view = renderPage({ onUpdate });
    await screen.findByText(/обсудили бюджет/);
    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();

    view.rerender(<RecordingPage {...pageProps({ onUpdate, titleSuggestion: suggestion })} />);

    expect(await screen.findByRole("form", { name: /rename meeting/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/title/i)).toHaveValue("Обзор бюджета на квартал");
    expect(screen.getByText(/suggested from the summary/i)).toBeInTheDocument();
    // The operator has not confirmed anything yet.
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("renames through the existing rename path only when the operator saves", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ entry: buildEntry({ id: "v-9" }), onUpdate, titleSuggestion: suggestion });

    await user.click(await screen.findByRole("button", { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith("v-9", {
      project: "RDDM",
      date: "260731",
      title: "Обзор бюджета на квартал",
    });
  });

  it("leaves the meeting's date and type as they are", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({
      entry: buildEntry({ id: "v-9", meeting_name: "260731 - Запись встречи - Standup" }),
      onUpdate,
      titleSuggestion: suggestion,
    });

    await screen.findByRole("form", { name: /rename meeting/i });
    expect(screen.getByLabelText(/date/i)).toHaveValue("260731");
    expect(screen.getByLabelText(/type/i)).toHaveValue("Standup");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledWith("v-9", {
      project: "RDDM",
      date: "260731",
      title: "Обзор бюджета на квартал",
      kind: "Standup",
    });
  });

  it("lets the operator edit the suggestion before saving", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ onUpdate, titleSuggestion: suggestion });

    const title = await screen.findByLabelText(/title/i);
    await user.clear(title);
    await user.type(title, "Бюджет Q3");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledWith("v-1", {
      project: "RDDM",
      date: "260731",
      title: "Бюджет Q3",
    });
  });

  it("is dropped on Cancel: nothing is renamed and the form reopens on the current name", async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage({ onUpdate, titleSuggestion: suggestion });

    await user.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("form", { name: /rename meeting/i })).not.toBeInTheDocument();
    expect(onUpdate).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(screen.getByLabelText(/title/i)).toHaveValue("Запись встречи 31.07.2026 11 04 56");
    expect(screen.queryByText(/suggested from the summary/i)).not.toBeInTheDocument();
  });

  it("reports the suggestion as shown, once", async () => {
    const onTitleSuggestionShown = vi.fn();
    const view = renderPage({ titleSuggestion: suggestion, onTitleSuggestionShown });
    await screen.findByRole("form", { name: /rename meeting/i });

    // The same suggestion coming round again (a re-render) is not news.
    view.rerender(
      <RecordingPage
        {...pageProps({ titleSuggestion: { ...suggestion }, onTitleSuggestionShown })}
      />,
    );

    expect(onTitleSuggestionShown).toHaveBeenCalledTimes(1);
    expect(onTitleSuggestionShown).toHaveBeenCalledWith("job-7");
  });

  it("keeps the form open once the app stops offering the suggestion", async () => {
    const view = renderPage({ titleSuggestion: suggestion });
    await screen.findByRole("form", { name: /rename meeting/i });

    view.rerender(<RecordingPage {...pageProps({ titleSuggestion: null })} />);

    expect(screen.getByLabelText(/title/i)).toHaveValue("Обзор бюджета на квартал");
  });

  it("replaces an earlier suggestion in the open form with a newer one", async () => {
    const view = renderPage({ titleSuggestion: suggestion });
    await screen.findByRole("form", { name: /rename meeting/i });

    view.rerender(
      <RecordingPage
        {...pageProps({ titleSuggestion: { jobId: "job-8", title: "Планы на квартал" } })}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText(/title/i)).toHaveValue("Планы на квартал"));
  });
});

describe("MeetingEditor: a suggested title", () => {
  function renderEditor(props: Partial<React.ComponentProps<typeof MeetingEditor>> = {}) {
    return render(
      <MeetingEditor
        entry={buildEntry({ meeting_name: "260731 - Запись встречи - Standup" })}
        projects={["RDDM"]}
        onSave={() => Promise.resolve()}
        onCancel={() => {}}
        {...props}
      />,
    );
  }

  it("seeds only the title field, keeping the date and the type", () => {
    renderEditor({ suggestedTitle: "Budget review" });

    expect(screen.getByLabelText(/title/i)).toHaveValue("Budget review");
    expect(screen.getByLabelText(/date/i)).toHaveValue("260731");
    expect(screen.getByLabelText(/type/i)).toHaveValue("Standup");
    expect(screen.getByText(/RDDM\\260731 - Budget review - Standup/)).toBeInTheDocument();
  });

  it("names the title it would replace", () => {
    renderEditor({ suggestedTitle: "Budget review" });

    expect(screen.getByText(/suggested from the summary/i)).toHaveTextContent("Запись встречи");
  });

  it("falls back to the current name without a suggestion", () => {
    renderEditor({ suggestedTitle: null });

    expect(screen.getByLabelText(/title/i)).toHaveValue("Запись встречи");
    expect(screen.queryByText(/suggested from the summary/i)).not.toBeInTheDocument();
  });

  it("still refuses a suggestion carrying the reserved separator", () => {
    // The service strips `-` from every suggestion; if one ever got
    // through, the form must hold the line rather than rename.
    renderEditor({ suggestedTitle: "Budget - review" });

    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    expect(screen.getByText(/is the separator/i)).toBeInTheDocument();
  });
});

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-7",
    source_path: MEETING_DIR,
    job_type: "suggest_title",
    file_name: "260731 - Запись встречи",
    state: "running",
    classification: null,
    meeting_dir: MEETING_DIR,
    source_dest: null,
    transcript_path: null,
    progress: null,
    phase: "writing title",
    message: null,
    error_kind: null,
    created_at: "2026-07-31T12:00:00Z",
    ...overrides,
  };
}

describe("a suggest_title job in the job list and the header", () => {
  it("narrates a running job with its own verb and phase", () => {
    render(<JobRow job={buildJob()} onReveal={() => {}} />);

    expect(screen.getByText("Suggesting a title · writing title")).toBeInTheDocument();
  });

  it("reports a failure like every other LLM job: its own line plus the service's message", () => {
    render(
      <JobRow
        job={buildJob({
          state: "failed",
          phase: null,
          error_kind: "llm_output",
          message: "the model produced no usable title; try again or rename by hand",
        })}
        onReveal={() => {}}
      />,
    );

    expect(
      screen.getByText("Title suggestion failed — the meeting keeps its name."),
    ).toBeInTheDocument();
    expect(screen.getByText(/produced no usable title/)).toBeInTheDocument();
  });

  it("is named in the header chip", () => {
    expect(activeJobView([buildJob()])).toEqual({
      label: "Suggesting a title for “Запись встречи”",
      percent: null,
      phase: "writing title",
    });
  });
});
