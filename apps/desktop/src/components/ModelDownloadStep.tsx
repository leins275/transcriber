import { useEffect, useRef, useState } from "react";
import styles from "./ModelDownloadStep.module.css";
import {
  MODEL_DOWNLOAD_POLL_INTERVAL_MS,
  formatProgress,
  isInProgress,
  pollDownloadStatus,
  type ModelDownloadStatus,
} from "../lib/modelDownload";

/** Injected in production from `api.ts`'s three model-download wrappers; a
 * fake command layer in tests -- this component never imports
 * `@tauri-apps/api` itself (T6/T12 convention). */
export type ModelDownloadCommands = {
  start: () => Promise<ModelDownloadStatus>;
  cancel: () => Promise<ModelDownloadStatus>;
  status: () => Promise<ModelDownloadStatus>;
};

export type ModelDownloadStepProps = {
  commands: ModelDownloadCommands;
  /** The status already known when this step first mounts (from the app's
   * initial `model_download_status` fetch) -- an in-progress state here
   * resumes polling immediately rather than waiting for a click. */
  initialStatus: ModelDownloadStatus;
  /** Compact, non-modal missing-model notice mode, entered after "Skip for
   * now" (FR-17) -- the rest of the app stays usable underneath. */
  compact?: boolean;
  /** Fires once a status carries `model_present: true` (FR-16). */
  onModelPresent?: () => void;
  /** Fires when the operator picks "Skip for now" (FR-17). */
  onSkip?: () => void;
  /** Poll cadence override (tests only; production uses F3's existing
   * 1.5s job-poll cadence, Q3-A). */
  pollIntervalMs?: number;
};

/**
 * First-run model-download step and missing-model recovery (T13, FR-12,
 * FR-16, FR-17). Renders nothing once the model is present. Otherwise shows
 * either the full step (idle/downloading/verifying/cancelled/error) or, in
 * `compact` mode, a persistent one-line notice with the same retry -- the
 * state a "Skip for now" click leaves behind.
 */
export function ModelDownloadStep({
  commands,
  initialStatus,
  compact = false,
  onModelPresent,
  onSkip,
  pollIntervalMs = MODEL_DOWNLOAD_POLL_INTERVAL_MS,
}: ModelDownloadStepProps) {
  const [status, setStatus] = useState<ModelDownloadStatus>(initialStatus);
  const stopPollingRef = useRef<(() => void) | null>(null);
  const reportedPresentRef = useRef(false);

  const stopPolling = () => {
    stopPollingRef.current?.();
    stopPollingRef.current = null;
  };

  const beginPolling = () => {
    stopPolling();
    stopPollingRef.current = pollDownloadStatus(commands.status, setStatus, pollIntervalMs);
  };

  // Resume polling immediately for a download that was already in progress
  // when this step mounted (e.g. the app was restarted mid-transfer).
  useEffect(() => {
    if (isInProgress(initialStatus.state)) {
      beginPolling();
    }
    return stopPolling;
    // Only ever re-run this for the initial mount -- later transitions are
    // driven by handleStart/handleCancel below, not by prop changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (status.model_present && !reportedPresentRef.current) {
      reportedPresentRef.current = true;
      stopPolling();
      onModelPresent?.();
    }
  }, [status.model_present, onModelPresent]);

  const handleStart = () => {
    commands.start().then((next) => {
      setStatus(next);
      // E16: never trust the transient POST state to decide whether to
      // poll. On the production `SetupDownload` path, `already_present()`'s
      // filesystem probe yields the GIL back to the request thread before
      // `self.state` flips to `DOWNLOADING`, so the response reliably reads
      // `idle` even though a transfer is now running in the background.
      // Poll unconditionally instead -- `pollDownloadStatus` already stops
      // itself on the first non-in-progress result, so a genuinely finished
      // start costs one harmless extra request.
      beginPolling();
    });
  };

  const handleCancel = () => {
    stopPolling();
    commands.cancel().then(setStatus);
  };

  const handleSkip = () => {
    onSkip?.();
  };

  if (status.model_present) {
    // E13: surfaces even without an in-session `cuda_warning` -- a fresh
    // process (no `SetupDownload` instance, so `cuda_warning` is always
    // `null`) still learns "CUDA runtime missing" from `/health`'s durable
    // `cuda_runtime_present`. `false` is a strict signal (this host has a
    // GPU and F2 judged the runtime absent); `null` means "no GPU on this
    // machine" and must never prompt for a runtime it could never use.
    const cudaSetupNeeded = Boolean(status.cuda_warning) || status.cuda_runtime_present === false;
    if (!cudaSetupNeeded) {
      return null;
    }
    // E17: a "Retry GPU setup" click re-POSTs `/v1/model/download` (whose
    // `SetupDownload` skips the model phase entirely once it finds it
    // already present, see `model_routes.py`) and E16 now polls
    // unconditionally afterwards -- while that retry is in flight, show its
    // progress instead of letting the notice go blank.
    if (status.state === "downloading" || status.state === "verifying") {
      return (
        <div className={styles.notice} data-state="cuda-warning" role="status">
          <p>
            {status.state === "downloading" ? "Downloading" : "Verifying"} GPU runtime:{" "}
            {formatProgress(status)}
          </p>
        </div>
      );
    }
    // The model is present, but the CUDA-runtime phase failed and the setup
    // continued anyway (E4) -- a persistent notice, not a toast, with the
    // backend's own message verbatim (when one is available) and a retry.
    return (
      <div className={styles.notice} data-state="cuda-warning" role="alert">
        <p>GPU acceleration is not installed. {status.cuda_warning}</p>
        <button type="button" onClick={handleStart}>
          Retry GPU setup
        </button>
      </div>
    );
  }

  if (compact) {
    return (
      <div className={styles.notice} data-state="notice" role="status">
        <p>
          The transcription model is still missing.
          {status.error_message && <> {status.error_message}</>}
        </p>
        <button type="button" onClick={handleStart}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className={styles.step} data-state={status.state}>
      <h2>Download the transcription model</h2>
      {(status.state === "idle" || status.state === "cancelled") && (
        <>
          <p>
            The transcription model is missing.
            {status.state === "cancelled" && " The download was cancelled."} It needs to be
            downloaded once before transcription can run.
          </p>
          <div className={styles.actions}>
            <button type="button" onClick={handleStart}>
              {status.state === "cancelled" ? "Retry" : "Start download"}
            </button>
            <button type="button" onClick={handleSkip}>
              Skip for now
            </button>
          </div>
        </>
      )}
      {(status.state === "downloading" || status.state === "verifying") && (
        <>
          <p role="status">
            {status.state === "downloading" ? "Downloading" : "Verifying"}: {formatProgress(status)}
          </p>
          <div className={styles.actions}>
            <button type="button" onClick={handleCancel}>
              Cancel
            </button>
          </div>
        </>
      )}
      {status.state === "error" && (
        <>
          <p role="alert">{status.error_message ?? "The download failed."}</p>
          <div className={styles.actions}>
            <button type="button" onClick={handleStart}>
              Retry
            </button>
            <button type="button" onClick={handleSkip}>
              Skip for now
            </button>
          </div>
        </>
      )}
    </div>
  );
}
