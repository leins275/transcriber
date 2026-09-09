import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptViewer } from "./TranscriptViewer";
import type { ProjectRosterView, TranscriptView } from "../types";

/**
 * The roster reaching the two name controls, seen from the viewer: in roster
 * mode the operator picks from a list, in open mode they type and the
 * project's own names are offered as hints.
 *
 * A `<datalist>`'s options carry no `option` role, while a `<select>`'s do —
 * which is how these tests tell a pick-list apart from a free-text box whose
 * suggestions happen to make it a combobox too.
 */

/** One turn of two sentences: enough for "the whole turn" to mean something. */
const ONE_TURN = [
  { id: 0, start: 0, end: 1.5, text: " Раз." },
  { id: 1, start: 1.5, end: 3, text: " Два." },
];

/** Five sentences running on, so a selection can cover part of a turn. */
const SENTENCES = [
  { id: 0, start: 0, end: 1.5, text: " Раз." },
  { id: 1, start: 1.5, end: 3, text: " Два." },
  { id: 2, start: 3, end: 4.5, text: " Три." },
  { id: 3, start: 4.5, end: 6, text: " Четыре." },
  { id: 4, start: 6, end: 7.5, text: " Пять." },
];

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
    text: "Раз. Два.",
    segments: ONE_TURN,
    speakers: {},
    transcript_path: "D:\\Meetings\\RDDM\\260822 - source\\transcript.json",
    ...overrides,
  };
}

function segmentText(segmentId: string): Text {
  const span = document.querySelector(`[data-segment-id="${segmentId}"]`);
  if (span === null) throw new Error(`no span for segment ${segmentId}`);
  return span.firstChild as Text;
}

/** The operator dragging the pointer over the transcript, as offsets — jsdom
 * has no layout, so a drag cannot be expressed in coordinates. */
function selectText(from: { segment: string }, to: { segment: string }) {
  const start = segmentText(from.segment);
  const end = segmentText(to.segment);
  const range = document.createRange();
  range.setStart(start, 0);
  range.setEnd(end, end.data.length);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent.pointerUp(end.parentElement as HTMLElement);
}

/** The values a free-text box's own datalist offers. */
function datalistValues(control: HTMLElement): string[] {
  const listId = control.getAttribute("list");
  const datalist = listId === null ? null : document.getElementById(listId);
  return Array.from(datalist?.querySelectorAll("option") ?? []).map((option) => option.value);
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
});

describe("TranscriptViewer with a project roster", () => {
  it("attributes a whole turn to a name picked from the roster", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript()}
        roster={ROSTER}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.selectOptions(screen.getByRole("combobox", { name: /name this speaker/i }), "Anna");

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "0": "Anna", "1": "Anna" });
  });

  it("attributes exactly the selected sentences to a name picked from the roster", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript({ segments: SENTENCES, text: "Раз. Два. Три." })}
        roster={ROSTER}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    selectText({ segment: "1" }, { segment: "2" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: /attribute selection to a speaker/i }),
      "Maxim",
    );

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "1": "Maxim", "2": "Maxim" });
  });

  it("offers the project's names as typing hints instead of a pick list in open mode", async () => {
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript()}
        roster={OPEN_ROSTER}
        suggestedSpeakers={["Olga"]}
      />,
    );

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(datalistValues(screen.getByLabelText(/name this speaker/i))).toEqual(["Olga"]);
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("still takes a freely typed name when the project has no roster at all", async () => {
    const onSaveSpeakers = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <TranscriptViewer
        transcript={buildTranscript()}
        suggestedSpeakers={["Olga"]}
        onSaveSpeakers={onSaveSpeakers}
      />,
    );

    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.type(screen.getByLabelText(/name this speaker/i), "Zoe{Enter}");

    expect(onSaveSpeakers).toHaveBeenCalledWith({ "0": "Zoe", "1": "Zoe" });
  });
});
