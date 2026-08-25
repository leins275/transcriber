import { useCallback, useEffect, useState } from "react";
import styles from "./ActionItemsPanel.module.css";
import { Markdown } from "./Markdown";
import { formatTimecode } from "../lib/format";
import type { ActionItemView, ActionItemsView, ItemScreenshotView } from "../types";

export type ActionItemsPanelProps = {
  entryId: string;
  onLoad: (entryId: string) => Promise<ActionItemsView>;
  /** On-demand frame capture at the item's cited timestamps; resolves with
   * the item's full screenshot list afterwards. */
  onCapture: (entryId: string, itemDirName: string) => Promise<{ screenshots: string[] }>;
  /** One item's screenshots as data URLs — loaded lazily per item, never
   * eagerly for the whole listing. */
  onLoadScreenshots: (entryId: string, itemDirName: string) => Promise<ItemScreenshotView[]>;
  /** Runs the action-items extraction — the empty state's Generate button
   * (the factored layout keeps generate verbs in the content area, never
   * the header). */
  onGenerate: () => void;
  /** True while an action-items job for this entry is queued or running;
   * the Generate button renders busy instead of firing twice. */
  generateBusy?: boolean;
  /** Reports what this panel currently shows as copyable markdown (`null`
   * when nothing), so the page's Copy button can act on the visible tab. */
  onContentChange?: (markdown: string | null) => void;
  /** Bump to re-read the items — App increments it when an action-items
   * job for this entry finishes, so fresh items appear without reopening
   * the page. */
  reloadToken?: number;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

type ItemScreenshotsState = {
  /** Loaded data URLs, or null while never loaded. */
  shots: ItemScreenshotView[] | null;
  open: boolean;
  busy: boolean;
};

/** The items as one markdown document — what the page's Copy button puts on
 * the clipboard when this tab is visible; `null` when there are no items. */
function itemsAsMarkdown(items: ActionItemView[]): string | null {
  if (items.length === 0) return null;
  return items
    .map((item) => {
      const heading = `# ${item.title}${item.item_type ? ` (${item.item_type})` : ""}`;
      return item.body_md ? `${heading}\n\n${item.body_md.trim()}` : heading;
    })
    .join("\n\n");
}

/**
 * A meeting's extracted action items — the `action items/` folder beside
 * the summary, rendered in place. Presentational apart from its callbacks,
 * like `SummaryPanel`.
 *
 * Screenshots are the deliberate exception to eager loading: extraction
 * only auto-captures moments the model marked as visually load-bearing, so
 * most items have none, and the ones that do load their images only when
 * the operator opens them. The Capture button fills the gap the model left:
 * it grabs frames at the item's cited timestamps on demand.
 */
export function ActionItemsPanel({
  entryId,
  onLoad,
  onCapture,
  onLoadScreenshots,
  onGenerate,
  generateBusy = false,
  onContentChange,
  reloadToken = 0,
}: ActionItemsPanelProps) {
  const [view, setView] = useState<ActionItemsView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shotsByItem, setShotsByItem] = useState<Record<string, ItemScreenshotsState>>({});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    onLoad(entryId)
      .then((loaded) => {
        if (cancelled) return;
        setView(loaded);
        setShotsByItem({});
        onContentChange?.(itemsAsMarkdown(loaded.items));
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

  const patchItemState = useCallback((dirName: string, patch: Partial<ItemScreenshotsState>) => {
    setShotsByItem((prev) => {
      const base = prev[dirName] ?? { shots: null, open: false, busy: false };
      return { ...prev, [dirName]: { ...base, ...patch } };
    });
  }, []);

  const toggleScreenshots = useCallback(
    async (item: ActionItemView) => {
      const current = shotsByItem[item.dir_name];
      if (current?.open) {
        patchItemState(item.dir_name, { open: false });
        return;
      }
      if (current?.shots) {
        patchItemState(item.dir_name, { open: true });
        return;
      }
      patchItemState(item.dir_name, { busy: true });
      try {
        const shots = await onLoadScreenshots(entryId, item.dir_name);
        patchItemState(item.dir_name, { shots, open: true, busy: false });
      } catch (caught) {
        setError(messageOf(caught));
        patchItemState(item.dir_name, { busy: false });
      }
    },
    [entryId, onLoadScreenshots, patchItemState, shotsByItem],
  );

  const capture = useCallback(
    async (item: ActionItemView) => {
      patchItemState(item.dir_name, { busy: true });
      setError(null);
      try {
        const result = await onCapture(entryId, item.dir_name);
        // Refresh the names on the item and show the frames right away.
        setView((prev) =>
          prev
            ? {
                ...prev,
                items: prev.items.map((it) =>
                  it.dir_name === item.dir_name
                    ? { ...it, screenshot_names: result.screenshots }
                    : it,
                ),
              }
            : prev,
        );
        const shots = await onLoadScreenshots(entryId, item.dir_name);
        patchItemState(item.dir_name, { shots, open: true, busy: false });
      } catch (caught) {
        setError(messageOf(caught));
        patchItemState(item.dir_name, { busy: false });
      }
    },
    [entryId, onCapture, onLoadScreenshots, patchItemState],
  );

  if (loading && view === null) {
    return (
      <p role="status" className={styles.status}>
        Looking for action items…
      </p>
    );
  }

  if (error && view === null) {
    return (
      <p role="alert" className="alert">
        {error}
      </p>
    );
  }

  if (!view || view.items.length === 0) {
    return (
      <div className={styles.empty}>
        <p className={styles.emptyLead}>No action items for this meeting yet.</p>
        <button type="button" className="btn" disabled={generateBusy} onClick={onGenerate}>
          {generateBusy ? "Extracting…" : "Extract action items"}
        </button>
        <p className={styles.emptyDetail}>
          Extracted with the local language model. Anything written to{" "}
          <span className="mono">{view?.dir ?? "action items"}</span> shows up here.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <ul className={styles.list}>
        {view.items.map((item) => {
          const state = shotsByItem[item.dir_name];
          const hasShots = item.screenshot_names.length > 0;
          const canCapture = item.timestamps.length > 0;
          return (
            <li key={item.dir_name} className={styles.item}>
              <div className={styles.itemHead}>
                <h3 className={styles.itemTitle}>{item.title}</h3>
                {item.item_type && <span className="pill">{item.item_type}</span>}
                {item.archived && <span className="pill">archived</span>}
              </div>
              {item.timestamps.length > 0 && (
                <p className={styles.timestamps} aria-label="Discussed at">
                  {item.timestamps.map((stamp) => formatTimecode(stamp)).join(" · ")}
                </p>
              )}
              {item.body_md && <Markdown markdown={item.body_md} />}
              <div className={styles.itemActions}>
                {hasShots && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={state?.busy}
                    onClick={() => void toggleScreenshots(item)}
                  >
                    {state?.open
                      ? "Hide screenshots"
                      : `Screenshots (${item.screenshot_names.length})`}
                  </button>
                )}
                {canCapture && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={state?.busy}
                    onClick={() => void capture(item)}
                  >
                    {state?.busy ? "Capturing…" : "Capture screenshots"}
                  </button>
                )}
              </div>
              {state?.open && state.shots && state.shots.length > 0 && (
                <div className={styles.shots}>
                  {state.shots.map((shot) => (
                    <img
                      key={shot.name}
                      className={styles.shot}
                      src={shot.data_url}
                      alt={`Screenshot ${shot.name}`}
                    />
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
