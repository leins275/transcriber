/**
 * Pure formatting for the sidebar's compact service/model line ("Ready ·
 * GPU"). Derives the GPU/CPU suffix from the model download status's
 * existing `cuda_runtime_present` field (T13's frozen shape) -- no new IPC
 * data is needed: `true` means the runtime is installed and in use, `false`
 * means this host has a GPU but the runtime is missing (falls back to CPU),
 * and `null` means the host has no GPU at all, so the suffix is omitted
 * entirely rather than mentioning a runtime it could never use.
 */
import type { ServiceStatusView } from "../types";
import type { EngineRow } from "./modelDownload";

/** The sidebar's word for the device the transcription engine runs on. */
const DEVICE_SUFFIX: Record<string, string> = {
  cuda: "GPU",
  metal: "GPU",
  NPU: "NPU",
  GPU: "GPU",
  cpu: "CPU",
  CPU: "CPU",
};

export function serviceStatusLabel(
  state: ServiceStatusView["state"],
  cudaRuntimePresent: boolean | null | undefined,
  engines: EngineRow[] | undefined = undefined,
): string {
  if (state === "starting") return "Starting…";
  if (state === "unavailable") return "Unavailable";
  // The engine plan, when the service reports one, names the device the
  // transcription actually runs on (an engine whose payload is still
  // missing runs on the CPU fallback meanwhile).
  const whisper = engines?.find((row) => row.stage === "whisper");
  if (whisper) {
    const suffix = whisper.present === false ? "CPU" : DEVICE_SUFFIX[whisper.device];
    return suffix ? `Ready · ${suffix}` : "Ready";
  }
  if (cudaRuntimePresent === true) return "Ready · GPU";
  if (cudaRuntimePresent === false) return "Ready · CPU";
  return "Ready";
}
