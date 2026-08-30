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
  onExportPdf: (entryId: string) => Promise<void>;
  /** Derived-job types currently in flight for this entry — the matching
   * controls render busy instead of firing twice. */
  activeLlmJobs: JobType[];
  /** Bumped when a summarize job for this entry finishes, so the summary
   * tab re-reads `summary.md`. */
  summaryReloadToken: number;
};

type Tab = "transcript" | "summary";
type Panel = "none" | "edit" | "delete";

/** The languages the app can name. A transcript written before this feature —
 * or in anything outside the operator's two — carries a code we do not label,
 * and the meta line then shows nothing rather than a placeholder. */
const LANGUAGE_NAMES: Record<string, string | undefined> = {
  ru: "Russian",
  en: "English",
};

/** The overflow menu's transcribe choices: the language is picked on the
 * menu item itself, not in a separate toolbar dropdown. */
const TRANSCRIBE_CHOICES: { label: string; suffix: string; language: TranscriptLanguage | null }[] =
  [
    { label: "Auto", suffix: "(Auto)", language: null },
    { label: "Russian", suffix: "in Russian", language: "ru" },
    { label: "English", suffix: "in English", language: "en" },
  ];

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * One recording, given the whole window — the factored layout.
 *
 * Two rows instead of four: every derived view is a tab (Transcript /
 * Summary), and an empty tab opens to its own Generate
 * button in the content area — the generate verbs never sit in the header.
 * Copy acts on the visible tab. Everything rare lives in the `…` overflow
 * menu: re-transcribe (with its language picked right there), regenerate,
 * reveal, rename and delete. Rename is also the pencil at the title.
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
  onExportPdf,
  activeLlmJobs,
  summaryReloadToken,
}: RecordingPageProps) {
  const [tab, setTab] = useState<Tab>("transcript");
  const [panel, setPanel] = useState<Panel>("none");
  const [menuOpen, setMenuOpen] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  // What the visible tab holds, reported up by the mounted panel, so Copy
  // can act on the tab the operator is looking at.
  const [summaryText, setSummaryText] = useState<string | null>(null);

  // Opening a different recording resets the page-local view state; stale
  // panel content must never survive into another meeting's Copy.
  useEffect(() => {
    setTab("transcript");
    setPanel("none");
    setMenuOpen(false);
    setSummaryText(null);
  }, [entry.id]);

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

  // Copy acts on the visible tab; a tab with nothing loaded copies nothing.
  const copyText = tab === "transcript" ? (transcript?.text.trim() ?? null) : summaryText;

  const copyVisible = useCallback(async () => {
    if (!copyText) return;
    try {
      await navigator.clipboard.writeText(copyText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (caught) {
      setError(messageOf(caught));
    }
  }, [copyText]);

  const transcribe = useCallback(
    async (language: TranscriptLanguage | null) => {
      setBusy(true);
      setError(null);
      try {
        await onTranscribe(entry.id, language);
      } catch (caught) {
        setError(messageOf(caught));
      } finally {
        setBusy(false);
      }
    },
    [entry.id, onTranscribe],
  );

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

  // One provenance line, the decoded language first — it is the one value
  // the operator acts on (a wrong one means re-transcribe with an override).
  const languageName = transcript?.language ? LANGUAGE_NAMES[transcript.language] : undefined;
  const meta = [
    languageName,
    parsed ? formatMeetingDate(parsed.date) : null,
    transcript?.duration_sec != null ? formatDuration(transcript.duration_sec) : null,
    speakers.length > 0 ? `${speakers.length} speaker${speakers.length === 1 ? "" : "s"}` : null,
    transcript?.model,
    transcript?.device,
  ].filter((part): part is string => Boolean(part));

  const summarizing = activeLlmJobs.includes("summarize");
  const exporting = activeLlmJobs.includes("export");

  const closeMenuAnd = (action: () => void) => () => {
    setMenuOpen(false);
    action();
  };

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

        <div className={styles.titleBlock}>
          <div className={styles.titleLine}>
            <h2 className={styles.title}>{parsed ? parsed.title : entry.meeting_name}</h2>
            <button
              type="button"
              className={`btn btn-ghost ${styles.pencil}`}
              aria-label="Rename"
              onClick={() => setPanel((p) => (p === "edit" ? "none" : "edit"))}
            >
              ✎
            </button>
          </div>
          {meta.length > 0 && <p className={styles.meta}>{meta.join(" · ")}</p>}
        </div>

        <div className={styles.tabRow}>
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

          <div className={styles.toolbar}>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={!copyText}
              onClick={() => void copyVisible()}
            >
              {copied ? "Copied" : "Copy"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => onReveal(entry.id)}>
              Reveal in Explorer
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              aria-label="More actions"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              ⋯
            </button>
            {menuOpen && (
              <>
                <button
                  type="button"
                  className={styles.menuBackdrop}
                  aria-label="Close menu"
                  onClick={() => setMenuOpen(false)}
                />
                <div role="menu" className={styles.menu} aria-label="More actions">
                  {entry.has_source && (
                    <>
                      <span className={styles.menuLabel}>
                        {entry.has_transcript ? "Re-transcribe" : "Transcribe"}
                      </span>
                      {TRANSCRIBE_CHOICES.map((choice) => (
                        <button
                          key={choice.label}
                          type="button"
                          role="menuitem"
                          className={styles.menuItem}
                          disabled={busy}
                          aria-label={`${entry.has_transcript ? "Re-transcribe" : "Transcribe"} ${choice.suffix}`}
                          onClick={closeMenuAnd(() => void transcribe(choice.language))}
                        >
                          {choice.label}
                        </button>
                      ))}
                      <hr className={styles.menuDivider} />
                    </>
                  )}
                  {entry.has_transcript && (
                    <>
                      <button
                        type="button"
                        role="menuitem"
                        className={styles.menuItem}
                        disabled={summarizing}
                        onClick={closeMenuAnd(() => void runLlm(onSummarize))}
                      >
                        Regenerate summary
                      </button>
                      <hr className={styles.menuDivider} />
                    </>
                  )}
                  {entry.has_transcript && (
                    <button
                      type="button"
                      role="menuitem"
                      className={styles.menuItem}
                      disabled={exporting}
                      onClick={closeMenuAnd(() => void runLlm(onExportPdf))}
                    >
                      {exporting ? "Exporting…" : "Export PDF"}
                    </button>
                  )}
                  <button
                    type="button"
                    role="menuitem"
                    className={styles.menuItem}
                    onClick={closeMenuAnd(() => setPanel("edit"))}
                  >
                    Rename
                  </button>
                  <hr className={styles.menuDivider} />
                  <button
                    type="button"
                    role="menuitem"
                    className={`${styles.menuItem} ${styles.menuDanger}`}
                    onClick={closeMenuAnd(() => setPanel("delete"))}
                  >
                    Delete recording…
                  </button>
                </div>
              </>
            )}
          </div>
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
              <div className={styles.emptyPanel}>
                <p className={styles.status}>
                  {entry.has_source
                    ? "No transcript yet."
                    : "This meeting has neither a recording nor a transcript."}
                </p>
                {entry.has_source && (
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => void transcribe(null)}
                  >
                    {busy ? "Queueing…" : "Transcribe"}
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {tab === "summary" && (
          <div role="tabpanel" id="recording-panel-summary" aria-labelledby="recording-tab-summary">
            <SummaryPanel
              entryId={entry.id}
              onLoad={onReadSummary}
              reloadToken={summaryReloadToken}
              onGenerate={() => void runLlm(onSummarize)}
              busy={summarizing}
              onContentChange={setSummaryText}
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
