import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import styles from "./TranscriptViewer.module.css";
import { SelectionSpeakerMenu } from "./SelectionSpeakerMenu";
import { SpeakerTag } from "./SpeakerTag";
import { formatTimecode } from "../lib/format";
import { segmentIdsFromRange } from "../lib/selection";
import {
  assignSpeaker,
  assignSpeakerToSegments,
  filterTurns,
  groupIntoTurns,
  renameSpeaker,
  speakerNames,
} from "../lib/turns";
import type { TranscriptView } from "../types";

/**
 * What the operator has dragged over, resolved the moment the pointer comes
 * up rather than when the menu is used: pressing anything in the popover
 * collapses the browser selection, so the ids have to be taken while the
 * highlight still exists.
 */
type PendingSelection = {
  segmentIds: string[];
  anchor: { x: number; y: number };
};

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
  const [pending, setPending] = useState<PendingSelection | null>(null);
  const listRef = useRef<HTMLOListElement>(null);
  // Whether the drag in progress began on the transcript. Only that answers
  // whether a release somewhere else is an overshot selection or someone
  // else's click.
  const draggingFromList = useRef(false);

  const turns = useMemo(
    () => groupIntoTurns(transcript.segments, speakers),
    [transcript.segments, speakers],
  );
  const visible = useMemo(() => filterTurns(turns, query), [turns, query]);
  const known = useMemo(() => speakerNames(turns), [turns]);
  // One lookup built per transcript rather than a scan per turn: an hour of
  // speech is thousands of segments, and the paragraphs re-render on every
  // keystroke in the search box.
  const segmentById = useMemo(
    () => new Map(transcript.segments.map((segment) => [String(segment.id), segment])),
    [transcript.segments],
  );

  const persist = useCallback(
    (next: Record<string, string>) => {
      setSpeakers(next);
      setSaveError(null);
      if (!onSaveSpeakers) return;
      onSaveSpeakers(next).catch((error: unknown) => setSaveError(messageOf(error)));
    },
    [onSaveSpeakers],
  );

  // One handler on the whole list, not one per segment: an hour of speech is
  // thousands of spans, and a selection is a single event either way. Takes
  // only the release point, so the same reader serves the list's own
  // pointer-up and the document-level one below.
  const readSelection = useCallback((release: { clientX: number; clientY: number }) => {
    const root = listRef.current;
    const selection = window.getSelection();
    if (root === null || selection === null || selection.rangeCount === 0) {
      setPending(null);
      return;
    }
    const segmentIds = segmentIdsFromRange(selection.getRangeAt(0), root);
    // A caret, or a drag that ended on the toolbar: nothing to attribute, so
    // nothing is offered.
    if (segmentIds.length === 0) {
      setPending(null);
      return;
    }
    // The point the drag ended at, which is where the operator is already
    // looking — and no geometry read over a transcript of thousands of spans.
    setPending({ segmentIds, anchor: { x: release.clientX, y: release.clientY } });
  }, []);

  // Overshooting the list is how a paragraph gets selected to its end — the
  // pointer is released below the last turn, in the margin — and a release
  // there never reaches the `<ol>`. So a drag that began on the transcript is
  // also heard out at the document. Releases *inside* the list are left to
  // the list's own handler, and a release that began anywhere else (the
  // popover's own buttons, a click away) is not a selection gesture at all.
  useEffect(() => {
    function overTheList(event: PointerEvent): boolean {
      const target = event.target;
      return target instanceof Node && listRef.current !== null && listRef.current.contains(target);
    }

    function handlePress(event: PointerEvent) {
      draggingFromList.current = overTheList(event);
    }

    function handleRelease(event: PointerEvent) {
      if (!draggingFromList.current) return;
      draggingFromList.current = false;
      if (overTheList(event)) return;
      readSelection(event);
    }

    document.addEventListener("pointerdown", handlePress);
    document.addEventListener("pointerup", handleRelease);
    return () => {
      document.removeEventListener("pointerdown", handlePress);
      document.removeEventListener("pointerup", handleRelease);
    };
  }, [readSelection]);

  const assignSelection = useCallback(
    (name: string) => {
      if (pending === null) return;
      persist(assignSpeakerToSegments(speakers, pending.segmentIds, name));
      // The highlight has served its purpose; leaving it up would suggest the
      // next assignment still applies to it.
      window.getSelection()?.removeAllRanges();
      setPending(null);
    },
    [pending, persist, speakers],
  );

  const dismissSelection = useCallback(() => setPending(null), []);

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
            <ol
              ref={listRef}
              className={styles.turns}
              aria-label="Transcript"
              onPointerUp={readSelection}
            >
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
                  {/* One span per segment, joined by the same single space
                      `groupIntoTurns` uses for `turn.text`: the paragraph
                      reads identically, and a selection over it can be
                      resolved back to segment ids. */}
                  <p className={styles.text}>
                    {turn.segmentIds.map((segmentId, index) => {
                      const segment = segmentById.get(segmentId);
                      if (!segment) return null;
                      return (
                        <Fragment key={segmentId}>
                          {index > 0 && " "}
                          <span className={styles.segment} data-segment-id={segmentId}>
                            {segment.text.trim()}
                          </span>
                        </Fragment>
                      );
                    })}
                  </p>
                </li>
              ))}
            </ol>
            {/* Rendered outside the list so pressing it is not another
                pointer-up over the transcript. */}
            {pending !== null && (
              <SelectionSpeakerMenu
                known={known}
                anchor={pending.anchor}
                onAssign={assignSelection}
                onDismiss={dismissSelection}
              />
            )}
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
