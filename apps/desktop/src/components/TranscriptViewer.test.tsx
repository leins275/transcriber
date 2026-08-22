import { describe, expect, it } from "vitest";
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
    ...overrides,
  };
}

describe("TranscriptViewer", () => {
  it("opens on the timeline with a timecode per segment", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getByText("0:00")).toBeInTheDocument();
    expect(screen.getByText("2:05")).toBeInTheDocument();
    expect(screen.getByText("Да, ребят,")).toBeInTheDocument();
  });

  it("renders Cyrillic text as characters, not escapes", () => {
    const { container } = render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getByText("всем привет.")).toBeInTheDocument();
    // A transcript written before F2 dropped `ensure_ascii` would surface
    // its escapes here rather than letters.
    expect(container.innerHTML).not.toContain(String.raw`\u04`);
  });

  it("drops whitespace-only segments from the timeline", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("shows provenance the transcript carries", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);
    expect(screen.getByText("RU")).toBeInTheDocument();
    expect(screen.getByText("large-v3")).toBeInTheDocument();
    expect(screen.getByText("cuda")).toBeInTheDocument();
    expect(screen.getByText("1h 0m")).toBeInTheDocument();
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
