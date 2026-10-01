import { describe, expect, it } from "vitest";
import {
  DEFAULT_SPEAKER_SORT,
  filterSpeakers,
  nextSpeakerSort,
  sortSpeakers,
  type SpeakerSort,
} from "./speakerSort";
import type { SpeakerView } from "../types";

function person(overrides: Partial<SpeakerView> = {}): SpeakerView {
  return {
    name: "Anna",
    aliases: [],
    bio: "",
    registered: true,
    projects: ["ELS"],
    meetings: 1,
    labelled_segments: 10,
    hand_segments: 10,
    speech_sec: 60,
    voice_samples: 1,
    voice_set_aside: 0,
    ...overrides,
  };
}

const names = (people: SpeakerView[]) => people.map((one) => one.name);

describe("nextSpeakerSort", () => {
  it("starts a text column ascending and a number column descending", () => {
    expect(nextSpeakerSort(DEFAULT_SPEAKER_SORT, "projects")).toEqual({
      column: "projects",
      direction: "asc",
    });
    expect(nextSpeakerSort(DEFAULT_SPEAKER_SORT, "meetings")).toEqual({
      column: "meetings",
      direction: "desc",
    });
  });

  it("flips the direction on a second click of the same column", () => {
    const once = nextSpeakerSort(DEFAULT_SPEAKER_SORT, "speech");
    expect(nextSpeakerSort(once, "speech")).toEqual({ column: "speech", direction: "asc" });
    expect(nextSpeakerSort(DEFAULT_SPEAKER_SORT, "name")).toEqual({
      column: "name",
      direction: "desc",
    });
  });
});

describe("filterSpeakers", () => {
  const people = [
    person({ name: "Nikita", aliases: ["Никита"] }),
    person({ name: "Anna", aliases: ["Anya"] }),
  ];

  it("keeps everybody for a blank query", () => {
    expect(filterSpeakers(people, "  ")).toEqual(people);
  });

  it("matches the name case-insensitively", () => {
    expect(names(filterSpeakers(people, "nIK"))).toEqual(["Nikita"]);
  });

  it("matches an alias", () => {
    expect(names(filterSpeakers(people, "никит"))).toEqual(["Nikita"]);
    expect(names(filterSpeakers(people, "anya"))).toEqual(["Anna"]);
  });

  it("answers nobody when nothing matches", () => {
    expect(filterSpeakers(people, "zzz")).toEqual([]);
  });
});

describe("sortSpeakers", () => {
  const people = [
    person({ name: "Maxim", meetings: 3, speech_sec: 30, voice_samples: 0, projects: [] }),
    person({ name: "Anna", meetings: 7, speech_sec: 900, voice_samples: 4, projects: ["GIS"] }),
    person({ name: "Boris", meetings: 3, speech_sec: 120, voice_samples: 9, projects: ["ELS"] }),
  ];
  const by = (sort: SpeakerSort) => names(sortSpeakers(people, sort));

  it("rests alphabetical by name", () => {
    expect(by(DEFAULT_SPEAKER_SORT)).toEqual(["Anna", "Boris", "Maxim"]);
    expect(by({ column: "name", direction: "desc" })).toEqual(["Maxim", "Boris", "Anna"]);
  });

  it("sorts by a number, breaking ties by name in either direction", () => {
    expect(by({ column: "meetings", direction: "desc" })).toEqual(["Anna", "Boris", "Maxim"]);
    expect(by({ column: "meetings", direction: "asc" })).toEqual(["Boris", "Maxim", "Anna"]);
  });

  it("sorts by labelled speech and by voice samples", () => {
    expect(by({ column: "speech", direction: "desc" })).toEqual(["Anna", "Boris", "Maxim"]);
    expect(by({ column: "voice", direction: "desc" })).toEqual(["Boris", "Anna", "Maxim"]);
  });

  it("keeps a person in no project last under the Projects sort, both ways", () => {
    expect(by({ column: "projects", direction: "asc" })).toEqual(["Boris", "Anna", "Maxim"]);
    expect(by({ column: "projects", direction: "desc" })).toEqual(["Anna", "Boris", "Maxim"]);
  });

  it("never reorders the array it was handed", () => {
    sortSpeakers(people, { column: "name", direction: "asc" });
    expect(names(people)).toEqual(["Maxim", "Anna", "Boris"]);
  });
});
