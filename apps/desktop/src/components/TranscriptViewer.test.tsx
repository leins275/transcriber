import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptViewer } from "./TranscriptViewer";
import type { TranscriptView } from "../types";

function buildTranscript(overrides: Partial<TranscriptView> = {}): TranscriptView {
  return {
    entry_id: "v-1",
    meeting_name: "260822 - source",
    language: "ru",
    created_at: "2026-08-22T15:29:58Z",
    duration_sec: 3625.8,
    model: "large-v3",
    device: "cuda",
    text: "Да, ребят, всем привет. Приветствую.",
    segments: [
      { id: 0, start: 0, end: 2.5, text: " Да, ребят," },
      { id: 1, start: 125, end: 128, text: " всем привет." },
      { id: 2, start: 130, end: 131, text: "   " },
    ],
    speakers: {},
    transcript_path: "D:\\Meetings\\unsorted\\260822 - source\\transcript.json",
    ...overrides,
  };
}

describe("TranscriptViewer", () => {
  it("opens on the timeline with a timecode per turn", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getByText("0:00")).toBeInTheDocument();
    expect(screen.getByText("2:05")).toBeInTheDocument();
  });

  it("renders Cyrillic text as characters, not escapes", () => {
    const { container } = render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getByText(/всем привет\./)).toBeInTheDocument();
    expect(container.innerHTML).not.toContain(String.raw`\u04`);
  });

  it("groups segments into turns rather than one paragraph each", () => {
    // Two segments 122s apart become two turns; the whitespace-only third
    // becomes none at all.
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("says a turn is unattributed instead of inventing a speaker", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getAllByRole("button", { name: /add speaker/i }).length).toBeGreaterThan(0);
  });

  it("shows the speaker on a turn that has one", () => {
    render(
      <TranscriptViewer
        transcript={buildTranscript({ speakers: { "0": "Maxim", "1": "Anna" } })}
      />,
    );
    // The turn's own label, not the "attribute this turn to ..." offers.
    expect(screen.getByRole("button", { name: "Maxim" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Anna" })).toBeInTheDocument();
  });

  it("names an unattributed speaker and saves the whole map", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} onSaveSpeakers={onSaveSpeakers} />);

    await user.click(screen.getAllByRole("button", { name: /add speaker/i })[0]);
    await user.type(screen.getByLabelText(/name this speaker/i), "Maxim{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "0": "Maxim" });
  });

  it("renaming a speaker renames every segment they hold", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ speakers: { "0": "Speaker 2", "1": "Speaker 2" } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    const input = screen.getByLabelText(/rename speaker 2/i);
    await user.clear(input);
    await user.type(input, "Anna{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "0": "Anna", "1": "Anna" });
  });

  it("surfaces a failed save without discarding the label on screen", async () => {
    const onSaveSpeakers = vi
      .fn()
      .mockRejectedValue({ kind: "io", message: "could not write speakers.json" });
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} onSaveSpeakers={onSaveSpeakers} />);

    await user.click(screen.getAllByRole("button", { name: /add speaker/i })[0]);
    await user.type(screen.getByLabelText(/name this speaker/i), "Maxim{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not write/i);
    expect(screen.getByRole("button", { name: "Maxim" })).toBeInTheDocument();
  });

  it("filters the transcript with Find, and restores it when cleared", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} />);

    const find = screen.getByLabelText(/find in transcript/i);
    await user.type(find, "привет");

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText(/1 of 2 passages match/i)).toBeInTheDocument();

    await user.clear(find);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("says so when a search matches nothing", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} />);

    await user.type(screen.getByLabelText(/find in transcript/i), "kubernetes");

    expect(screen.getByText(/nothing in this transcript matches/i)).toBeInTheDocument();
  });

  it("switches to a selectable plain-text view for copying", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} />);

    await user.click(screen.getByRole("button", { name: /plain text/i }));

    const textarea = screen.getByLabelText(/transcript text/i);
    expect(textarea).toHaveValue("Да, ребят, всем привет. Приветствую.");
    // readOnly, not disabled -- a disabled textarea cannot be selected from.
    expect(textarea).not.toBeDisabled();
  });

  it("says so when a transcript has no segments at all", () => {
    render(<TranscriptViewer transcript={buildTranscript({ segments: [] })} />);
    expect(screen.getByText(/no segments/i)).toBeInTheDocument();
  });
});
