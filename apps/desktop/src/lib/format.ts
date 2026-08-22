/**
 * Shared display formatters for durations, transcript timecodes and the
 * ISO-8601 timestamps F2 writes into its ledger and transcripts.
 *
 * Every function here answers "what do we render when the value is missing
 * or nonsense" the same way: an em dash, never `NaN`, `Invalid Date`, or a
 * silently plausible wrong value. These come off a service that fills its
 * rows in over a job's lifetime, so absent fields are the normal case.
 */

const EMPTY = "—";

/**
 * A transcript segment's start offset as `m:ss` (or `h:mm:ss` past an hour)
 * — the form a reader scrubbing a recording expects, not a duration phrase.
 */
export function formatTimecode(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const paddedSecs = String(secs).padStart(2, "0");
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${paddedSecs}`;
  }
  return `${minutes}:${paddedSecs}`;
}

/**
 * A length of time as a phrase (`1h 0m`, `4m 12s`, `9s`) — for "how long was
 * this meeting" and "how long did transcribing take", where a timecode would
 * read as a position rather than a span.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return EMPTY;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

/**
 * An ISO-8601 timestamp rendered in the operator's own locale and timezone.
 * F2 writes UTC; a ledger read at a glance should show local wall-clock time,
 * because that is what the operator will compare against their memory of the
 * meeting.
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return EMPTY;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * A realtime factor as `0.28× realtime`, the ratio F2 records of elapsed
 * transcription time to audio duration (below 1 means faster than realtime).
 */
export function formatRealtimeFactor(factor: number | null | undefined): string {
  if (factor == null || !Number.isFinite(factor) || factor < 0) return EMPTY;
  return `${factor.toFixed(2)}× realtime`;
}

/** A count with its noun, pluralized (`1 segment`, `1144 segments`). */
export function formatCount(count: number | null | undefined, noun: string): string {
  if (count == null || !Number.isFinite(count)) return EMPTY;
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
