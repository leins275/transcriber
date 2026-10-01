import styles from "./VaultRow.module.css";
import { formatMeetingDate, parseEntryName } from "../lib/meetingName";
import type { VaultMeetingView } from "../types";

export type VaultRowProps = {
  entry: VaultMeetingView;
  /** Opens the recording's own page. Called with the entry's server-issued
   * id (FR: never a raw path from the UI). */
  onOpen: (entryId: string) => void;
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

function transcriptState(entry: VaultMeetingView): string {
  if (entry.has_transcript) return "Transcript ready";
  if (entry.has_source) return "No transcript yet";
  return "No recording";
}

/**
 * One filed recording in the library table: a `<tr>` whose cells follow the
 * naming convention's own order — project, date, name, type — and end with
 * the transcript state.
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
export function VaultRow({ entry, onOpen }: VaultRowProps) {
  const parsed = parseEntryName(entry.meeting_name, entry.project);

  return (
    // The whole row opens the recording for a pointer; the name inside it is
    // the real button, which is what the keyboard and a screen reader reach.
    <tr className={styles.row} onClick={() => onOpen(entry.id)}>
      <td>
        {entry.project ? (
          <span className={`${styles.project} mono`}>{entry.project}</span>
        ) : (
          <span className={styles.muted}>Unsorted</span>
        )}
      </td>
      <td className={styles.date}>{parsed ? formatMeetingDate(parsed.date) : null}</td>
      <td className={styles.nameCell}>
        <button
          type="button"
          className={styles.name}
          onClick={(event) => {
            event.stopPropagation();
            onOpen(entry.id);
          }}
        >
          {parsed ? parsed.title : entry.meeting_name}
        </button>
      </td>
      <td>{parsed?.kind && <span className="pill">{parsed.kind}</span>}</td>
      <td>
        <span className={styles.state}>
          <span className={styles.icon} aria-hidden="true">
            <TranscriptIcon present={entry.has_transcript} />
          </span>
          {transcriptState(entry)}
        </span>
      </td>
    </tr>
  );
}
