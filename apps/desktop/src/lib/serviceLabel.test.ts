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

describe("serviceStatusLabel with the engine plan", () => {
  it("names the device the transcription engine runs on", () => {
    const row = (engine: string, device: string, present: boolean | null) => [
      { stage: "whisper", engine, device, present },
    ];
    expect(serviceStatusLabel("ready", null, row("mlx", "metal", true))).toBe("Ready · GPU");
    expect(serviceStatusLabel("ready", null, row("openvino", "NPU", true))).toBe("Ready · NPU");
    expect(serviceStatusLabel("ready", true, row("ctranslate2", "cuda", true))).toBe("Ready · GPU");
    expect(serviceStatusLabel("ready", null, row("ctranslate2", "cpu", true))).toBe("Ready · CPU");
  });

  it("says CPU while an accelerated engine's payload is still missing", () => {
    const engines = [{ stage: "whisper", engine: "openvino", device: "NPU", present: false }];
    expect(serviceStatusLabel("ready", null, engines)).toBe("Ready · CPU");
  });

  it("falls back to the CUDA flag without a plan", () => {
    expect(serviceStatusLabel("ready", true, [])).toBe("Ready · GPU");
    expect(serviceStatusLabel("ready", null, undefined)).toBe("Ready");
  });
});
