import { describe, expect, it } from "vitest";
import {
  assignSpeaker,
  assignSpeakerToSegments,
  filterTurns,
  groupIntoTurns,
  renameSpeaker,
  speakerNames,
} from "./turns";
import type { TranscriptSegmentView } from "../types";

function seg(id: number, start: number, end: number, text: string): TranscriptSegmentView {
  return { id, start, end, text };
}

describe("groupIntoTurns", () => {
  it("merges consecutive segments with no meaningful pause into one turn", () => {
    const turns = groupIntoTurns(
      [seg(0, 0, 2, " Hello there"), seg(1, 2.1, 4, " and welcome")],
      {},
    );

    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe("Hello there and welcome");
    expect(turns[0].segmentIds).toEqual(["0", "1"]);
    expect(turns[0].start).toBe(0);
    expect(turns[0].end).toBe(4);
  });

  it("starts a new turn after a silence, when nothing is attributed yet", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "first"), seg(1, 6, 8, "second")], {});

    expect(turns).toHaveLength(2);
    expect(turns[1].id).toBe("1");
  });

  it("leaves turns unattributed rather than inventing Speaker 1", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "first")], {});
    expect(turns[0].speaker).toBeNull();
  });

  it("splits on a change of assigned speaker even with no pause", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "mine"), seg(1, 2, 4, "yours")], {
      "0": "Maxim",
      "1": "Anna",
    });

    expect(turns.map((t) => t.speaker)).toEqual(["Maxim", "Anna"]);
  });

  it("does not split a labelled turn on a pause in the middle of it", () => {
    // Rule 1 outranks rule 2: once someone is attributed, their pause for
    // breath is not a handover.
    const turns = groupIntoTurns([seg(0, 0, 2, "first"), seg(1, 30, 32, "still me")], {
      "0": "Maxim",
      "1": "Maxim",
    });

    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe("first still me");
  });

  it("drops whitespace-only segments instead of opening turns for them", () => {
    const turns = groupIntoTurns(
      [seg(0, 0, 2, "real"), seg(1, 2, 3, "   "), seg(2, 3, 5, "also real")],
      {},
    );

    expect(turns).toHaveLength(1);
    expect(turns[0].segmentIds).toEqual(["0", "2"]);
  });

  it("honours a custom gap threshold", () => {
    const segments = [seg(0, 0, 2, "first"), seg(1, 3, 5, "second")];

    expect(groupIntoTurns(segments, {}, { gapSeconds: 5 })).toHaveLength(1);
    expect(groupIntoTurns(segments, {}, { gapSeconds: 0.5 })).toHaveLength(2);
  });

  it("returns nothing for an empty transcript", () => {
    expect(groupIntoTurns([], {})).toEqual([]);
  });
});

describe("speakerNames", () => {
  it("lists each speaker once, in the order they first speak", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "a"), seg(1, 2, 4, "b"), seg(2, 4, 6, "c")], {
      "0": "Maxim",
      "1": "Anna",
      "2": "Maxim",
    });

    expect(speakerNames(turns)).toEqual(["Maxim", "Anna"]);
  });

  it("is empty when nothing is attributed", () => {
    expect(speakerNames(groupIntoTurns([seg(0, 0, 2, "a")], {}))).toEqual([]);
  });
});

describe("assignSpeaker", () => {
  it("labels every segment of the turn, not just its first", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "a"), seg(1, 2, 4, "b")], {});

    const next = assignSpeaker({}, turns[0], "Maxim");

    expect(next).toEqual({ "0": "Maxim", "1": "Maxim" });
  });

  it("clears the attribution when given null or blank", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "a")], { "0": "Maxim" });

    expect(assignSpeaker({ "0": "Maxim" }, turns[0], null)).toEqual({});
    expect(assignSpeaker({ "0": "Maxim" }, turns[0], "   ")).toEqual({});
  });

  it("trims the name rather than storing the operator's whitespace", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "a")], {});
    expect(assignSpeaker({}, turns[0], "  Maxim ")).toEqual({ "0": "Maxim" });
  });

  it("does not mutate the map it is given", () => {
    const turns = groupIntoTurns([seg(0, 0, 2, "a")], {});
    const original = {};

    assignSpeaker(original, turns[0], "Maxim");

    expect(original).toEqual({});
  });
});

describe("assignSpeakerToSegments", () => {
  // Five sentence-sized segments, no pause anywhere: one turn until a label
  // splits it.
  const fiveSegments = [
    seg(1, 0, 2, "one"),
    seg(2, 2, 4, "two"),
    seg(3, 4, 6, "three"),
    seg(4, 6, 8, "four"),
    seg(5, 8, 10, "five"),
  ];
  const allMaxim = { "1": "Maxim", "2": "Maxim", "3": "Maxim", "4": "Maxim", "5": "Maxim" };

  it("labels exactly the ids it is given, no more and no fewer", () => {
    const next = assignSpeakerToSegments({ "1": "Maxim" }, ["2", "3"], "Anna");

    expect(next).toEqual({ "1": "Maxim", "2": "Anna", "3": "Anna" });
  });

  it("trims the name rather than storing the operator's whitespace", () => {
    expect(assignSpeakerToSegments({}, ["7"], "  Anna ")).toEqual({ "7": "Anna" });
  });

  it("clears the attribution of those ids when given null or blank", () => {
    expect(assignSpeakerToSegments(allMaxim, ["2", "3"], null)).toEqual({
      "1": "Maxim",
      "4": "Maxim",
      "5": "Maxim",
    });
    expect(assignSpeakerToSegments(allMaxim, ["2", "3"], "   ")).toEqual({
      "1": "Maxim",
      "4": "Maxim",
      "5": "Maxim",
    });
  });

  it("does not mutate the map it is given", () => {
    const original = { "1": "Maxim" };

    assignSpeakerToSegments(original, ["1", "2"], "Anna");

    expect(original).toEqual({ "1": "Maxim" });
  });

  it("changes nothing when given no ids", () => {
    expect(assignSpeakerToSegments(allMaxim, [], "Anna")).toEqual(allMaxim);
  });

  it("splits a labelled turn in three when the middle segment is reassigned", () => {
    const next = assignSpeakerToSegments(allMaxim, ["3"], "Anna");

    const turns = groupIntoTurns(fiveSegments, next);

    expect(turns.map((turn) => [turn.speaker, turn.segmentIds])).toEqual([
      ["Maxim", ["1", "2"]],
      ["Anna", ["3"]],
      ["Maxim", ["4", "5"]],
    ]);
  });

  it("leaves the flanks of an unattributed turn unattributed", () => {
    const next = assignSpeakerToSegments({}, ["3"], "Anna");

    const turns = groupIntoTurns(fiveSegments, next);

    expect(turns.map((turn) => [turn.speaker, turn.segmentIds])).toEqual([
      [null, ["1", "2"]],
      ["Anna", ["3"]],
      [null, ["4", "5"]],
    ]);
  });
});

describe("renameSpeaker", () => {
  it("renames every segment that speaker holds", () => {
    const before = { "0": "Speaker 2", "1": "Maxim", "2": "Speaker 2" };

    expect(renameSpeaker(before, "Speaker 2", "Anna")).toEqual({
      "0": "Anna",
      "1": "Maxim",
      "2": "Anna",
    });
  });

  it("merges when renamed onto a name already in use", () => {
    const before = { "0": "Speaker 2", "1": "Maxim" };
    expect(renameSpeaker(before, "Speaker 2", "Maxim")).toEqual({ "0": "Maxim", "1": "Maxim" });
  });

  it("refuses to rename a speaker to nothing", () => {
    const before = { "0": "Maxim" };
    expect(renameSpeaker(before, "Maxim", "   ")).toEqual(before);
  });
});

describe("filterTurns", () => {
  const turns = groupIntoTurns([seg(0, 0, 2, "docker compose"), seg(1, 9, 11, "grafana")], {
    "0": "Maxim",
    "1": "Anna",
  });

  it("matches text case-insensitively", () => {
    expect(filterTurns(turns, "DOCKER").map((t) => t.id)).toEqual(["0"]);
  });

  it("matches a speaker's name too", () => {
    expect(filterTurns(turns, "anna").map((t) => t.id)).toEqual(["1"]);
  });

  it("restores everything when the query is cleared", () => {
    expect(filterTurns(turns, "")).toHaveLength(2);
    expect(filterTurns(turns, "   ")).toHaveLength(2);
  });

  it("returns nothing when nothing matches", () => {
    expect(filterTurns(turns, "kubernetes")).toEqual([]);
  });
});
