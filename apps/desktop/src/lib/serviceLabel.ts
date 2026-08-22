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

export function serviceStatusLabel(
  state: ServiceStatusView["state"],
  cudaRuntimePresent: boolean | null | undefined,
): string {
  if (state === "starting") return "Starting…";
  if (state === "unavailable") return "Unavailable";
  if (cudaRuntimePresent === true) return "Ready · GPU";
  if (cudaRuntimePresent === false) return "Ready · CPU";
  return "Ready";
}
