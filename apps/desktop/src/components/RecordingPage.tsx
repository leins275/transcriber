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
  TranscriptLanguage,
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
  /** `language` is the operator's per-recording override; `null` is Auto,
   * which leaves the service on its constrained {ru, en} detection. */
  onTranscribe: (entryId: string, language: TranscriptLanguage | null) => Promise<void>;
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
/** What the language picker holds. `"auto"` is the default and the only
 * value that sends no override at all. */
type LanguageChoice = "auto" | TranscriptLanguage;

/** The languages the app can name. A transcript written before this feature —
 * or in anything outside the operator's two — carries a code we do not label,
 * and the indicator then shows nothing rather than a placeholder. */
const LANGUAGE_NAMES: Record<string, string | undefined> = {
  ru: "Russian",
  en: "English",
};

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
  // Local to the page and deliberately not persisted: the choice belongs to
  // this one transcribe run, not to the recording or the app.
  const [language, setLanguage] = useState<LanguageChoice>("auto");

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
      await onTranscribe(entry.id, language === "auto" ? null : language);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy(false);
    }
  }, [entry.id, language, onTranscribe]);

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
    speakers.length > 0 ? `${speakers.length} speaker${speakers.length === 1 ? "" : "s"}` : null,
    transcript?.model,
    transcript?.device,
  ].filter((part): part is string => Boolean(part));
  // The decode language is the one crumb the operator acts on — a wrong one
  // means "re-transcribe with an override" — so it leaves the run-together
  // provenance line and gets named in full beside it.
  const languageName = transcript?.language ? LANGUAGE_NAMES[transcript.language] : undefined;

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
            <div className={styles.metaRow}>
              {languageName && (
                <span className="pill" aria-label={`Language: ${languageName}`}>
                  {languageName}
                </span>
              )}
              <span className={styles.meta}>{meta.join(" · ")}</span>
            </div>
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
              // The picker travels with the button it modifies, so the
              // operator reads "Auto · Re-transcribe" as one sentence. The
              // visible word is short; the accessible name says which
              // language it means.
              <span className={styles.transcribeGroup}>
                <span className={styles.languageLabel} aria-hidden="true">
                  Language
                </span>
                <select
                  className={styles.language}
                  aria-label="Transcript language"
                  value={language}
                  disabled={busy}
                  onChange={(event) => setLanguage(event.target.value as LanguageChoice)}
                >
                  <option value="auto">Auto</option>
                  <option value="ru">Russian</option>
                  <option value="en">English</option>
                </select>
                <button type="button" className="btn" disabled={busy} onClick={transcribe}>
                  {busy ? "Queueing…" : entry.has_transcript ? "Re-transcribe" : "Transcribe"}
                </button>
              </span>
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
