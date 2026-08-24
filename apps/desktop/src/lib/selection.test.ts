import { afterEach, describe, expect, it } from "vitest";
import { segmentIdsFromRange } from "./selection";

type Segment = { id: string; text: string };

/**
 * A stand-in for the viewer's Timeline markup: a toolbar above the transcript
 * list, and one `<li>` per turn holding the speaker cell, the timestamp, and a
 * paragraph of per-segment spans separated by whitespace text nodes.
 *
 * jsdom has no layout, so every case here is built out of document order:
 * ranges are constructed over named text nodes, never from pointer geometry.
 */
function buildTranscript(turns: Segment[][]): HTMLElement {
  const container = document.createElement("div");

  const toolbar = document.createElement("div");
  toolbar.append(document.createTextNode("Find in transcript"));
  container.append(toolbar);

  const list = document.createElement("ol");
  for (const turn of turns) {
    const item = document.createElement("li");

    const speakerCell = document.createElement("span");
    speakerCell.className = "speaker";
    speakerCell.append(document.createTextNode("Add speaker"));

    const time = document.createElement("span");
    time.className = "time";
    time.append(document.createTextNode("0:00"));

    const paragraph = document.createElement("p");
    turn.forEach((segment, index) => {
      if (index > 0) paragraph.append(document.createTextNode(" "));
      const span = document.createElement("span");
      span.dataset.segmentId = segment.id;
      span.append(document.createTextNode(segment.text));
      paragraph.append(span);
    });

    item.append(speakerCell, time, paragraph);
    list.append(item);
  }
  container.append(list);
  document.body.append(container);
  return list;
}

function textOf(root: HTMLElement, segmentId: string): Text {
  const span = root.querySelector(`[data-segment-id="${segmentId}"]`);
  if (span === null) throw new Error(`no span for segment ${segmentId}`);
  return span.firstChild as Text;
}

/** The whitespace text node the paragraph puts after segment `segmentId`. */
function gapAfter(root: HTMLElement, segmentId: string): Text {
  const span = root.querySelector(`[data-segment-id="${segmentId}"]`);
  if (span === null) throw new Error(`no span for segment ${segmentId}`);
  const next = span.nextSibling;
  if (next === null || next.nodeType !== Node.TEXT_NODE) {
    throw new Error(`segment ${segmentId} is not followed by whitespace`);
  }
  return next as Text;
}

function rangeBetween(start: Text, startOffset: number, end: Text, endOffset: number): Range {
  const range = document.createRange();
  range.setStart(start, startOffset);
  range.setEnd(end, endOffset);
  return range;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("segmentIdsFromRange", () => {
  it("returns exactly the ids of the segments a selection covers", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
        { id: "2", text: "Third sentence." },
        { id: "3", text: "Fourth sentence." },
      ],
    ]);
    const from = textOf(root, "1");
    const to = textOf(root, "2");

    const range = rangeBetween(from, 0, to, to.data.length);

    expect(segmentIdsFromRange(range, root)).toEqual(["1", "2"]);
  });

  it("snaps outward: a selection starting and ending mid-segment takes both whole", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
        { id: "2", text: "Third sentence." },
      ],
    ]);
    const from = textOf(root, "0");
    const to = textOf(root, "2");

    const range = rangeBetween(from, 6, to, 5);

    expect(segmentIdsFromRange(range, root)).toEqual(["0", "1", "2"]);
  });

  it("returns ids from both turns, in document order, for a cross-turn selection", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "Turn A opening." },
        { id: "1", text: "Turn A tail." },
      ],
      [
        { id: "2", text: "Turn B head." },
        { id: "3", text: "Turn B tail." },
      ],
    ]);
    const from = textOf(root, "1");
    const to = textOf(root, "2");

    const range = rangeBetween(from, 3, to, 4);

    expect(segmentIdsFromRange(range, root)).toEqual(["1", "2"]);
  });

  it("returns nothing for a collapsed selection", () => {
    const root = buildTranscript([[{ id: "0", text: "First sentence." }]]);
    const caret = textOf(root, "0");

    const range = rangeBetween(caret, 4, caret, 4);

    expect(segmentIdsFromRange(range, root)).toEqual([]);
  });

  it("returns nothing for a selection outside the transcript text", () => {
    const root = buildTranscript([[{ id: "0", text: "First sentence." }]]);
    const toolbarText = document.body.firstChild?.firstChild?.firstChild as Text;

    const range = rangeBetween(toolbarText, 0, toolbarText, toolbarText.data.length);

    expect(segmentIdsFromRange(range, root)).toEqual([]);
  });

  it("returns nothing for a selection confined to turn chrome", () => {
    const root = buildTranscript([[{ id: "0", text: "First sentence." }]]);
    const timestamp = root.querySelector(".time")?.firstChild as Text;

    const range = rangeBetween(timestamp, 0, timestamp, timestamp.data.length);

    expect(segmentIdsFromRange(range, root)).toEqual([]);
  });

  it("resolves a boundary landing between segment spans via the in-range fallback", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
        { id: "2", text: "Third sentence." },
      ],
    ]);
    const gap = gapAfter(root, "0");
    const to = textOf(root, "2");

    const range = rangeBetween(gap, 0, to, 5);

    expect(segmentIdsFromRange(range, root)).toEqual(["1", "2"]);
  });

  it("excludes a segment the selection ends on without covering any of it", () => {
    // Dragging past the end of a sentence is normalized by the browser to
    // offset 0 of the *next* segment's text node. Zero characters of that
    // segment are covered, so it is not part of the selection -- attributing
    // it would hand the operator's speaker to a sentence they never dragged
    // over.
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
        { id: "2", text: "Third sentence." },
        { id: "3", text: "Fourth sentence." },
      ],
    ]);
    const from = textOf(root, "1");
    const to = textOf(root, "3");

    const range = rangeBetween(from, 0, to, 0);

    expect(segmentIdsFromRange(range, root)).toEqual(["1", "2"]);
  });

  it("excludes a segment the selection starts on without covering any of it", () => {
    // The mirror case at the leading edge: a drag begun just after the last
    // character of a sentence.
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
        { id: "2", text: "Third sentence." },
        { id: "3", text: "Fourth sentence." },
      ],
    ]);
    const from = textOf(root, "1");
    const to = textOf(root, "3");

    const range = rangeBetween(from, from.data.length, to, to.data.length);

    expect(segmentIdsFromRange(range, root)).toEqual(["2", "3"]);
  });

  it("returns nothing for a selection of the whitespace between two segments", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
      ],
    ]);
    const from = textOf(root, "0");
    const to = textOf(root, "1");

    const range = rangeBetween(from, from.data.length, to, 0);

    expect(segmentIdsFromRange(range, root)).toEqual([]);
  });

  it("resolves a selection that starts on the timestamp and runs into the text", () => {
    const root = buildTranscript([
      [
        { id: "0", text: "First sentence." },
        { id: "1", text: "Second sentence." },
      ],
    ]);
    const timestamp = root.querySelector(".time")?.firstChild as Text;
    const to = textOf(root, "0");

    const range = rangeBetween(timestamp, 0, to, 5);

    expect(segmentIdsFromRange(range, root)).toEqual(["0"]);
  });

  it("returns nothing when the transcript renders no segment spans", () => {
    const root = buildTranscript([]);
    const paragraph = document.createElement("p");
    paragraph.append(document.createTextNode("This transcript has no segments."));
    root.append(paragraph);
    const empty = paragraph.firstChild as Text;

    const range = rangeBetween(empty, 0, empty, 5);

    expect(segmentIdsFromRange(range, root)).toEqual([]);
  });
});
