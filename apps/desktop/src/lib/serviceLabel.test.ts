import { describe, expect, it } from "vitest";
import { serviceStatusLabel } from "./serviceLabel";

describe("serviceStatusLabel", () => {
  it("shows a starting label regardless of cuda info", () => {
    expect(serviceStatusLabel("starting", null)).toBe("Starting…");
  });

  it("shows an unavailable label regardless of cuda info", () => {
    expect(serviceStatusLabel("unavailable", true)).toBe("Unavailable");
  });

  it("appends GPU when the cuda runtime is present", () => {
    expect(serviceStatusLabel("ready", true)).toBe("Ready · GPU");
  });

  it("appends CPU when the cuda runtime is absent on a GPU-capable host", () => {
    expect(serviceStatusLabel("ready", false)).toBe("Ready · CPU");
  });

  it("omits the suffix when there is no GPU on this host at all", () => {
    expect(serviceStatusLabel("ready", null)).toBe("Ready");
  });

  it("omits the suffix when cuda info is not yet known", () => {
    expect(serviceStatusLabel("ready", undefined)).toBe("Ready");
  });
});
