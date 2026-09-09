import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptViewer } from "./TranscriptViewer";
import type { ProjectRosterView, TranscriptView } from "../types";

/**
 * A voice the service could not put a name to, seen from the viewer under a
 * strict project roster.
 *
 * Diarization labels its clusters `Speaker 1`, `Speaker 2`, … — a count, not
 * a person. In a project whose roster is the whole cast, showing that count
 * as if it were a name invents someone who is not on the roster. The
 * transcript still carries the raw label (it is what holds a turn together),
 * so these tests watch the two places it could leak into the UI: the tag over
 * a turn, and every list of names the operator can pick from.
 *
 * Open mode is untouched — the last tests here are the controls that say so.
 */

/** Four sentences, each its own segment, so a speaker map can cut them into
 * turns any way a case needs. */
const SENTENCES = [
  { id: 0, start: 0, end: 1.5, text: " Раз." },
  { id: 1, start: 1.5, end: 3, text: " Два." },
  { id: 2, start: 3, end: 4.5, text: " Три." },
  { id: 3, start: 4.5, end: 6, text: " Четыре." },
];

/** Two generic clusters and one named person: `Speaker 1` runs over the first
 * two sentences, `Speaker 2` over the third, "Anna" over the last. */
const SEEDED_SPEAKERS: Record<string, string> = {
  "0": "Speaker 1",
  "1": "Speaker 1",
  "2": "Speaker 2",
  "3": "Anna",
};

const ROSTER: ProjectRosterView = { mode: "roster", names: ["Anna", "Maxim"] };
const OPEN_ROSTER: ProjectRosterView = { mode: "open", names: ["Anna", "Maxim"] };

function buildTranscript(overrides: Partial<TranscriptView> = {}): TranscriptView {
  return {
    entry_id: "v-1",
    meeting_name: "260822 - source",
    language: "ru",
    created_at: "2026-08-22T15:29:58Z",
    duration_sec: 3625.8,
    model: "large-v3",
    device: "cuda",
    text: "Раз. Два. Три. Четыре.",
    segments: SENTENCES,
    speakers: SEEDED_SPEAKERS,
    transcript_path: "D:\\Meetings\\RDDM\\260822 - source\\transcript.json",
    ...overrides,
  };
}

/** The turn a given sentence is rendered in — how a case points at one tag
 * among several identical ones. */
function turnSaying(sentence: string): HTMLElement {
  const paragraph = screen.getByText(sentence).closest("li");
  if (paragraph === null) throw new Error(`no turn holding ${sentence}`);
  return paragraph;
}

/** Everything a pick-list shows, placeholder included and in order. */
function offeredOptions(picker: HTMLElement): string[] {
  return within(picker)
    .getAllByRole("option")
    .map((option) => option.textContent ?? "");
}

/** The reuse buttons the viewer puts beside its tags, by accessible name. */
function reuseButtons(): string[] {
  return within(screen.getByRole("list", { name: "Transcript" }))
    .queryAllByRole("button")
    .map((button) => button.getAttribute("aria-label") ?? "")
    .filter((name) => name.startsWith("Attribute this turn to "));
}

/** The operator dragging the pointer over one sentence, as offsets — jsdom
 * has no layout, so a drag cannot be expressed in coordinates. */
function selectSentence(segmentId: string) {
  const span = document.querySelector(`[data-segment-id="${segmentId}"]`);
  if (span === null) throw new Error(`no span for segment ${segmentId}`);
  const text = span.firstChild as Text;
  const range = document.createRange();
  range.setStart(text, 0);
  range.setEnd(text, text.data.length);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent.pointerUp(span.parentElement as HTMLElement);
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
});

describe("TranscriptViewer under a strict roster, with voices the service could not name", () => {
  it("shows a generic diarization label as an unnamed voice rather than a person", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    expect(screen.getAllByRole("button", { name: "Unnamed voice" })).toHaveLength(2);
    expect(screen.queryAllByText(/Speaker \d/)).toEqual([]);
  });

  it("still shows a voice that already carries a roster name", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    expect(screen.getByRole("button", { name: "Anna" })).toBeInTheDocument();
  });

  it("keeps the turns the raw labels make, so two unnamed voices stay two turns", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    expect(
      within(screen.getByRole("list", { name: "Transcript" })).getAllByRole("listitem"),
    ).toHaveLength(3);
  });

  it("offers the roster names and nothing else when naming an unnamed voice", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    await user.click(within(turnSaying("Раз.")).getByRole("button", { name: "Unnamed voice" }));

    const picker = screen.getByRole("combobox", { name: "Name this voice" });
    expect(picker).toHaveValue("");
    expect(offeredOptions(picker)).toEqual(["Choose a name…", "Anna", "Maxim"]);
  });

  it("asks whether a picked name is this turn only or the whole unnamed voice", async () => {
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({
          speakers: { "0": "Speaker 1", "1": "Speaker 2", "2": "Speaker 1", "3": "Anna" },
        })}
        roster={ROSTER}
      />,
    );

    await user.click(within(turnSaying("Раз.")).getByRole("button", { name: "Unnamed voice" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Name this voice" }), "Maxim");

    expect(screen.getByRole("button", { name: "Only this turn" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All 2 turns of this voice" })).toBeInTheDocument();
  });

  it("gives every turn of an unnamed voice the picked name when the whole voice is chosen", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({
          speakers: { "0": "Speaker 1", "1": "Speaker 2", "2": "Speaker 1", "3": "Anna" },
        })}
        roster={ROSTER}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    await user.click(within(turnSaying("Раз.")).getByRole("button", { name: "Unnamed voice" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Name this voice" }), "Maxim");
    await user.click(screen.getByRole("button", { name: "All 2 turns of this voice" }));

    expect(onSaveSpeakers).toHaveBeenCalledWith({
      "0": "Maxim",
      "1": "Speaker 2",
      "2": "Maxim",
      "3": "Anna",
    });
  });

  it("never offers a generic label as a name to reuse beside a turn", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    expect(reuseButtons()).toEqual(["Attribute this turn to Anna", "Attribute this turn to Anna"]);
  });

  it("never offers a generic label to a text selection", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={ROSTER} />);

    selectSentence("2");

    expect(
      within(screen.getByRole("group", { name: "Attribute the selected text to a speaker" }))
        .getAllByRole("button")
        .map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Attribute selection to Anna"]);
  });

  it.each(["Speaker", "Speaker 7b"])(
    "shows a name that only resembles a generic label (%s) as itself",
    (name) => {
      render(
        <TranscriptViewer
          transcript={buildTranscript({ speakers: { "0": name, "1": name } })}
          roster={ROSTER}
        />,
      );

      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    },
  );

  it("shows the raw labels unchanged in open mode", () => {
    render(<TranscriptViewer transcript={buildTranscript()} roster={OPEN_ROSTER} />);

    expect(screen.getByRole("button", { name: "Speaker 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("shows the raw labels unchanged when the meeting has no project roster at all", () => {
    render(<TranscriptViewer transcript={buildTranscript()} />);

    expect(screen.getByRole("button", { name: "Speaker 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });
});
