import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MODEL_DOWNLOAD_POLL_INTERVAL_MS,
  engineLabel,
  engineSetupNeeded,
  formatBytes,
  formatProgress,
  isInProgress,
  pollDownloadStatus,
  type ModelDownloadStatus,
} from "./modelDownload";

function status(overrides: Partial<ModelDownloadStatus> = {}): ModelDownloadStatus {
  return {
    state: "downloading",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 0,
    error_kind: null,
    error_message: null,
    model_present: false,
    cuda_warning: null,
    cuda_runtime_present: null,
    phase: null,
    warnings: {},
    engines: [],
    ...overrides,
  };
}

describe("formatBytes", () => {
  it("renders a small byte count verbatim", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
  });

  it("renders kilobytes with one decimal place", () => {
    expect(formatBytes(1536)).toBe("1.5 KB");
  });

  it("renders gigabytes with one decimal place", () => {
    expect(formatBytes(3_221_225_472)).toBe("3.0 GB");
  });
});

describe("formatProgress", () => {
  it("combines downloaded/total bytes and percent into one string (FR-12: bytes and percentage)", () => {
    const result = formatProgress(
      status({ downloaded_bytes: 1_610_612_736, total_bytes: 3_221_225_472, percent: 50 }),
    );
    expect(result).toBe("1.5 GB / 3.0 GB (50%)");
  });
});

describe("isInProgress", () => {
  it("is true only while downloading or verifying", () => {
    expect(isInProgress("downloading")).toBe(true);
    expect(isInProgress("verifying")).toBe(true);
    expect(isInProgress("idle")).toBe(false);
    expect(isInProgress("complete")).toBe(false);
    expect(isInProgress("cancelled")).toBe(false);
    expect(isInProgress("error")).toBe(false);
  });
});

describe("pollDownloadStatus", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("polls at the configured cadence and reports every result (FR-12: at least once a second)", async () => {
    const results = [
      status({ downloaded_bytes: 100, percent: 10 }),
      status({ downloaded_bytes: 200, percent: 20 }),
    ];
    let call = 0;
    const getStatus = vi.fn(() => Promise.resolve(results[call++] ?? results[results.length - 1]));
    const onUpdate = vi.fn();

    const stop = pollDownloadStatus(getStatus, onUpdate, 1000);

    await vi.advanceTimersByTimeAsync(1000);
    expect(onUpdate).toHaveBeenNthCalledWith(1, results[0]);

    await vi.advanceTimersByTimeAsync(1000);
    expect(onUpdate).toHaveBeenNthCalledWith(2, results[1]);

    stop();
  });

  it("stops issuing further calls once the stop function is invoked", async () => {
    const getStatus = vi.fn(() => Promise.resolve(status()));
    const onUpdate = vi.fn();

    const stop = pollDownloadStatus(getStatus, onUpdate, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(getStatus).toHaveBeenCalledTimes(1);

    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(getStatus).toHaveBeenCalledTimes(1);
  });

  it("stops polling automatically once a terminal state is reported", async () => {
    const getStatus = vi.fn(() => Promise.resolve(status({ state: "complete" })));
    const onUpdate = vi.fn();

    pollDownloadStatus(getStatus, onUpdate, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(getStatus).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5000);
    expect(getStatus).toHaveBeenCalledTimes(1);
  });

  it("tolerates a transient poll failure and keeps polling on the next tick", async () => {
    let call = 0;
    const getStatus = vi.fn(() => {
      call += 1;
      if (call === 1) return Promise.reject(new Error("transient network error"));
      return Promise.resolve(status({ downloaded_bytes: 300 }));
    });
    const onUpdate = vi.fn();

    pollDownloadStatus(getStatus, onUpdate, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onUpdate).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1000);
    expect(onUpdate).toHaveBeenCalledWith(status({ downloaded_bytes: 300 }));
  });

  it("exports a 1s cadence matching FR-12's 'updates at least once a second' (E8)", () => {
    expect(MODEL_DOWNLOAD_POLL_INTERVAL_MS).toBe(1000);
  });
});

describe("engineSetupNeeded", () => {
  const base = { warnings: {}, engines: [], cuda_warning: null, cuda_runtime_present: null };

  it("is nothing on a machine with nothing to set up", () => {
    expect(engineSetupNeeded(base)).toBeNull();
    expect(
      engineSetupNeeded({
        ...base,
        engines: [{ stage: "whisper", engine: "ctranslate2", device: "cpu", present: true }],
      }),
    ).toBeNull();
  });

  it("names a failed optional phase first, with the service's message", () => {
    const setup = engineSetupNeeded({
      ...base,
      warnings: { openvino_weights: "digest mismatch" },
      cuda_runtime_present: false,
    });
    expect(setup).toMatchObject({
      phase: "openvino_weights",
      label: "NPU speech model",
      short: "NPU",
      detail: "digest mismatch",
    });
  });

  it("keeps the durable CUDA signal (E13)", () => {
    expect(engineSetupNeeded({ ...base, cuda_runtime_present: false })).toMatchObject({
      phase: "cuda_runtime",
      label: "GPU acceleration",
      payload: "GPU runtime",
      short: "GPU",
      detail: null,
    });
  });

  it("offers the accelerated engine the plan names but has not got", () => {
    const setup = engineSetupNeeded({
      ...base,
      engines: [{ stage: "whisper", engine: "mlx", device: "metal", present: false }],
    });
    expect(setup).toMatchObject({ phase: "mlx_weights", short: "Apple GPU" });
    expect(
      engineSetupNeeded({
        ...base,
        engines: [{ stage: "whisper", engine: "openvino", device: "NPU", present: false }],
      }),
    ).toMatchObject({ phase: "openvino_runtime", label: "Intel NPU engine" });
  });
});

describe("engineLabel", () => {
  it("puts an engine row into words", () => {
    expect(engineLabel({ stage: "whisper", engine: "mlx", device: "metal", present: true })).toBe(
      "MLX whisper on the Apple GPU",
    );
    expect(
      engineLabel({ stage: "compress", engine: "hevc_qsv", device: "gpu", present: null }),
    ).toBe("Quick Sync on the GPU");
    expect(engineLabel({ stage: "llm", engine: "llama.cpp", device: "cpu", present: true })).toBe(
      "llama.cpp on the CPU",
    );
  });
});
