import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearMocks, mockIPC, mockWindows } from "@tauri-apps/api/mocks";
import { api } from "./api";

/** The speakers-database commands at the IPC edge: which command each
 * wrapper invokes, and with exactly which payload. */

function recordCalls(answer: (cmd: string) => unknown = () => null) {
  const seen: Array<{ cmd: string; payload: unknown }> = [];
  mockIPC((cmd, payload) => {
    seen.push({ cmd, payload });
    return answer(cmd);
  });
  return seen;
}

beforeEach(() => {
  mockWindows("main");
});

afterEach(() => {
  clearMocks();
});

describe("api speakers database", () => {
  it("lists speakers with no arguments", async () => {
    const seen = recordCalls(() => []);

    expect(await api.listSpeakers()).toEqual([]);
    expect(seen.map((call) => call.cmd)).toEqual(["list_speakers"]);
  });

  it("reads one speaker by name", async () => {
    const seen = recordCalls();

    await api.speakerDetail("Никита");

    expect(seen).toContainEqual({ cmd: "speaker_detail", payload: { name: "Никита" } });
  });

  it("creates a speaker with every optional field spelled out as null", async () => {
    const seen = recordCalls();

    await api.saveSpeaker("Olga");

    expect(seen).toContainEqual({
      cmd: "save_speaker",
      payload: { name: "Olga", newName: null, aliases: null, bio: null },
    });
  });

  it("sends only the fields an update carries, and null for the rest", async () => {
    const seen = recordCalls();

    await api.saveSpeaker("Nikita", { newName: "Nikita L" });
    await api.saveSpeaker("Nikita", { aliases: ["Никита"] });
    await api.saveSpeaker("Nikita", { bio: "" });

    expect(seen.map((call) => call.payload)).toEqual([
      { name: "Nikita", newName: "Nikita L", aliases: null, bio: null },
      { name: "Nikita", newName: null, aliases: ["Никита"], bio: null },
      // An emptied bio is a real value: it clears the stored one.
      { name: "Nikita", newName: null, aliases: null, bio: "" },
    ]);
  });

  it("deletes a speaker by name and resolves the flag", async () => {
    const seen = recordCalls(() => true);

    expect(await api.deleteSpeaker("Nikita")).toBe(true);
    expect(seen).toContainEqual({ cmd: "delete_speaker", payload: { name: "Nikita" } });
  });
});
