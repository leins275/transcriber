import { useCallback, useEffect, useState } from "react";
import styles from "./VaultRow.module.css";
import { MeetingEditor } from "./MeetingEditor";
import { TranscriptViewer } from "./TranscriptViewer";
import { formatMeetingDate, parseMeetingName } from "../lib/meetingName";
import type { MeetingUpdate, TranscriptView, VaultMeetingView } from "../types";

/** Which of the row's expandable panels is open. Exactly one at a time: the
 * row is a single object being acted on, and stacking a delete confirmation
 * under an open transcript would make it far too easy to confirm the wrong
 * one. */
type OpenPanel = "none" | "transcript" | "edit" | "delete";

export type VaultRowProps = {
  entry: VaultMeetingView;
  /** Project codes already in the vault, for the editor's picker. */
  projects: string[];
  /** Calls the Rust side by the entry's server-issued id (FR: never a raw
   * path from the UI) -- mirrors `JobRow`'s own `onReveal(job.id)` contract.
   * Every callback below follows that same rule. */
  onReveal: (entryId: string) => void;
  onReadTranscript: (entryId: string) => Promise<TranscriptView>;
  onUpdate: (entryId: string, update: MeetingUpdate) => Promise<void>;
  onDelete: (entryId: string) => Promise<void>;
};

/** A filled check for a meeting that already has a transcript, a hollow
 * ring (matching `JobRow`'s own pending indicator) when it does not. */
function TranscriptIcon({ present }: { present: boolean }) {
  if (present) {
    return (
      <svg
        width="15"
        height="15"
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
  }
  return <span className={styles.ring} />;
}

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * One meeting in the vault browser, and the app's UI representation of a
 * recording folder: its name, its status, and the actions that folder
 * supports — Reveal, Transcript, Summary (not built yet), Rename, Delete.
 *
 * Actions that open something do it *inline*, expanding the row rather than
 * covering the list with a modal: the operator is usually comparing rows
 * (which of these is the one I meant?), and a dialog hides exactly the
 * context that answers the question.
 *
 * Presentational only: no invoke, no listen, no fetch — every action is a
 * callback prop, and the transcript arrives through `onReadTranscript`
 * rather than being fetched here.
 */
export function VaultRow({
  entry,
  projects,
  onReveal,
  onReadTranscript,
  onUpdate,
  onDelete,
}: VaultRowProps) {
  const [open, setOpen] = useState<OpenPanel>("none");
  const [transcript, setTranscript] = useState<TranscriptView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  // A rename can change the meeting's transcript-bearing folder out from
  // under a transcript already loaded here; drop it so reopening re-reads
  // rather than showing the previous name's content under the new one.
  useEffect(() => {
    setTranscript(null);
  }, [entry.meeting_dir]);

  const toggle = useCallback((panel: OpenPanel) => {
    setError(null);
    setOpen((current) => (current === panel ? "none" : panel));
  }, []);

  const openTranscript = useCallback(async () => {
    if (open === "transcript") {
      setOpen("none");
      return;
    }
    setError(null);
    setOpen("transcript");
    if (transcript) return;
    setLoading(true);
    try {
      setTranscript(await onReadTranscript(entry.id));
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setLoading(false);
    }
  }, [entry.id, onReadTranscript, open, transcript]);

  const confirmDelete = useCallback(async () => {
    setDeleting(true);
    setError(null);
    try {
      await onDelete(entry.id);
      // No `setOpen`/`setDeleting` on success: the row is about to unmount
      // with the entry it rendered.
    } catch (caught) {
      setError(messageOf(caught));
      setDeleting(false);
    }
  }, [entry.id, onDelete]);

  const parsed = parseMeetingName(entry.meeting_name);
  const meta = [
    parsed ? formatMeetingDate(parsed.date) : null,
    entry.has_transcript ? "Transcript ready" : "No transcript yet",
    entry.has_source ? null : "Recording missing",
  ].filter((part): part is string => Boolean(part));

  return (
    <div className={styles.row} data-open={open}>
      <span className={styles.icon} aria-hidden="true">
        <TranscriptIcon present={entry.has_transcript} />
      </span>
      <div className={styles.content}>
        <span className={`${styles.name} mono`}>{entry.meeting_name}</span>
        <span className={styles.meta}>
          {meta.join(" · ")}
          <span className="pill">{entry.project ?? "unsorted"}</span>
        </span>
        <span className={`${styles.path} mono`}>{entry.meeting_dir}</span>
      </div>
      <div className={styles.actions}>
        <button type="button" className="btn btn-secondary" onClick={() => onReveal(entry.id)}>
          Reveal
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          aria-expanded={open === "transcript"}
          disabled={!entry.has_transcript}
          title={entry.has_transcript ? undefined : "No transcript for this recording yet"}
          onClick={openTranscript}
        >
          Transcript
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled
          title="Summaries are not built yet"
        >
          Summary
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          aria-expanded={open === "edit"}
          onClick={() => toggle("edit")}
        >
          Rename
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          aria-expanded={open === "delete"}
          onClick={() => toggle("delete")}
        >
          Delete
        </button>
      </div>

      {open !== "none" && (
        <div className={styles.panel}>
          {error && (
            <p role="alert" className="alert">
              {error}
            </p>
          )}

          {open === "transcript" &&
            (loading ? (
              <p role="status" className={styles.status}>
                Reading transcript…
              </p>
            ) : (
              transcript && <TranscriptViewer transcript={transcript} />
            ))}

          {open === "edit" && (
            <MeetingEditor
              entry={entry}
              projects={projects}
              onSave={async (update) => {
                await onUpdate(entry.id, update);
                setOpen("none");
              }}
              onCancel={() => setOpen("none")}
            />
          )}

          {open === "delete" && (
            <div className={styles.confirm}>
              <p className={styles.confirmText}>
                Move <span className="mono">{entry.meeting_name}</span> — recording, transcript and
                all — to the Recycle Bin? You can restore it from there.
              </p>
              <div className={styles.confirmActions}>
                <button type="button" className="btn" onClick={confirmDelete} disabled={deleting}>
                  {deleting ? "Deleting…" : "Move to Recycle Bin"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => setOpen("none")}
                  disabled={deleting}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
