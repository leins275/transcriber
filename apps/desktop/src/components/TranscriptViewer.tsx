import { useCallback, useMemo, useState } from "react";
import styles from "./TranscriptViewer.module.css";
import { SpeakerTag } from "./SpeakerTag";
import { formatTimecode } from "../lib/format";
import {
  assignSpeaker,
  filterTurns,
  groupIntoTurns,
  renameSpeaker,
  speakerNames,
} from "../lib/turns";
import type { TranscriptView } from "../types";

export type TranscriptViewerProps = {
  transcript: TranscriptView;
  /** Persists the whole `segment id -> speaker` map. Rejections surface
   * inline; the labels stay on screen either way, so a failed save never
   * silently loses the operator's work. */
  onSaveSpeakers?: (assignments: Record<string, string>) => Promise<void>;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * The reading view: a transcript at a book measure, timestamps in the
 * margin, speakers above each turn.
 *
 * Segments are grouped into turns before rendering (`lib/turns`) — Whisper
 * emits a chunk every few seconds, and one paragraph per chunk reads as
 * stutter. Speaker names are the operator's own: nothing detects them, and
 * an unattributed turn says so rather than inventing "Speaker 1".
 *
 * Two views over the same content: **Timeline** (the default — a meeting
 * recording is usually opened to answer "when was that said") and **Plain
 * text** for copying out. Neither is a preference worth persisting.
 *
 * Presentational apart from the save callback: no invoke, no listen, no
 * fetch.
 */
export function TranscriptViewer({ transcript, onSaveSpeakers }: TranscriptViewerProps) {
  const [view, setView] = useState<"timeline" | "text">("timeline");
  const [query, setQuery] = useState("");
  // Held locally so a label lands the instant it is typed; the save is
  // fire-and-report, not something the reader waits on.
  const [speakers, setSpeakers] = useState<Record<string, string>>(transcript.speakers);
  const [saveError, setSaveError] = useState<string | null>(null);

  const turns = useMemo(
    () => groupIntoTurns(transcript.segments, speakers),
    [transcript.segments, speakers],
  );
  const visible = useMemo(() => filterTurns(turns, query), [turns, query]);
  const known = useMemo(() => speakerNames(turns), [turns]);

  const persist = useCallback(
    (next: Record<string, string>) => {
      setSpeakers(next);
      setSaveError(null);
      if (!onSaveSpeakers) return;
      onSaveSpeakers(next).catch((error: unknown) => setSaveError(messageOf(error)));
    },
    [onSaveSpeakers],
  );

  return (
    <div className={styles.viewer}>
      <div className={styles.toolbar}>
        <div className={styles.views} role="group" aria-label="Transcript view">
          <button
            type="button"
            className={styles.viewButton}
            aria-pressed={view === "timeline"}
            onClick={() => setView("timeline")}
          >
            Timeline
          </button>
          <button
            type="button"
            className={styles.viewButton}
            aria-pressed={view === "text"}
            onClick={() => setView("text")}
          >
            Plain text
          </button>
        </div>
        {view === "timeline" && (
          <label className={styles.find}>
            <svg
              width="11"
              height="11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="7"></circle>
              <line x1="21" y1="21" x2="16.6" y2="16.6"></line>
            </svg>
            <input
              className={styles.findInput}
              type="search"
              value={query}
              aria-label="Find in transcript"
              placeholder="Find in transcript"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        )}
      </div>

      {saveError && (
        <p role="alert" className="alert">
          {saveError}
        </p>
      )}

      {view === "timeline" ? (
        turns.length === 0 ? (
          <p className={styles.empty}>This transcript has no segments.</p>
        ) : visible.length === 0 ? (
          <p className={styles.empty}>
            Nothing in this transcript matches <span className="mono">{query.trim()}</span>.
          </p>
        ) : (
          <>
            {query.trim().length > 0 && (
              <p role="status" className={styles.matchCount}>
                {visible.length} of {turns.length} passages match
              </p>
            )}
            <ol className={styles.turns} aria-label="Transcript">
              {visible.map((turn) => (
                <li key={turn.id} className={styles.turn}>
                  <span className={styles.speakerCell}>
                    <SpeakerTag
                      speaker={turn.speaker}
                      known={known}
                      onAssign={(name) => persist(assignSpeaker(speakers, turn, name))}
                      onRename={(from, to) => persist(renameSpeaker(speakers, from, to))}
                    />
                  </span>
                  <span className={`${styles.time} mono`}>{formatTimecode(turn.start)}</span>
                  <p className={styles.text}>{turn.text}</p>
                </li>
              ))}
            </ol>
          </>
        )
      ) : (
        // `readOnly` rather than `disabled`: the operator must still be able
        // to select and copy the text, which a disabled textarea forbids.
        <textarea
          className={styles.plain}
          aria-label="Transcript text"
          readOnly
          value={transcript.text.trim()}
        />
      )}
    </div>
  );
}
