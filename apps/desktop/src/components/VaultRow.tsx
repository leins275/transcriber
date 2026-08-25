import styles from "./VaultRow.module.css";
import { formatMeetingDate, parseMeetingName } from "../lib/meetingName";
import type { VaultMeetingView } from "../types";

export type VaultRowProps = {
  entry: VaultMeetingView;
  /** Opens the recording's own page. Called with the entry's server-issued
   * id (FR: never a raw path from the UI). */
  onOpen: (entryId: string) => void;
  /** Whether the row names its project in the meta line. On in the flat
   * library list, where the tag is the only thing saying which project a
   * row belongs to; off under a project group header, which already says. */
  showProject?: boolean;
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

/** The recording's file extension, from the meeting folder's `source.*`.
 * Not known from the listing, so the row says what it can. */
function statusLine(entry: VaultMeetingView): string {
  const parsed = parseMeetingName(entry.meeting_name);
  const parts = [parsed ? formatMeetingDate(parsed.date) : null];
  if (entry.has_transcript) {
    parts.push("transcript ready");
  } else if (entry.has_source) {
    parts.push("filed, no transcript yet");
  } else {
    parts.push("no recording");
  }
  return parts.filter(Boolean).join(" · ");
}

/**
 * One filed recording in the library.
 *
 * Deliberately thin: the row's job is to be scanned and chosen between, so
 * it carries a name, a state and one action — opening the recording.
 * Everything else about a recording — its transcript, its speakers, Reveal,
 * renaming, deleting — lives on the recording's own page, because those are
 * things you do to *one* recording after you have picked it, and doing them
 * inside a list means reading an hour of transcript through a keyhole.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function VaultRow({ entry, onOpen, showProject = true }: VaultRowProps) {
  const parsed = parseMeetingName(entry.meeting_name);

  return (
    <div className={styles.row}>
      <span className={styles.icon} aria-hidden="true">
        <TranscriptIcon present={entry.has_transcript} />
      </span>
      <button type="button" className={styles.content} onClick={() => onOpen(entry.id)}>
        <span className={styles.name}>{parsed ? parsed.title : entry.meeting_name}</span>
        <span className={styles.meta}>
          <span>{statusLine(entry)}</span>
          {showProject && entry.project && (
            <span className={`${styles.project} mono`}>{entry.project}</span>
          )}
        </span>
      </button>
    </div>
  );
}
