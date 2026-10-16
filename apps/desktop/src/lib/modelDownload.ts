/**
 * Pure, framework-agnostic model-download logic (T13, FR-12, FR-17):
 * formatting helpers and the status-poll scheduler `ModelDownloadStep.tsx`
 * drives. No `@tauri-apps/api` import here -- `api.ts` stays the only module
 * that imports it; this file is handed injected command functions instead
 * (mirrors the "fake command layer" the component test uses).
 */

export type ModelDownloadState =
  "idle" | "downloading" | "verifying" | "complete" | "cancelled" | "error";

export type ModelDownloadStatus = {
  state: ModelDownloadState;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error_kind: string | null;
  error_message: string | null;
  model_present: boolean;
  /** E13: verbatim backend text, set once a `SetupDownload`'s CUDA-runtime
   * phase has failed and the setup continued into the model phase anyway
   * (E4). Never re-worded on this side. */
  cuda_warning: string | null;
  /** E13: `null` when the service itself judged this host GPU-less (never
   * prompt about a runtime that machine could never use). */
  cuda_runtime_present: boolean | null;
  /** The chained setup's current phase (`model`, `cuda_runtime`,
   * `openvino_runtime`, `openvino_weights`, `mlx_weights`); `null` for a
   * bare model download. */
  phase: string | null;
  /** The optional phases that failed while the setup carried on, by phase
   * name, with the service's own message verbatim. */
  warnings: Record<string, string>;
  /** `/health`'s engine plan for this machine: which engine each stage
   * runs on and whether its payload is installed (`null` = nothing to
   * install). Empty until the service answered. */
  engines: EngineRow[];
};

/** One row of the service's engine plan (`engines.py`). */
export type EngineRow = {
  stage: "whisper" | "llm" | "diarization" | "compress" | string;
  engine: string;
  device: string;
  present: boolean | null;
};

/** A phase in the operator's words: `label` names the feature ("GPU
 * acceleration is not installed"), `payload` what is being transferred
 * ("Downloading GPU runtime"), `short` the retry button's subject ("Retry
 * GPU setup"). */
export type PhaseWords = { label: string; payload: string; short: string };

/** The phase names on the wire. */
export const PHASE_WORDS: Record<string, PhaseWords> = {
  model: { label: "Speech model", payload: "speech model", short: "model" },
  cuda_runtime: { label: "GPU acceleration", payload: "GPU runtime", short: "GPU" },
  openvino_runtime: { label: "Intel NPU engine", payload: "NPU engine", short: "NPU" },
  openvino_weights: { label: "NPU speech model", payload: "NPU speech model", short: "NPU" },
  mlx_weights: {
    label: "Apple GPU speech model",
    payload: "Apple GPU speech model",
    short: "Apple GPU",
  },
};

export function phaseWords(phase: string | null | undefined): PhaseWords | null {
  if (!phase) return null;
  return PHASE_WORDS[phase] ?? { label: phase, payload: phase, short: phase };
}

/** Which phase sets up a whisper engine the plan names but has not got. */
const SETUP_PHASE_FOR_ENGINE: Record<string, string> = {
  ctranslate2: "cuda_runtime",
  mlx: "mlx_weights",
  openvino: "openvino_runtime",
};

export type EngineSetup = PhaseWords & { phase: string; detail: string | null };

/** The accelerated-engine setup this machine is offered but does not have
 * yet: a failed optional phase first (with the service's own message),
 * else the durable `/health` signals -- the CUDA runtime missing on an
 * NVIDIA host, or the whisper row's payload missing. `null` when there is
 * nothing to set up (or nothing known yet). */
export function engineSetupNeeded(
  status: Pick<
    ModelDownloadStatus,
    "warnings" | "engines" | "cuda_warning" | "cuda_runtime_present"
  >,
): EngineSetup | null {
  const failed = Object.entries(status.warnings ?? {});
  if (failed.length > 0) {
    const [phase, message] = failed[0];
    return { phase, ...phaseWords(phase)!, detail: message };
  }
  if (status.cuda_warning || status.cuda_runtime_present === false) {
    return { phase: "cuda_runtime", ...PHASE_WORDS.cuda_runtime, detail: status.cuda_warning };
  }
  const whisper = (status.engines ?? []).find((row) => row.stage === "whisper");
  if (whisper && whisper.present === false) {
    const phase = SETUP_PHASE_FOR_ENGINE[whisper.engine] ?? "model";
    return { phase, ...phaseWords(phase)!, detail: null };
  }
  return null;
}

const ENGINE_NAMES: Record<string, string> = {
  ctranslate2: "faster-whisper",
  mlx: "MLX whisper",
  openvino: "OpenVINO whisper",
  "llama.cpp": "llama.cpp",
  pyannote: "pyannote",
  hevc_nvenc: "NVENC",
  hevc_qsv: "Quick Sync",
  hevc_videotoolbox: "VideoToolbox",
  libx265: "x265",
};

const DEVICE_NAMES: Record<string, string> = {
  cuda: "NVIDIA GPU",
  metal: "Apple GPU",
  mps: "Apple GPU",
  NPU: "Intel NPU",
  GPU: "Intel GPU",
  gpu: "GPU",
  cpu: "CPU",
  CPU: "CPU",
};

/** "MLX whisper on the Apple GPU" -- one engine row in words. */
export function engineLabel(row: EngineRow): string {
  const engine = ENGINE_NAMES[row.engine] ?? row.engine;
  const device = DEVICE_NAMES[row.device] ?? row.device;
  return `${engine} on the ${device}`;
}

export const STAGE_LABELS: Record<string, string> = {
  whisper: "Transcription",
  llm: "Summaries",
  diarization: "Speakers",
  compress: "Video compression",
};

/** E8/FR-12 acceptance ("updates at least once a second"): the service
 * emits progress at a 1 s cadence, so this polls at the same 1000 ms rather
 * than F3's 1500 ms job-poll cadence (Q3-A) -- a cheap in-memory read, so
 * there is no cost to matching the documented acceptance criterion exactly
 * instead of under-polling it. */
export const MODEL_DOWNLOAD_POLL_INTERVAL_MS = 1000;

const UNITS = ["B", "KB", "MB", "GB", "TB"];

/** Renders a byte count as a short, human string ("1.5 GB"). Whole bytes
 * under 1 KB are rendered with no decimal ("512 B"). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${Math.round(bytes)} B`;
  }
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${UNITS[unitIndex]}`;
}

/** "1.5 GB / 3.0 GB (50%)" -- bytes *and* percentage together (FR-12). */
export function formatProgress(
  status: Pick<ModelDownloadStatus, "downloaded_bytes" | "total_bytes" | "percent">,
): string {
  return `${formatBytes(status.downloaded_bytes)} / ${formatBytes(status.total_bytes)} (${Math.round(
    status.percent,
  )}%)`;
}

/** Whether `state` is still an active transfer worth polling for. */
export function isInProgress(state: ModelDownloadState): boolean {
  return state === "downloading" || state === "verifying";
}

/**
 * Polls `getStatus` every `intervalMs`, reporting each result to `onUpdate`.
 * Stops itself automatically once a result is no longer in progress (FR-12
 * acceptance: a terminal state -- complete, cancelled or error -- needs no
 * further polling), and a transient rejection is swallowed rather than
 * killing the loop (the next tick simply tries again). Returns a stop
 * function safe to call more than once (component unmount racing a
 * self-stop).
 */
export function pollDownloadStatus(
  getStatus: () => Promise<ModelDownloadStatus>,
  onUpdate: (status: ModelDownloadStatus) => void,
  intervalMs: number = MODEL_DOWNLOAD_POLL_INTERVAL_MS,
): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setInterval> | undefined;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer !== undefined) {
      clearInterval(timer);
      timer = undefined;
    }
  };

  const tick = () => {
    if (stopped) return;
    getStatus()
      .then((status) => {
        if (stopped) return;
        onUpdate(status);
        if (!isInProgress(status.state)) {
          stop();
        }
      })
      .catch(() => {
        // Transient poll failure: never let it kill the loop -- the next
        // tick retries on its own.
      });
  };

  timer = setInterval(tick, intervalMs);
  return stop;
}
