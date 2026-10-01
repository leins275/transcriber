import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./RecordingPage.module.css";
import { MeetingEditor } from "./MeetingEditor";
import { NotePanel } from "./NotePanel";
import { ProjectRosterPanel } from "./ProjectRosterPanel";
import { SummaryPanel } from "./SummaryPanel";
import { TranscriptViewer } from "./TranscriptViewer";
import { VoiceMemoryPanel } from "./VoiceMemoryPanel";
import { formatDuration } from "../lib/format";
import { formatMeetingDate, parseEntryName } from "../lib/meetingName";
import { EMPTY_ROSTER } from "../lib/roster";
import { speakerNames } from "../lib/turns";
import { groupIntoTurns } from "../lib/turns";
import type {
  JobType,
  MeetingUpdate,
  NoteView,
  ProjectRosterView,
  SummaryView,
  TranscriptLanguage,
  TranscriptView,
  VaultMeetingView,
  VoiceStatusView,
} from "../types";

/** A meeting's folder name, the way the service names it in its lists. */
function folderName(meetingDir: string): string {
  const parts = meetingDir.split(/[\\/]/).filter((part) => part !== "");
  return parts[parts.length - 1] ?? meetingDir;
}

export type RecordingPageProps = {
  entry: VaultMeetingView;
  projects: string[];
  /** Project-level speaker memory: names assigned across this meeting's
   * project siblings, suggested while typing a speaker name. */
  projectSpeakers: string[];
  /** The roster of the project this meeting is filed under, as loaded by
   * the app. Absent while it loads — and meaningless for an unfiled
   * recording, which is why it never reaches the transcript in that case. */
  projectRoster?: ProjectRosterView;
  /** Persists the roster edited in the panel. The project is passed back
   * explicitly: the page owns which project the open meeting belongs to. */
  onSaveRoster?: (project: string, roster: ProjectRosterView) => Promise<void>;
  /** Reads the voice memory as the given project sees it; the "Project
   * speakers" panel shows it under the roster. Absent, the section is not
   * rendered. */
  onLoadVoiceMemory?: (project: string) => Promise<VoiceStatusView>;
  /** Vouches for the names speaker recognition gave this meeting. */
  onConfirmSpeakers?: (entryId: string) => Promise<void>;
  onBack: () => void;
  onReveal: (entryId: string) => void;
  onReadTranscript: (entryId: string) => Promise<TranscriptView>;
  onReadSummary: (entryId: string) => Promise<SummaryView>;
  onReadNote: (entryId: string) => Promise<NoteView>;
  onSaveNote: (entryId: string, markdown: string) => Promise<void>;
  onSaveSpeakers: (entryId: string, assignments: Record<string, string>) => Promise<void>;
  onUpdate: (entryId: string, update: MeetingUpdate) => Promise<void>;
  onDelete: (entryId: string) => Promise<void>;
  /** `language` is the operator's per-recording override; `null` is Auto,
   * which leaves the service on its constrained {ru, en} detection. */
  onTranscribe: (entryId: string, language: TranscriptLanguage | null) => Promise<void>;
  /** The LLM feature's on-demand jobs over this recording. */
  onSummarize: (entryId: string) => Promise<void>;
  onExportPdf: (entryId: string) => Promise<void>;
  /** Speaker identification over this recording's existing transcript. */
  onDiarize: (entryId: string) => Promise<void>;
  /** Whether speaker identification is set up (runtime and models on
   * disk); the menu item renders disabled otherwise, pointing at Settings. */
  speakersReady: boolean;
  /** Derived-job types currently in flight for this entry — the matching
   * controls render busy instead of firing twice. */
  activeLlmJobs: JobType[];
  /** Bumped when a summarize job for this entry finishes, so the summary
   * tab re-reads `summary.md`. */
  summaryReloadToken: number;
  /** Asks the language model for a short title out of this recording's
   * summary. The answer comes back as `titleSuggestion`; nothing is renamed
   * by asking. */
  onSuggestTitle?: (entryId: string) => Promise<void>;
  /** Whether a language model is installed — without one the suggest-title
   * action is not offered at all. */
  llmReady?: boolean;
  /** A finished title suggestion for this recording that the operator has
   * not been shown yet. Arriving, it opens the rename form with the title
   * prefilled — the operator still has to save it. */
  titleSuggestion?: TitleSuggestion | null;
  /** Reports that `titleSuggestion` has been put in front of the operator,
   * so the app stops offering it. */
  onTitleSuggestionShown?: (jobId: string) => void;
};

/** One finished `suggest_title` job's answer. */
export type TitleSuggestion = {
  /** The job that produced it — what tells two suggestions apart. */
  jobId: string;
  title: string;
};

type Tab = "transcript" | "summary" | "note";
type Panel = "none" | "edit" | "delete" | "roster";

/** The languages the app can name. A transcript written before this feature —
 * or in anything outside the operator's universe — carries a code we do not
 * label, and the meta line then shows nothing rather than a placeholder. */
const LANGUAGE_NAMES: Record<string, string | undefined> = {
  ru: "Russian",
  en: "English",
  tr: "Turkish",
};

/** The overflow menu's transcribe choices: the language is picked on the
 * menu item itself, not in a separate toolbar dropdown. */
const TRANSCRIBE_CHOICES: { label: string; suffix: string; language: TranscriptLanguage | null }[] =
  [
    { label: "Auto", suffix: "(Auto)", language: null },
    { label: "Russian", suffix: "in Russian", language: "ru" },
    { label: "English", suffix: "in English", language: "en" },
    { label: "Turkish", suffix: "in Turkish", language: "tr" },
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
  projectSpeakers,
  projectRoster,
  onSaveRoster,
  onLoadVoiceMemory,
  onConfirmSpeakers,
  onBack,
  onReveal,
  onReadTranscript,
  onReadSummary,
  onReadNote,
  onSaveNote,
  onSaveSpeakers,
  onUpdate,
  onDelete,
  onTranscribe,
  onSummarize,
  onExportPdf,
  onDiarize,
  speakersReady,
  activeLlmJobs,
  summaryReloadToken,
  onSuggestTitle,
  llmReady = false,
  titleSuggestion = null,
  onTitleSuggestionShown,
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
  const [noteText, setNoteText] = useState<string | null>(null);
  // The note panel reports an unsaved draft; Back is guarded on it.
  const [noteDirty, setNoteDirty] = useState(false);
  // The title suggestion the open rename form was prefilled with, if any.
  const [prefilledTitle, setPrefilledTitle] = useState<TitleSuggestion | null>(null);

  // Opening a different recording resets the page-local view state; stale
  // panel content must never survive into another meeting's Copy.
  useEffect(() => {
    setTab("transcript");
    setPanel("none");
    setMenuOpen(false);
    setSummaryText(null);
    setNoteText(null);
    setNoteDirty(false);
  }, [entry.id]);

  // Closing the rename form — by any route — forgets the suggestion it was
  // prefilled with: reopened by hand, it starts from the meeting's current
  // name again.
  useEffect(() => {
    if (panel !== "edit") setPrefilledTitle(null);
  }, [panel]);

  // A finished title suggestion opens the rename form with the title field
  // prefilled. That is all it does: the folder is renamed only when the
  // operator saves the form. Declared after the two effects above so that
  // a suggestion waiting for this recording survives the page opening (state
  // updates apply in order, and this one comes last).
  const shownSuggestionRef = useRef<string | null>(null);
  useEffect(() => {
    if (!titleSuggestion || shownSuggestionRef.current === titleSuggestion.jobId) return;
    shownSuggestionRef.current = titleSuggestion.jobId;
    setPrefilledTitle(titleSuggestion);
    setPanel("edit");
    onTitleSuggestionShown?.(titleSuggestion.jobId);
  }, [titleSuggestion, onTitleSuggestionShown]);

  // Whether there is a summary to name the meeting after. Only asked while
  // a language model is installed — without one the action is not offered,
  // so there is nothing to find out.
  const [hasSummary, setHasSummary] = useState(false);
  const canSuggestTitle = llmReady && onSuggestTitle !== undefined;
  useEffect(() => {
    if (!canSuggestTitle) {
      setHasSummary(false);
      return;
    }
    let cancelled = false;
    onReadSummary(entry.id)
      .then((summary) => {
        if (!cancelled) setHasSummary(Boolean(summary.markdown?.trim()));
      })
      .catch(() => {
        if (!cancelled) setHasSummary(false);
      });
    return () => {
      cancelled = true;
    };
  }, [canSuggestTitle, entry.id, entry.meeting_dir, onReadSummary, summaryReloadToken]);

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

  // Bumped when this page changes what the voice memory is built from, so
  // an open voice-memory panel reads again and shows the change picked up.
  const [labelsSaved, setLabelsSaved] = useState(0);
  const saveSpeakers = useCallback(
    async (assignments: Record<string, string>) => {
      await onSaveSpeakers(entry.id, assignments);
      setLabelsSaved((count) => count + 1);
    },
    [entry.id, onSaveSpeakers],
  );
  const confirmSpeakers = useCallback(async () => {
    await onConfirmSpeakers?.(entry.id);
  }, [entry.id, onConfirmSpeakers]);

  // The roster belongs to the project, not to the recording: an unfiled
  // meeting has none, and naming a speaker there stays free text however the
  // last opened project was configured.
  const project = entry.project;
  const saveRoster = useCallback(
    async (roster: ProjectRosterView) => {
      if (project === null) return;
      await onSaveRoster?.(project, roster);
    },
    [project, onSaveRoster],
  );

  // Copy acts on the visible tab; a tab with nothing loaded copies nothing.
  const copyText =
    tab === "transcript"
      ? (transcript?.text.trim() ?? null)
      : tab === "summary"
        ? summaryText
        : noteText;

  // A half-typed note must not be lost to a stray click on Back.
  const guardedBack = useCallback(() => {
    if (noteDirty && !window.confirm("Discard unsaved note changes?")) return;
    onBack();
  }, [noteDirty, onBack]);

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

  const parsed = parseEntryName(entry.meeting_name, entry.project);
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
  const diarizing = activeLlmJobs.includes("diarize");
  const suggestingTitle = activeLlmJobs.includes("suggest_title");

  const closeMenuAnd = (action: () => void) => () => {
    setMenuOpen(false);
    action();
  };

  return (
    <section className={styles.page} aria-label="Recording">
      <div className={styles.head}>
        <div className={styles.breadcrumb}>
          <button type="button" className="btn btn-ghost" onClick={guardedBack}>
            ← Recordings
          </button>
          <span className={styles.crumbSeparator}>/</span>
          <span className="pill">{entry.project ?? "unsorted"}</span>
          {project !== null && (
            <button
              type="button"
              className={`btn btn-ghost ${styles.rosterToggle}`}
              aria-pressed={panel === "roster"}
              onClick={() => setPanel((p) => (p === "roster" ? "none" : "roster"))}
            >
              Project speakers
            </button>
          )}
        </div>

        <div className={styles.titleBlock}>
          <div className={styles.titleLine}>
            <h2 className={styles.title}>{parsed ? parsed.title : entry.meeting_name}</h2>
            {parsed?.kind && <span className="pill">{parsed.kind}</span>}
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
            <button
              type="button"
              role="tab"
              id="recording-tab-note"
              aria-selected={tab === "note"}
              aria-controls="recording-panel-note"
              className={styles.tab}
              onClick={() => setTab("note")}
            >
              Note
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
                  {entry.has_transcript && entry.has_source && (
                    <>
                      <button
                        type="button"
                        role="menuitem"
                        className={styles.menuItem}
                        disabled={diarizing || !speakersReady}
                        title={
                          speakersReady
                            ? undefined
                            : "Set up speaker identification in Settings first"
                        }
                        onClick={closeMenuAnd(() => void runLlm(onDiarize))}
                      >
                        {diarizing ? "Identifying speakers…" : "Identify speakers"}
                      </button>
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
                  {canSuggestTitle && hasSummary && (
                    <button
                      type="button"
                      role="menuitem"
                      className={styles.menuItem}
                      disabled={suggestingTitle}
                      onClick={closeMenuAnd(() => {
                        if (onSuggestTitle) void runLlm(onSuggestTitle);
                      })}
                    >
                      {suggestingTitle ? "Suggesting title…" : "Suggest title from summary"}
                    </button>
                  )}
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
          // Keyed by the suggestion so a second one replaces the first in
          // the title field instead of being ignored by a mounted form.
          key={prefilledTitle?.jobId ?? "manual"}
          entry={entry}
          projects={projects}
          suggestedTitle={prefilledTitle?.title}
          onSave={async (update) => {
            await onUpdate(entry.id, update);
            setPanel("none");
          }}
          onCancel={() => setPanel("none")}
        />
      )}

      {panel === "roster" && project !== null && (
        <ProjectRosterPanel
          roster={projectRoster ?? EMPTY_ROSTER}
          siblingNames={projectSpeakers}
          onSave={saveRoster}
          onClose={() => setPanel("none")}
        />
      )}

      {panel === "roster" && project !== null && onLoadVoiceMemory && (
        <VoiceMemoryPanel
          project={project}
          currentMeeting={folderName(entry.meeting_dir)}
          onLoad={onLoadVoiceMemory}
          onConfirm={confirmSpeakers}
          // A finished speaker pass changes the memory as surely as a saved
          // label does: the job leaving the active list moves the token too.
          reloadToken={labelsSaved * 2 + (diarizing ? 1 : 0)}
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
              <TranscriptViewer
                transcript={transcript}
                onSaveSpeakers={saveSpeakers}
                suggestedSpeakers={projectSpeakers}
                roster={project === null ? undefined : projectRoster}
              />
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

        {/* Unlike its siblings, the note panel stays mounted and merely
            hides: it can hold an unsaved draft, and unmounting on a tab
            switch would silently discard it. */}
        <div
          role="tabpanel"
          id="recording-panel-note"
          aria-labelledby="recording-tab-note"
          hidden={tab !== "note"}
        >
          <NotePanel
            key={entry.id}
            entryId={entry.id}
            onLoad={onReadNote}
            onSave={onSaveNote}
            onContentChange={setNoteText}
            onDirtyChange={setNoteDirty}
          />
        </div>
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
