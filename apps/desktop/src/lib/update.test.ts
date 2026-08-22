import { describe, expect, it } from "vitest";
import { downloadPercent, isVisible, updateMessage, type UpdateState } from "./update";

describe("downloadPercent", () => {
  it("reports progress against a known total", () => {
    expect(downloadPercent(0, 100)).toBe(0);
    expect(downloadPercent(50, 100)).toBe(50);
    expect(downloadPercent(100, 100)).toBe(100);
  });

  it("is null when the server gave no content length", () => {
    // An invented denominator produces a bar that races to 100% and sits
    // there, which is worse than admitting the total is unknown.
    expect(downloadPercent(500, null)).toBeNull();
    expect(downloadPercent(500, 0)).toBeNull();
  });

  it("clamps rather than reporting more than complete", () => {
    expect(downloadPercent(150, 100)).toBe(100);
    expect(downloadPercent(-10, 100)).toBe(0);
  });
});

describe("updateMessage", () => {
  const update = { version: "0.3.0", notes: "Fixes things", date: null };

  it("says nothing while checking, idle, or already current", () => {
    // An app that announces it is looking for updates, or that there are
    // none, is interrupting to report that nothing happened.
    expect(updateMessage({ status: "idle" })).toBeNull();
    expect(updateMessage({ status: "checking" })).toBeNull();
    expect(updateMessage({ status: "up-to-date" })).toBeNull();
  });

  it("names the version that is available", () => {
    expect(updateMessage({ status: "available", update })).toContain("0.3.0");
  });

  it("shows a percentage while downloading, and omits it when unknown", () => {
    expect(updateMessage({ status: "downloading", update, percent: 42 })).toContain("42%");
    const unknown = updateMessage({ status: "downloading", update, percent: null });
    expect(unknown).toContain("0.3.0");
    expect(unknown).not.toContain("%");
  });

  it("asks for a restart once installed", () => {
    expect(updateMessage({ status: "installed", update })).toMatch(/restart/i);
  });

  it("reports a failed check with its reason", () => {
    expect(updateMessage({ status: "error", message: "network unreachable" })).toContain(
      "network unreachable",
    );
  });
});

describe("isVisible", () => {
  it("is true exactly when there is something to say", () => {
    const update = { version: "0.3.0", notes: null, date: null };
    const silent: UpdateState[] = [
      { status: "idle" },
      { status: "checking" },
      { status: "up-to-date" },
    ];
    const speaking: UpdateState[] = [
      { status: "available", update },
      { status: "downloading", update, percent: null },
      { status: "installed", update },
      { status: "error", message: "boom" },
    ];

    for (const state of silent) expect(isVisible(state)).toBe(false);
    for (const state of speaking) expect(isVisible(state)).toBe(true);
  });
});
