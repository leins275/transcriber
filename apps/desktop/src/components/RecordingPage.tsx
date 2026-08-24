import { useCallback, useEffect, useState } from "react";
import styles from "./RecordingPage.module.css";
import { MeetingEditor } from "./MeetingEditor";
import { SummaryPanel } from "./SummaryPanel";
import { TranscriptViewer } from "./TranscriptViewer";
import { formatDuration } from "../lib/format";
import { formatMeetingDate, parseMeetingName } from "../lib/meetingName";
import { speakerNames } from "../lib/turns";
import { groupIntoTurns } from "../lib/turns";
import type {
  ArtifactKind,
  JobType,
  MeetingUpdate,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
} from "../types";

export type RecordingPageProps = {
  entry: VaultMeetingView;
  projects: string[];
  onBack: () => void;
  onReveal: (entryId: string) => void;
  onReadTranscript: (entryId: string) => Promise<TranscriptView>;
  onReadSummary: (entryId: string) => Promise<SummaryView>;
  onSaveSpeakers: (entryId: string, assignments: Record<string, string>) => Promise<void>;
  onUpdate: (entryId: string, update: MeetingUpdate) => Promise<void>;
  onDelete: (entryId: string) => Promise<void>;
  onTranscribe: (entryId: string) => Promise<void>;
  /** The LLM feature's on-demand jobs over this recording. */
  onSummarize: (entryId: string) => Promise<void>;
  onExtract: (entryId: string, kind: ArtifactKind) => Promise<void>;
  onExportPdf: (entryId: string) => Promise<void>;
  /** Derived-job types currently in flight for this entry — the matching
   * buttons render busy instead of firing twice. */
  activeLlmJobs: JobType[];
  /** Bumped when a summarize job for this entry finishes, so the summary
   * tab re-reads `summary.md`. */
  summaryReloadToken: number;
};

type Tab = "transcript" | "summary";
type Panel = "none" | "edit" | "delete";

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * One recording, given the whole window.
 *
 * A page rather than a row that expands: reading an hour of transcript is
 * not something to do through a keyhole in a list, and everything else about
 * the recording — where it is, how it was transcribed, what to do with it —
 * belongs beside the text rather than three clicks away.
 *
 * Presentational apart from its callbacks: no invoke, no listen, no fetch.
 */
export function RecordingPage({
  entry,
  projects,
  onBack,
  onReveal,
  onReadTranscript,
  onReadSummary,
  onSaveSpeakers,
  onUpdate,
  onDelete,
  onTranscribe,
  onSummarize,
  onExtract,
  onExportPdf,
  activeLlmJobs,
  summaryReloadToken,
}: RecordingPageProps) {
  const [tab, setTab] = useState<Tab>("transcript");
  const [panel, setPanel] = useState<Panel>("none");
  const [transcript, setTranscript] = useState<TranscriptView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!entry.has_transcript) {
      setTranscript(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    onReadTranscript(entry.id)
      .then((loaded) => {
        if (!cancelled) setTranscript(loaded);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(messageOf(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entry.id, entry.has_transcript, entry.meeting_dir, onReadTranscript]);

  const saveSpeakers = useCallback(
    (assignments: Record<string, string>) => onSaveSpeakers(entry.id, assignments),
    [entry.id, onSaveSpeakers],
  );

  const copyAll = useCallback(async () => {
    if (!transcript) return;
    try {
      await navigator.clipboard.writeText(transcript.text.trim());
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (caught) {
      setError(messageOf(caught));
    }
  }, [transcript]);

  const transcribe = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await onTranscribe(entry.id);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  }, [entry.id, onTranscribe]);

  const runLlm = useCallback(
    async (action: (entryId: string) => Promise<void>) => {
      setError(null);
      try {
        await action(entry.id);
      } catch (caught) {
        setError(messageOf(caught));
      }
    },
    [entry.id],
  );

  const confirmDelete = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await onDelete(entry.id);
    } catch (caught) {
      setError(messageOf(caught));
      setBusy(false);
    }
  }, [entry.id, onDelete]);

  const parsed = parseMeetingName(entry.meeting_name);
  const turns = transcript ? groupIntoTurns(transcript.segments, transcript.speakers) : [];
  const speakers = speakerNames(turns);

  const meta = [
    parsed ? formatMeetingDate(parsed.date) : null,
    transcript?.duration_sec != null ? formatDuration(transcript.duration_sec) : null,
    transcript?.language ? transcript.language.toUpperCase() : null,
    speakers.length > 0 ? `${speakers.length} speaker${speakers.length === 1 ? "" : "s"}` : null,
    transcript?.model,
    transcript?.device,
  ].filter((part): part is string => Boolean(part));

  return (
    <section className={styles.page} aria-label="Recording">
      <div className={styles.head}>
        <div className={styles.breadcrumb}>
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            ← Recordings
          </button>
          <span className={styles.crumbSeparator}>/</span>
          <span className="pill">{entry.project ?? "unsorted"}</span>
        </div>

        <div className={styles.titleRow}>
          <div className={styles.titleBlock}>
            <h2 className={styles.title}>{parsed ? parsed.title : entry.meeting_name}</h2>
            <div className={styles.meta}>{meta.join(" · ")}</div>
          </div>
          <div className={styles.actions}>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={!transcript}
              onClick={copyAll}
            >
              {copied ? "Copied" : "Copy all"}
            </button>
            {entry.has_source && (
              <button type="button" className="btn" disabled={busy} onClick={transcribe}>
                {busy ? "Queueing…" : entry.has_transcript ? "Re-transcribe" : "Transcribe"}
              </button>
            )}
            {entry.has_transcript && (
              <>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={activeLlmJobs.includes("summarize")}
                  onClick={() => void runLlm(onSummarize)}
                >
                  {activeLlmJobs.includes("summarize") ? "Summarizing…" : "Summarize"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={activeLlmJobs.includes("action_items")}
                  onClick={() => void runLlm((id) => onExtract(id, "action_items"))}
                >
                  {activeLlmJobs.includes("action_items") ? "Extracting…" : "Action items"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={activeLlmJobs.includes("facts")}
                  onClick={() => void runLlm((id) => onExtract(id, "facts"))}
                >
                  {activeLlmJobs.includes("facts") ? "Extracting…" : "Facts"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={activeLlmJobs.includes("export")}
                  onClick={() => void runLlm(onExportPdf)}
                >
                  {activeLlmJobs.includes("export") ? "Exporting…" : "Export PDF"}
                </button>
              </>
            )}
            <button type="button" className="btn btn-ghost" onClick={() => onReveal(entry.id)}>
              Reveal
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              aria-expanded={panel === "edit"}
              onClick={() => setPanel((p) => (p === "edit" ? "none" : "edit"))}
            >
              Rename
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              aria-expanded={panel === "delete"}
              onClick={() => setPanel((p) => (p === "delete" ? "none" : "delete"))}
            >
              Delete
            </button>
          </div>
        </div>

        <div className={styles.tabs} role="tablist" aria-label="Recording views">
          <button
            type="button"
            role="tab"
            id="recording-tab-transcript"
            aria-selected={tab === "transcript"}
            aria-controls="recording-panel-transcript"
            className={styles.tab}
            onClick={() => setTab("transcript")}
          >
            Transcript
          </button>
          <button
            type="button"
            role="tab"
            id="recording-tab-summary"
            aria-selected={tab === "summary"}
            aria-controls="recording-panel-summary"
            className={styles.tab}
            onClick={() => setTab("summary")}
          >
            Summary
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {panel === "edit" && (
        <MeetingEditor
          entry={entry}
          projects={projects}
          onSave={async (update) => {
            await onUpdate(entry.id, update);
            setPanel("none");
          }}
          onCancel={() => setPanel("none")}
        />
      )}

      {panel === "delete" && (
        <div className={styles.confirm}>
          <p className={styles.confirmText}>
            Move <span className="mono">{entry.meeting_name}</span> — recording, transcript and all
            — to the Recycle Bin? You can restore it from there.
          </p>
          <div className={styles.confirmActions}>
            <button type="button" className="btn" disabled={busy} onClick={confirmDelete}>
              {busy ? "Deleting…" : "Move to Recycle Bin"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => setPanel("none")}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className={styles.body}>
        {tab === "transcript" && (
          <div
            role="tabpanel"
            id="recording-panel-transcript"
            aria-labelledby="recording-tab-transcript"
          >
            {loading ? (
              <p role="status" className={styles.status}>
                Reading transcript…
              </p>
            ) : transcript ? (
              <TranscriptViewer transcript={transcript} onSaveSpeakers={saveSpeakers} />
            ) : (
              <p className={styles.status}>
                {entry.has_source
                  ? "No transcript yet. Transcribe runs this recording through the service again."
                  : "This meeting has neither a recording nor a transcript."}
              </p>
            )}
          </div>
        )}

        {tab === "summary" && (
          <div role="tabpanel" id="recording-panel-summary" aria-labelledby="recording-tab-summary">
            <SummaryPanel
              entryId={entry.id}
              onLoad={onReadSummary}
              reloadToken={summaryReloadToken}
            />
          </div>
        )}
      </div>

      <div className={styles.footer}>
        <span className={`${styles.path} mono`}>
          {transcript?.transcript_path ?? entry.meeting_dir}
        </span>
        <button type="button" className="btn btn-ghost" onClick={() => onReveal(entry.id)}>
          Open folder
        </button>
      </div>
    </section>
  );
}
