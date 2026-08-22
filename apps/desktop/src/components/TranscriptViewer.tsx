import { useMemo, useState } from "react";
import styles from "./TranscriptViewer.module.css";
import { formatDuration, formatTimecode } from "../lib/format";
import type { TranscriptView } from "../types";

export type TranscriptViewerProps = {
  transcript: TranscriptView;
};

/** Segments whose text is only whitespace carry no reading value and only
 * make the timeline harder to scan; the underlying file keeps them. */
function readableSegments(transcript: TranscriptView) {
  return transcript.segments.filter((segment) => segment.text.trim().length > 0);
}

/**
 * Reads a meeting's transcript inside the app.
 *
 * Two views over the same content: a **timeline** of timestamped segments
 * (the default — this is a meeting recording, and "when was that said" is
 * the question a transcript is usually opened to answer) and **plain text**
 * for copying out. Neither is a preference worth persisting, so the toggle
 * is local and resets each time a transcript is opened.
 *
 * Presentational only: no invoke, no listen, no fetch — the caller loads the
 * transcript and hands it in.
 */
export function TranscriptViewer({ transcript }: TranscriptViewerProps) {
  const [view, setView] = useState<"timeline" | "text">("timeline");
  const segments = useMemo(() => readableSegments(transcript), [transcript]);

  const provenance = [
    transcript.language ? transcript.language.toUpperCase() : null,
    transcript.duration_sec != null ? formatDuration(transcript.duration_sec) : null,
    transcript.model,
    transcript.device,
  ].filter((part): part is string => Boolean(part));

  return (
    <div className={styles.viewer}>
      <div className={styles.toolbar}>
        <div className={styles.provenance}>
          {provenance.map((part) => (
            <span key={part} className="pill">
              {part}
            </span>
          ))}
        </div>
        <div className={styles.views} role="group" aria-label="Transcript view">
          <button
            type="button"
            className="btn btn-ghost"
            aria-pressed={view === "timeline"}
            onClick={() => setView("timeline")}
          >
            Timeline
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            aria-pressed={view === "text"}
            onClick={() => setView("text")}
          >
            Plain text
          </button>
        </div>
      </div>

      {view === "timeline" ? (
        segments.length === 0 ? (
          <p className={styles.empty}>This transcript has no segments.</p>
        ) : (
          <ol className={styles.segments} aria-label="Transcript segments">
            {segments.map((segment) => (
              <li key={segment.id} className={styles.segment}>
                <span className={`${styles.time} mono`}>{formatTimecode(segment.start)}</span>
                <span className={styles.text}>{segment.text.trim()}</span>
              </li>
            ))}
          </ol>
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
