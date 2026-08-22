import { describe, expect, it } from "vitest";
import {
  formatCount,
  formatDuration,
  formatRealtimeFactor,
  formatTimecode,
  formatTimestamp,
} from "./format";

describe("formatTimecode", () => {
  it("renders m:ss under an hour and h:mm:ss past it", () => {
    expect(formatTimecode(0)).toBe("0:00");
    expect(formatTimecode(9)).toBe("0:09");
    expect(formatTimecode(125)).toBe("2:05");
    expect(formatTimecode(3661)).toBe("1:01:01");
  });

  it("never renders NaN or a negative position", () => {
    expect(formatTimecode(Number.NaN)).toBe("0:00");
    expect(formatTimecode(-5)).toBe("0:00");
  });
});

describe("formatDuration", () => {
  it("renders a span as a phrase", () => {
    expect(formatDuration(9)).toBe("9s");
    expect(formatDuration(125)).toBe("2m 5s");
    expect(formatDuration(3625)).toBe("1h 0m");
  });

  it("renders an em dash for a missing or nonsense value", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("formatTimestamp", () => {
  it("renders a real ISO timestamp", () => {
    expect(formatTimestamp("2026-08-22T15:29:58Z")).toMatch(/2026/);
  });

  it("passes an unparseable string through rather than showing Invalid Date", () => {
    expect(formatTimestamp("not a timestamp")).toBe("not a timestamp");
  });

  it("renders an em dash for a missing value", () => {
    expect(formatTimestamp(null)).toBe("—");
  });
});

describe("formatRealtimeFactor", () => {
  it("renders the ratio with its unit", () => {
    expect(formatRealtimeFactor(0.2812)).toBe("0.28× realtime");
  });

  it("renders an em dash for a missing value", () => {
    expect(formatRealtimeFactor(null)).toBe("—");
  });
});

describe("formatCount", () => {
  it("pluralizes the noun", () => {
    expect(formatCount(1, "segment")).toBe("1 segment");
    expect(formatCount(1144, "segment")).toBe("1144 segments");
    expect(formatCount(0, "segment")).toBe("0 segments");
  });

  it("renders an em dash for a missing count", () => {
    expect(formatCount(null, "segment")).toBe("—");
  });
});
