import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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

/**
 * Five sentence-sized segments running on without a pause: one turn, five
 * selectable sentences — the shape a `resegment`ed transcript actually has,
 * and the one sub-turn markup exists for.
 */
const SENTENCES = [
  { id: 0, start: 0, end: 1.5, text: " Раз." },
  { id: 1, start: 1.5, end: 3, text: " Два." },
  { id: 2, start: 3, end: 4.5, text: " Три." },
  { id: 3, start: 4.5, end: 6, text: " Четыре." },
  { id: 4, start: 6, end: 7.5, text: " Пять." },
];

const ALL_MAXIM = { "0": "Maxim", "1": "Maxim", "2": "Maxim", "3": "Maxim", "4": "Maxim" };

/**
 * Two unattributed turns two minutes apart: the shape needed to say anything
 * about a selection crossing a turn boundary, or about a search that hides
 * one of them.
 */
const TWO_TURNS = [
  { id: 0, start: 0, end: 1.5, text: " Раз." },
  { id: 1, start: 1.5, end: 3, text: " Два." },
  { id: 2, start: 130, end: 131.5, text: " Три." },
  { id: 3, start: 131.5, end: 133, text: " Четыре." },
];

function segmentText(segmentId: string): Text {
  const span = document.querySelector(`[data-segment-id="${segmentId}"]`);
  if (span === null) throw new Error(`no span for segment ${segmentId}`);
  return span.firstChild as Text;
}

/**
 * The operator dragging the pointer over the transcript: a real `Range` over
 * the rendered spans, handed to the document's selection, then the pointer-up
 * the viewer listens for. jsdom has no layout, so a drag is expressed as text
 * offsets rather than coordinates.
 */
function selectText(
  from: { segment: string; offset?: number },
  to: { segment: string; offset?: number },
) {
  const start = segmentText(from.segment);
  const end = segmentText(to.segment);
  const range = document.createRange();
  range.setStart(start, from.offset ?? 0);
  range.setEnd(end, to.offset ?? end.data.length);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent.pointerUp(end.parentElement as HTMLElement);
}

function assignControl() {
  return screen.queryByRole("group", { name: /attribute the selected text/i });
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
});

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

  it("renders each segment of a turn as its own identifiable span", () => {
    // Selection markup needs to know which segments a selection covers, so
    // every segment carries its id in the DOM.
    const { container } = render(
      <TranscriptViewer
        transcript={buildTranscript({
          segments: [
            { id: 0, start: 0, end: 2.5, text: " Да, ребят," },
            { id: 1, start: 2.5, end: 4, text: " всем привет." },
          ],
        })}
      />,
    );

    const spans = Array.from(container.querySelectorAll("[data-segment-id]"));
    expect(spans.map((span) => span.getAttribute("data-segment-id"))).toEqual(["0", "1"]);
    expect(spans.map((span) => span.textContent)).toEqual(["Да, ребят,", "всем привет."]);
  });

  it("keeps the turn's reading text identical to the joined segments", () => {
    const { container } = render(
      <TranscriptViewer
        transcript={buildTranscript({
          segments: [
            { id: 0, start: 0, end: 2.5, text: " Да, ребят," },
            { id: 1, start: 2.5, end: 4, text: " всем привет." },
          ],
        })}
      />,
    );

    const paragraph = container.querySelector("p");
    expect(paragraph?.textContent).toBe("Да, ребят, всем привет.");
  });

  it("renders no span for a whitespace-only segment", () => {
    const { container } = render(<TranscriptViewer transcript={buildTranscript()} />);

    expect(container.querySelector('[data-segment-id="2"]')).toBeNull();
    expect(container.querySelectorAll("[data-segment-id]")).toHaveLength(2);
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

  it("offers the known speakers and a new name for text selected inside a turn", () => {
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { "0": "Maxim" } })}
      />,
    );

    selectText({ segment: "2" }, { segment: "3" });

    expect(
      screen.getByRole("button", { name: /attribute selection to maxim/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /new speaker/i })).toBeInTheDocument();
  });

  it("offers nothing for a caret rather than a selection", () => {
    render(<TranscriptViewer transcript={buildTranscript({ segments: SENTENCES })} />);

    selectText({ segment: "2", offset: 2 }, { segment: "2", offset: 2 });

    expect(assignControl()).toBeNull();
  });

  it("attributes exactly the selected segments, keeping the rest of the map", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({
      "0": "Maxim",
      "1": "Anna",
      "2": "Anna",
      "3": "Maxim",
      "4": "Maxim",
    });
  });

  it("snaps a selection that starts and ends mid-sentence out to the whole segment", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { "0": "Maxim" } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "2", offset: 1 }, { segment: "2", offset: 2 });
    await user.click(screen.getByRole("button", { name: /attribute selection to maxim/i }));

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "0": "Maxim", "2": "Maxim" });
  });

  it("leaves out a sentence the drag only ran up to", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    // A drag released just past "Три." is normalized by the browser to offset
    // 0 of the next sentence's text node: not one character of segment 3 is
    // covered, so it is not part of what the operator dragged over.
    selectText({ segment: "1" }, { segment: "3", offset: 0 });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({
      "0": "Maxim",
      "1": "Anna",
      "2": "Anna",
      "3": "Maxim",
      "4": "Maxim",
    });
  });

  it("offers the control for a drag released past the end of the list", () => {
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { "0": "Maxim" } })}
      />,
    );

    const start = segmentText("2");
    const end = segmentText("4");
    const range = document.createRange();
    range.setStart(start, 0);
    range.setEnd(end, end.data.length);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);

    // Overshooting is how a paragraph gets selected to its end: the press
    // lands on the text, the release lands in the margin below the list.
    fireEvent.pointerDown(start.parentElement as HTMLElement);
    fireEvent.pointerUp(document.body);

    expect(assignControl()).not.toBeNull();
  });

  it("re-groups around the reassigned sentence, leaving the flanks as they were", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript({ segments: SENTENCES })} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);

    selectText({ segment: "2" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Anna" })).toBeInTheDocument();
    // The flanks were nobody's, and stay nobody's.
    expect(screen.getAllByRole("button", { name: /add speaker/i })).toHaveLength(2);
  });

  it("closes the assign control once the attribution is made", async () => {
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { "0": "Maxim" } })}
      />,
    );

    selectText({ segment: "2" }, { segment: "3" });
    await user.click(screen.getByRole("button", { name: /attribute selection to maxim/i }));

    expect(assignControl()).toBeNull();
  });

  it("keeps a sub-turn attribution on screen when its save fails", async () => {
    const onSaveSpeakers = vi
      .fn()
      .mockRejectedValue({ kind: "io", message: "could not write speakers.json" });
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "2" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not write/i);
    expect(screen.getByRole("button", { name: "Anna" })).toBeInTheDocument();
  });

  it("closes the assign control on Escape without changing any attribution", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    expect(assignControl()).not.toBeNull();

    await user.keyboard("{Escape}");

    expect(assignControl()).toBeNull();
    expect(onSaveSpeakers).not.toHaveBeenCalled();
    // Nothing changed hands, so the turn is still the one turn it was.
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("closes the assign control when the operator clicks away", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    expect(assignControl()).not.toBeNull();

    await user.click(screen.getByLabelText(/find in transcript/i));

    expect(assignControl()).toBeNull();
    expect(onSaveSpeakers).not.toHaveBeenCalled();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("closes the assign control when the selection is cleared", () => {
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    expect(assignControl()).not.toBeNull();

    // The operator clicks once inside the paragraph: the highlight collapses
    // to a caret, and with it goes the offer.
    selectText({ segment: "1", offset: 2 }, { segment: "1", offset: 2 });

    expect(assignControl()).toBeNull();
  });

  it("attributes a selection that spans two turns to the one speaker", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: TWO_TURNS })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(2);

    // From inside the last sentence of turn A to inside the first of turn B.
    selectText({ segment: "1", offset: 1 }, { segment: "2", offset: 1 });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "1": "Anna", "2": "Anna" });
  });

  it("assigns inside a filtered transcript without touching the hidden turns", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: TWO_TURNS })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    await user.type(screen.getByLabelText(/find in transcript/i), "Три");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText(/1 of 2 passages match/i)).toBeInTheDocument();

    selectText({ segment: "2" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    // Only the selected segment, though the hidden turn's segments sit on
    // either side of it in the transcript.
    expect(onSaveSpeakers).toHaveBeenCalledWith({ "2": "Anna" });
    // The split turn re-grouped and the count re-counted, filter still on.
    expect(screen.getByText(/1 of 3 passages match/i)).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Anna" })).toBeInTheDocument();
  });

  it("renaming a speaker after a sub-turn assignment renames the new segments too", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");

    await user.click(screen.getByRole("button", { name: "Anna" }));
    const input = screen.getByLabelText(/rename anna/i);
    await user.clear(input);
    await user.type(input, "Anya{Enter}");

    expect(onSaveSpeakers).toHaveBeenLastCalledWith({
      "0": "Maxim",
      "1": "Anya",
      "2": "Anya",
      "3": "Maxim",
      "4": "Maxim",
    });
  });

  it("still attributes a whole turn from its tag after a sub-turn split", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, speakers: { ...ALL_MAXIM } })}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "2" }, { segment: "2" });
    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "Anna{Enter}");
    expect(screen.getAllByRole("listitem")).toHaveLength(3);

    // The trailing Maxim turn -- segments 3-4, one of the two the split
    // produced -- handed over wholesale from its own tag.
    const offers = screen.getAllByRole("button", { name: /attribute this turn to anna/i });
    await user.click(offers[offers.length - 1]);

    expect(onSaveSpeakers).toHaveBeenLastCalledWith({
      "0": "Maxim",
      "1": "Maxim",
      "2": "Anna",
      "3": "Anna",
      "4": "Anna",
    });
  });
});
