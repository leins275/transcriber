import { useCallback, useEffect, useState } from "react";
import styles from "./LedgerPanel.module.css";
import { formatCount, formatDuration, formatRealtimeFactor, formatTimestamp } from "../lib/format";
import type { LedgerJobView } from "../types";

export type LedgerPanelProps = {
  /** Loads the newest rows of F2's sqlite job ledger. Rejects with an
   * `AppError`-shaped value; the message is shown inline. */
  onLoad: () => Promise<LedgerJobView[]>;
};

/** F2's five ledger states, in the vocabulary the operator sees. Kept
 * uncollapsed (unlike the job seam's four): a cancelled job did not fail,
 * and a log that says otherwise is the log lying. */
const STATUS_TEXT: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/** The last path component of a source path -- the ledger records absolute
 * paths, which are far too wide to scan a column of. The full path stays in
 * the row's `title` for the one time it is actually needed. */
function fileNameOf(path: string | null): string {
  if (!path) return "—";
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/**
 * The service log: F2's own sqlite job ledger, newest first.
 *
 * This is the durable record, not this session's activity — it survives
 * restarts of both processes, which is what makes it the place to look when
 * a transcription failed an hour ago and the app has been closed since. The
 * session's live pipeline is the Jobs panel; these two deliberately show
 * different things.
 *
 * Loads on mount and on demand. Presentational apart from that: the fetch
 * itself is the `onLoad` prop, so this component never touches IPC.
 */
export function LedgerPanel({ onLoad }: LedgerPanelProps) {
  const [rows, setRows] = useState<LedgerJobView[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(await onLoad());
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setLoading(false);
    }
  }, [onLoad]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    onLoad()
      .then((loaded) => {
        if (!cancelled) setRows(loaded);
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
  }, [onLoad]);

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <span className={styles.hint}>
          Every job this service has recorded, newest first — kept across restarts.
        </span>
        <button type="button" className="btn btn-secondary" onClick={load} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {!error && rows !== null && rows.length === 0 && (
        <p className={styles.empty}>No jobs recorded yet.</p>
      )}

      {rows !== null && rows.length > 0 && (
        <ul className={styles.rows}>
          {rows.map((row) => (
            <li key={row.job_id} className={styles.row} data-status={row.status}>
              <div className={styles.head}>
                <span className={`${styles.file} mono`} title={row.source_path ?? undefined}>
                  {fileNameOf(row.source_path)}
                </span>
                <span className={styles.status} data-status={row.status}>
                  {STATUS_TEXT[row.status] ?? row.status}
                </span>
              </div>
              <div className={styles.meta}>
                <span>{formatTimestamp(row.created_at)}</span>
                {row.model && <span className="pill">{row.model}</span>}
                {row.device && <span className="pill">{row.device}</span>}
                {row.language && <span className="pill">{row.language.toUpperCase()}</span>}
              </div>
              <div className={styles.meta}>
                <span>Audio {formatDuration(row.audio_duration_sec)}</span>
                <span>Took {formatDuration(row.elapsed_sec)}</span>
                <span>{formatRealtimeFactor(row.realtime_factor)}</span>
                {row.segment_count != null && (
                  <span>{formatCount(row.segment_count, "segment")}</span>
                )}
              </div>
              {row.error_message && (
                <p className={styles.error}>
                  {row.error_kind ? `${row.error_kind}: ` : ""}
                  {row.error_message}
                </p>
              )}
              <span className={`${styles.id} mono`}>{row.job_id}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
