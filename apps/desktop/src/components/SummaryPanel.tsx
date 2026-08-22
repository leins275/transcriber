import { useEffect, useState } from "react";
import styles from "./SummaryPanel.module.css";
import type { SummaryView } from "../types";

export type SummaryPanelProps = {
  entryId: string;
  onLoad: (entryId: string) => Promise<SummaryView>;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * A meeting's `summary.md`, if anything has written one.
 *
 * Nothing in this app generates summaries — that needs a language model, and
 * it is a feature in its own right rather than something to fake here. But
 * `summary.md` has been a reserved name in the vault contract since F1's
 * first spec, so a summary written by hand (or by whatever eventually
 * generates them) is readable the day it appears.
 *
 * The empty state therefore names the exact path a summary would live at,
 * rather than saying "coming soon" — that turns a disabled tab into
 * something the operator can actually act on.
 *
 * Rendered as preformatted text, not parsed Markdown: pulling in a Markdown
 * renderer to display a file this app does not yet produce would be
 * machinery ahead of a use case.
 */
export function SummaryPanel({ entryId, onLoad }: SummaryPanelProps) {
  const [summary, setSummary] = useState<SummaryView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    onLoad(entryId)
      .then((loaded) => {
        if (!cancelled) setSummary(loaded);
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
  }, [entryId, onLoad]);

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
    return <pre className={styles.markdown}>{summary.markdown}</pre>;
  }

  return (
    <div className={styles.empty}>
      <p className={styles.emptyLead}>No summary for this meeting yet.</p>
      <p className={styles.emptyDetail}>
        Nothing generates summaries yet — that needs a language model. Anything written to{" "}
        <span className="mono">{summary?.path ?? "summary.md"}</span> shows up here.
      </p>
    </div>
  );
}
