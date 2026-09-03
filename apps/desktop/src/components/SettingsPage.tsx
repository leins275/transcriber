import { useState } from "react";
import styles from "./SettingsPage.module.css";
import { serviceStatusLabel } from "../lib/serviceLabel";
import type { ModelDownloadStatus } from "../lib/modelDownload";
import type {
  DiarizationDownloadStatus,
  DiarizationStatusView,
  EmbeddingModelDownloadStatus,
  LlmCatalogModel,
  LlmModelsView,
  ServiceStatusView,
  SettingsView,
} from "../types";

export type SettingsPageProps = {
  settings: SettingsView;
  serviceStatus: ServiceStatusView;
  modelStatus: ModelDownloadStatus | null;
  /** The curated assistant (LLM) model catalog -- `null` while unknown,
   * which renders the row inert rather than showing a false "missing". */
  llmModels: LlmModelsView | null;
  /** The installed build's version, once known -- `null` simply omits the
   * row rather than showing a placeholder. */
  appVersion: string | null;
  onBack: () => void;
  onChangeRoot: () => void;
  onStartLlmModelDownload: (modelId: string) => void;
  onCancelLlmModelDownload: (modelId: string) => void;
  /** The search-embedding (bge-m3) download slot -- `null` while unknown,
   * which renders the row inert rather than showing a false "missing". */
  embeddingStatus: EmbeddingModelDownloadStatus | null;
  onStartEmbeddingModelDownload: () => void;
  onCancelEmbeddingModelDownload: () => void;
  /** Queues an incremental search-index pass over the whole vault. */
  onReindex: () => Promise<void>;
  /** Speaker identification's prerequisites -- `null` while unknown (an
   * older or unreachable service), which renders the row inert. */
  diarization: DiarizationStatusView | null;
  /** The two download slots, `null` while unknown. */
  diarizationRuntimeDownload: DiarizationDownloadStatus | null;
  diarizationModelDownload: DiarizationDownloadStatus | null;
  onStartDiarizationRuntimeDownload: () => void;
  onCancelDiarizationRuntimeDownload: () => void;
  onStartDiarizationModelDownload: () => void;
  onCancelDiarizationModelDownload: () => void;
  /** Stores the Hugging Face token (the service restarts to read it). */
  onSaveHfToken: (token: string) => Promise<void>;
  /** Flips `diarize` for new recordings (the service restarts to read it). */
  onSetDiarizeEnabled: (enabled: boolean) => Promise<void>;
  /** Queues speaker identification over every hand-labelled meeting that
   * never had a pass; resolves to how many were queued. */
  onDiarizeLabelledMeetings: () => Promise<number>;
};

function isDownloading(status: DiarizationDownloadStatus | null): boolean {
  return status?.state === "downloading" || status?.state === "verifying";
}

/** A URL the operator has to visit in a browser. The webview is granted no
 * opener/shell permission by design (`capabilities/default.json`), so the
 * link is shown verbatim with a Copy button rather than as an anchor that
 * would silently do nothing. */
function CopyLink({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard unavailable: the URL is still on screen to select.
    }
  };
  return (
    <span className={styles.link}>
      <span className="mono">{url}</span>
      <button
        type="button"
        className={`btn btn-ghost ${styles.copy}`}
        aria-label={`Copy ${url}`}
        onClick={() => void copy()}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}

function errorMessageOf(caught: unknown): string {
  return typeof caught === "object" && caught !== null && "message" in caught
    ? String((caught as { message: unknown }).message)
    : String(caught);
}

function extensionList(extensions: string[]): string {
  return extensions.map((ext) => ext.replace(/^\./, "")).join(" · ");
}

function sizeLabel(sizeBytes: number | null): string {
  if (sizeBytes === null) return "";
  return `~${(sizeBytes / 1_000_000_000).toFixed(1)} GB`;
}

function isTransferring(model: LlmCatalogModel): boolean {
  return model.download.state === "downloading" || model.download.state === "verifying";
}

const CHECK_ICON = (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="var(--accent)"
    strokeWidth="2.5"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polyline points="20 6 9 17 4 12"></polyline>
  </svg>
);

/** The one curated model's row in the Assistant section. The catalog is
 * deliberately a single model with no switching, so the row's only actions
 * are download-shaped. */
function LlmModelRow({
  model,
  gpuBuildPresent,
  anyTransferring,
  onStartDownload,
  onCancelDownload,
}: {
  model: LlmCatalogModel;
  gpuBuildPresent: boolean | null;
  anyTransferring: boolean;
  onStartDownload: (modelId: string) => void;
  onCancelDownload: (modelId: string) => void;
}) {
  const transferring = isTransferring(model);
  const size = sizeLabel(model.size_bytes);
  return (
    <div>
      <div className={styles.line}>
        {model.present && CHECK_ICON}
        {model.label}
        {size && <span className={styles.detail}>{size}</span>}
        {transferring ? (
          <>
            {model.present
              ? `Downloading GPU acceleration · ${Math.round(model.download.percent)}%`
              : `Downloading · ${Math.round(model.download.percent)}%`}
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => onCancelDownload(model.id)}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            {!model.present && (
              <button
                type="button"
                className="btn btn-secondary"
                disabled={anyTransferring}
                onClick={() => onStartDownload(model.id)}
              >
                Download {size && `(${size})`}
              </button>
            )}
            {model.present && gpuBuildPresent === false && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => onStartDownload(model.id)}
              >
                Enable GPU acceleration (~460 MB)
              </button>
            )}
          </>
        )}
      </div>
      {model.download.error_message && (
        <p className={styles.warning}>{model.download.error_message}</p>
      )}
    </div>
  );
}

/**
 * The Settings page (redesign turn 6): everything the old sidebar carried --
 * vault, model, service, accepted formats -- as a labelled ledger, reached
 * from the header's gear. A page rather than a modal for the same reason the
 * recording view is a page: this app has a handful of places to be, and
 * each gets the whole window.
 *
 * Presentational only: no invoke, no listen, no fetch (T6) -- App.tsx owns
 * the data and passes every action down.
 */
export function SettingsPage({
  settings,
  serviceStatus,
  modelStatus,
  llmModels,
  appVersion,
  onBack,
  onChangeRoot,
  onStartLlmModelDownload,
  onCancelLlmModelDownload,
  embeddingStatus,
  onStartEmbeddingModelDownload,
  onCancelEmbeddingModelDownload,
  onReindex,
  diarization,
  diarizationRuntimeDownload,
  diarizationModelDownload,
  onStartDiarizationRuntimeDownload,
  onCancelDiarizationRuntimeDownload,
  onStartDiarizationModelDownload,
  onCancelDiarizationModelDownload,
  onSaveHfToken,
  onSetDiarizeEnabled,
  onDiarizeLabelledMeetings,
}: SettingsPageProps) {
  const anyLlmTransferring = llmModels?.models.some(isTransferring) ?? false;
  const [reindexState, setReindexState] = useState<"idle" | "queueing" | "queued" | "failed">(
    "idle",
  );
  const [reindexError, setReindexError] = useState<string | null>(null);

  // Speaker identification: the token draft (the stored token never comes
  // back, so the box is always empty until typed into), and the outcome of
  // the last save / switch / backfill request, each surfaced inline.
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenState, setTokenState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [speakersError, setSpeakersError] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [backfillState, setBackfillState] = useState<
    { kind: "idle" } | { kind: "queueing" } | { kind: "queued"; count: number }
  >({ kind: "idle" });
  const speakersReady = !!diarization?.runtime_present && !!diarization?.model_present;
  const serviceReady = serviceStatus.state === "ready";

  const saveToken = () => {
    setTokenState("saving");
    setSpeakersError(null);
    onSaveHfToken(tokenDraft.trim())
      .then(() => {
        setTokenState("saved");
        setTokenDraft("");
      })
      .catch((caught: unknown) => {
        setTokenState("failed");
        setSpeakersError(errorMessageOf(caught));
      });
  };

  const toggleDiarize = (enabled: boolean) => {
    setSwitching(true);
    setSpeakersError(null);
    onSetDiarizeEnabled(enabled)
      .catch((caught: unknown) => setSpeakersError(errorMessageOf(caught)))
      .finally(() => setSwitching(false));
  };

  const backfill = () => {
    setBackfillState({ kind: "queueing" });
    setSpeakersError(null);
    onDiarizeLabelledMeetings()
      .then((count) => setBackfillState({ kind: "queued", count }))
      .catch((caught: unknown) => {
        setBackfillState({ kind: "idle" });
        setSpeakersError(errorMessageOf(caught));
      });
  };

  const reindex = () => {
    setReindexState("queueing");
    setReindexError(null);
    onReindex()
      .then(() => setReindexState("queued"))
      .catch((caught: unknown) => {
        setReindexState("failed");
        const message =
          typeof caught === "object" && caught !== null && "message" in caught
            ? String((caught as { message: unknown }).message)
            : String(caught);
        setReindexError(message);
      });
  };
  return (
    <section className={styles.page} role="region" aria-label="Settings">
      <button type="button" className={`btn btn-ghost ${styles.back}`} onClick={onBack}>
        ← Recordings
      </button>
      <h2 className={styles.title}>Settings</h2>

      <div className={styles.row}>
        <div className={styles.kicker}>Vault</div>
        <div className={styles.value}>
          <div className={styles.line}>
            <span className={`${styles.path} mono`}>{settings.meetings_root ?? "(not set)"}</span>
            <button type="button" className="btn btn-secondary" onClick={onChangeRoot}>
              Change…
            </button>
          </div>
          {settings.meetings_root && !settings.meetings_root_exists && (
            <p className={styles.warning}>
              This folder no longer exists. Choose a valid meetings folder.
            </p>
          )}
          <p className={styles.hint}>
            Where recordings and transcripts are filed. Everything stays on this machine.
          </p>
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Model</div>
        <div className={styles.value}>
          {modelStatus ? (
            modelStatus.model_present ? (
              <div className={styles.line}>
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="var(--accent)"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
                large-v3 installed
              </div>
            ) : (
              <>
                <div className={styles.line}>large-v3 not installed</div>
                <p className={styles.hint}>Download it from the notice on the Recordings page.</p>
              </>
            )
          ) : (
            <div className={styles.line}>large-v3</div>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Assistant</div>
        <div className={styles.value}>
          {llmModels === null ? (
            <div className={styles.line}>Local language model</div>
          ) : (
            <>
              {llmModels.models.map((model) => (
                <LlmModelRow
                  key={model.id}
                  model={model}
                  gpuBuildPresent={llmModels.gpu_build_present}
                  anyTransferring={anyLlmTransferring}
                  onStartDownload={onStartLlmModelDownload}
                  onCancelDownload={onCancelLlmModelDownload}
                />
              ))}
              <p className={styles.hint}>
                {llmModels.gpu_build_present === false
                  ? "Summaries currently run on CPU. Enabling GPU acceleration downloads the " +
                    "CUDA build of the local runtime and offloads as much of the model as " +
                    "fits in your GPU's memory."
                  : "Summaries and action items run on this machine, on the one built-in model."}
              </p>
            </>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Search</div>
        <div className={styles.value}>
          {embeddingStatus && (
            <div>
              <div className={styles.line}>
                {embeddingStatus.model_present && CHECK_ICON}
                Vector search (BGE-M3)
                {embeddingStatus.state === "downloading" ||
                embeddingStatus.state === "verifying" ? (
                  <>
                    {`Downloading · ${Math.round(embeddingStatus.percent)}%`}
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={onCancelEmbeddingModelDownload}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  !embeddingStatus.model_present && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={serviceStatus.state !== "ready"}
                      onClick={onStartEmbeddingModelDownload}
                    >
                      Enable vector search (~630 MB)
                    </button>
                  )
                )}
              </div>
              {embeddingStatus.error_message && (
                <p className={styles.warning}>{embeddingStatus.error_message}</p>
              )}
              {!embeddingStatus.model_present && (
                <p className={styles.hint}>
                  Without it, search matches words only. The embedding model lets search and chat
                  find meetings by meaning; once downloaded, the index re-embeds automatically.
                </p>
              )}
            </div>
          )}
          <div className={styles.line}>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={reindexState === "queueing" || serviceStatus.state !== "ready"}
              onClick={reindex}
            >
              {reindexState === "queueing" ? "Queueing…" : "Rebuild search index"}
            </button>
            {reindexState === "queued" && (
              <span className={styles.detail}>Queued — runs after any current job.</span>
            )}
          </div>
          {reindexError && <p className={styles.warning}>{reindexError}</p>}
          <p className={styles.hint}>
            The index updates itself after every transcription and note save; this catches up a
            vault that changed outside the app. Incremental — unchanged meetings are skipped.
          </p>
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Speakers</div>
        <div className={styles.value}>
          {diarization === null ? (
            <div className={styles.line}>Speaker identification</div>
          ) : !diarization.gpu_present ? (
            <>
              <div className={styles.line}>Speaker identification</div>
              <p className={styles.hint}>Needs an NVIDIA GPU; not available on this machine.</p>
            </>
          ) : (
            <>
              <div className={styles.line}>
                {diarization.runtime_present && CHECK_ICON}
                Speaker runtime (pyannote + CUDA torch)
                {isDownloading(diarizationRuntimeDownload) ? (
                  <>
                    {`Downloading · ${Math.round(diarizationRuntimeDownload?.percent ?? 0)}%`}
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={onCancelDiarizationRuntimeDownload}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  !diarization.runtime_present && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={!serviceReady}
                      onClick={onStartDiarizationRuntimeDownload}
                    >
                      Enable speaker identification ({sizeLabel(diarization.runtime_total_bytes)})
                    </button>
                  )
                )}
              </div>
              {diarizationRuntimeDownload?.error_message && (
                <p className={styles.warning}>{diarizationRuntimeDownload.error_message}</p>
              )}

              {/* The installer ships the models, so the token step only ever
                  shows on a build without them (a dev environment, or an
                  installer built without the HF_TOKEN secret). */}
              {!diarization.model_present && (
                <>
                  <div className={styles.line}>
                    {diarization.token_present && CHECK_ICON}
                    Hugging Face token
                    <input
                      type="password"
                      className={styles.tokenInput}
                      aria-label="Hugging Face token"
                      placeholder={diarization.token_present ? "Saved · paste to replace" : "hf_…"}
                      autoComplete="off"
                      value={tokenDraft}
                      onChange={(event) => setTokenDraft(event.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={tokenState === "saving" || tokenDraft.trim() === ""}
                      onClick={saveToken}
                    >
                      {tokenState === "saving" ? "Saving…" : "Save token"}
                    </button>
                    {tokenState === "saved" && <span className={styles.detail}>Saved</span>}
                  </div>
                  <ol className={styles.steps} aria-label="Token setup steps">
                    <li>
                      Sign in (or create a free account) at{" "}
                      <CopyLink url="https://huggingface.co/join" />
                    </li>
                    <li>
                      Open{" "}
                      <CopyLink url="https://huggingface.co/pyannote/speaker-diarization-3.1" /> and
                      click <b>Agree and access repository</b> (a short form; the model is free).
                    </li>
                    <li>
                      Do the same at{" "}
                      <CopyLink url="https://huggingface.co/pyannote/segmentation-3.0" />.
                    </li>
                    <li>
                      Open <CopyLink url="https://huggingface.co/settings/tokens" />, choose{" "}
                      <b>Create new token</b> with the <b>Read</b> type, and copy it.
                    </li>
                    <li>Paste it above and Save, then download the speaker models below.</li>
                  </ol>
                  <p className={styles.hint}>
                    The token stays in this machine&apos;s config file and is used only to fetch the
                    models; nothing else ever leaves this machine.
                  </p>
                </>
              )}

              <div className={styles.line}>
                {diarization.model_present && CHECK_ICON}
                Speaker models (pyannote 3.1)
                {isDownloading(diarizationModelDownload) ? (
                  <>
                    {`Downloading · ${Math.round(diarizationModelDownload?.percent ?? 0)}%`}
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={onCancelDiarizationModelDownload}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  !diarization.model_present && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={!serviceReady || !diarization.token_present}
                      onClick={onStartDiarizationModelDownload}
                    >
                      Download speaker models
                    </button>
                  )
                )}
              </div>
              {diarizationModelDownload?.error_message && (
                <p className={styles.warning}>{diarizationModelDownload.error_message}</p>
              )}

              <div className={styles.line}>
                <label className={styles.toggle}>
                  <input
                    type="checkbox"
                    checked={settings.diarize}
                    disabled={switching || !speakersReady}
                    onChange={(event) => toggleDiarize(event.target.checked)}
                  />
                  Identify speakers in new recordings
                </label>
              </div>

              <div className={styles.line}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={!serviceReady || !speakersReady || backfillState.kind === "queueing"}
                  onClick={backfill}
                >
                  {backfillState.kind === "queueing"
                    ? "Queueing…"
                    : "Identify speakers in labelled meetings"}
                </button>
                {backfillState.kind === "queued" && (
                  <span className={styles.detail}>
                    {backfillState.count === 0
                      ? "Nothing to do — every labelled meeting already has its speakers identified."
                      : `Queued ${backfillState.count} meeting${backfillState.count === 1 ? "" : "s"} — they run one after another.`}
                  </span>
                )}
              </div>
              {speakersError && <p className={styles.warning}>{speakersError}</p>}
              <p className={styles.hint}>
                Voices you have named by hand become recognizable: this attaches speaker labels and
                voice prints to every meeting you labelled before identification was set up, and new
                recordings in the same project then open with those names already filled in. Your
                labels are never changed.
              </p>
            </>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Service</div>
        <div className={styles.value}>
          <div className={styles.line}>
            <span className={styles.dot} data-state={serviceStatus.state} />
            {serviceStatusLabel(serviceStatus.state, modelStatus?.cuda_runtime_present)}
          </div>
          {serviceStatus.base_url && (
            <div className={`${styles.detail} mono`}>{serviceStatus.base_url}</div>
          )}
          {serviceStatus.state === "unavailable" && (
            <p className={styles.hint}>Filing still works.</p>
          )}
        </div>
      </div>

      <div className={styles.row}>
        <div className={styles.kicker}>Accepted formats</div>
        <div className={styles.value}>
          <div className={`${styles.detail} mono`}>
            {extensionList(settings.supported_extensions)}
          </div>
        </div>
      </div>

      {appVersion && (
        <div className={styles.row}>
          <div className={styles.kicker}>Version</div>
          <div className={styles.value}>
            <div className={styles.line}>Transcriber v{appVersion}</div>
          </div>
        </div>
      )}
    </section>
  );
}
