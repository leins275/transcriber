import { useEffect, useState } from "react";
import styles from "./SummaryPanel.module.css";
import { Markdown } from "./Markdown";
import type { SummaryView } from "../types";

export type SummaryPanelProps = {
  entryId: string;
  onLoad: (entryId: string) => Promise<SummaryView>;
  /** Runs the summarize job — the empty state's Generate button (the
   * factored layout keeps generate verbs in the content area, never the
   * header). */
  onGenerate: () => void;
  /** True while a summarize job for this entry is queued or running; the
   * Generate button renders busy instead of firing twice. */
  busy?: boolean;
  /** Reports what this panel currently shows (`null` when nothing), so the
   * page's Copy button can act on the visible tab. */
  onContentChange?: (markdown: string | null) => void;
  /** Bump to re-read `summary.md` — App increments it when a summarize job
   * for this entry finishes, so a freshly generated summary appears without
   * reopening the page. */
  reloadToken?: number;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * A meeting's `summary.md` — written by the Summarize job (the LLM
 * feature), or by hand; `summary.md` has been a reserved vault name since
 * F1's first spec, so both read identically here.
 *
 * The empty state carries its own Generate button and names the exact path
 * a summary would live at, so an empty tab is actionable rather than dead.
 */
export function SummaryPanel({
  entryId,
  onLoad,
  onGenerate,
  busy = false,
  onContentChange,
  reloadToken = 0,
}: SummaryPanelProps) {
  const [summary, setSummary] = useState<SummaryView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    onLoad(entryId)
      .then((loaded) => {
        if (cancelled) return;
        setSummary(loaded);
        onContentChange?.(loaded.markdown ?? null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(messageOf(caught));
        onContentChange?.(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entryId, onLoad, onContentChange, reloadToken]);

  if (loading) {
    return (
      <p role="status" className={styles.status}>
        Looking for a summary…
      </p>
    );
  }

  if (error) {
    return (
      <p role="alert" className="alert">
        {error}
      </p>
    );
  }

  if (summary?.markdown) {
    return <Markdown markdown={summary.markdown} />;
  }

  return (
    <div className={styles.empty}>
      <p className={styles.emptyLead}>No summary for this meeting yet.</p>
      <button type="button" className="btn" disabled={busy} onClick={onGenerate}>
        {busy ? "Summarizing…" : "Generate summary"}
      </button>
      <p className={styles.emptyDetail}>
        Generated with the local language model. Anything written to{" "}
        <span className="mono">{summary?.path ?? "summary.md"}</span> shows up here.
      </p>
    </div>
  );
}
