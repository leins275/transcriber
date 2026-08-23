import { useCallback, useEffect, useMemo, useState } from "react";
import styles from "./ProjectPage.module.css";
import { Markdown } from "./Markdown";
import type { ArtifactContentView, ArtifactKind, ArtifactView, ReportView } from "../types";

export type ProjectPageProps = {
  project: string;
  onBack: () => void;
  onListArtifacts: (project: string, kind: ArtifactKind) => Promise<ArtifactView[]>;
  onReadArtifact: (
    project: string,
    kind: ArtifactKind,
    slug: string,
  ) => Promise<ArtifactContentView>;
  onRevealArtifact: (project: string, kind: ArtifactKind, slug: string) => Promise<void>;
  onListReports: (project: string) => Promise<ReportView[]>;
  onReadReport: (project: string, name: string) => Promise<string>;
  onRevealReport: (project: string, name: string) => Promise<void>;
  /** Queue the project-essence report job. */
  onExportEssence: (project: string) => Promise<void>;
  /** A report job for this project is still running. */
  essenceBusy: boolean;
  /** Bumped when a derived job touching this project finishes, so lists
   * re-fetch without leaving the page. */
  reloadToken: number;
};

type Tab = ArtifactKind | "reports";

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/** `meta.title` when the artifact recorded one, else its slug. */
function titleOf(item: ArtifactContentView | null, slug: string): string {
  const title = item?.meta["title"];
  return typeof title === "string" && title.trim() ? title : slug;
}

/**
 * One project, full-window: its extracted action items, facts and dated
 * status reports. The same page pattern as `RecordingPage` (no router —
 * `App.tsx` owns which page is open).
 */
export function ProjectPage({
  project,
  onBack,
  onListArtifacts,
  onReadArtifact,
  onRevealArtifact,
  onListReports,
  onReadReport,
  onRevealReport,
  onExportEssence,
  essenceBusy,
  reloadToken,
}: ProjectPageProps) {
  const [tab, setTab] = useState<Tab>("action_items");
  const [artifacts, setArtifacts] = useState<ArtifactView[]>([]);
  const [reports, setReports] = useState<ReportView[]>([]);
  const [openSlug, setOpenSlug] = useState<string | null>(null);
  const [content, setContent] = useState<ArtifactContentView | null>(null);
  const [reportMarkdown, setReportMarkdown] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the open tab's listing; re-runs when a derived job finishes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load =
      tab === "reports"
        ? onListReports(project).then((listed) => {
            if (!cancelled) setReports(listed);
          })
        : onListArtifacts(project, tab).then((listed) => {
            if (!cancelled) setArtifacts(listed);
          });
    load
      .catch((caught: unknown) => {
        if (!cancelled) setError(messageOf(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [project, tab, reloadToken, onListArtifacts, onListReports]);

  // Reset the detail view when the tab (or project) changes.
  useEffect(() => {
    setOpenSlug(null);
    setContent(null);
    setReportMarkdown(null);
  }, [tab, project]);

  const openItem = useCallback(
    (slug: string) => {
      if (tab === "reports") return;
      setOpenSlug(slug);
      setContent(null);
      setError(null);
      onReadArtifact(project, tab, slug)
        .then(setContent)
        .catch((caught: unknown) => setError(messageOf(caught)));
    },
    [project, tab, onReadArtifact],
  );

  const openReport = useCallback(
    (name: string) => {
      setOpenSlug(name);
      setReportMarkdown(null);
      setError(null);
      onReadReport(project, name)
        .then(setReportMarkdown)
        .catch((caught: unknown) => setError(messageOf(caught)));
    },
    [project, onReadReport],
  );

  const exportEssence = useCallback(() => {
    setError(null);
    onExportEssence(project).catch((caught: unknown) => setError(messageOf(caught)));
  }, [project, onExportEssence]);

  const images = useMemo(() => {
    if (!content) return {};
    return Object.fromEntries(content.images.map((image) => [image.name, image.data_url]));
  }, [content]);

  const tabButton = (id: Tab, label: string, count: number | null) => (
    <button
      type="button"
      role="tab"
      id={`project-tab-${id}`}
      aria-selected={tab === id}
      aria-controls={`project-panel-${id}`}
      className={styles.tab}
      onClick={() => setTab(id)}
    >
      {label}
      {count !== null && <span className={styles.tabCount}>{count}</span>}
    </button>
  );

  return (
    <section className={styles.page} aria-label="Project">
      <div className={styles.head}>
        <div className={styles.breadcrumb}>
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            ← Recordings
          </button>
          <span className={styles.crumbSeparator}>/</span>
          <span className="pill">{project}</span>
        </div>

        <div className={styles.titleRow}>
          <h2 className={`${styles.title} mono`}>{project}</h2>
          <div className={styles.actions}>
            <button type="button" className="btn" disabled={essenceBusy} onClick={exportEssence}>
              {essenceBusy ? "Generating report…" : "Export project essence"}
            </button>
          </div>
        </div>

        <div className={styles.tabs} role="tablist" aria-label="Project views">
          {tabButton(
            "action_items",
            "Action items",
            tab === "action_items" ? artifacts.length : null,
          )}
          {tabButton("facts", "Facts", tab === "facts" ? artifacts.length : null)}
          {tabButton("reports", "Reports", tab === "reports" ? reports.length : null)}
        </div>
      </div>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className={styles.body} role="tabpanel" id={`project-panel-${tab}`}>
        {loading ? (
          <p role="status" className={styles.status}>
            Loading…
          </p>
        ) : tab === "reports" ? (
          reports.length === 0 ? (
            <p className={styles.empty}>
              No reports yet. <strong>Export project essence</strong> reads every transcript,
              summary, action item and fact in this project and writes a dated status report (with a
              PDF) under <span className="mono">reports/</span>.
            </p>
          ) : (
            <div className={styles.list}>
              {reports.map((report) => (
                <div key={report.name} className={styles.rowBlock}>
                  <div className={styles.row}>
                    <button
                      type="button"
                      className={styles.rowMain}
                      onClick={() =>
                        openSlug === report.name ? setOpenSlug(null) : openReport(report.name)
                      }
                    >
                      <span className="mono">{report.name}</span>
                      {report.has_pdf && <span className="pill">PDF</span>}
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => void onRevealReport(project, report.name)}
                    >
                      Reveal
                    </button>
                  </div>
                  {openSlug === report.name && (
                    <div className={styles.detail}>
                      {reportMarkdown === null ? (
                        <p role="status" className={styles.status}>
                          Reading report…
                        </p>
                      ) : (
                        <Markdown markdown={reportMarkdown} />
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )
        ) : artifacts.length === 0 ? (
          <p className={styles.empty}>
            {tab === "action_items"
              ? "No action items yet. Open a recording and use “Action items” to extract them from its transcript."
              : "No facts yet. Open a recording and use “Facts” to extract notable facts and answered questions."}
          </p>
        ) : (
          <div className={styles.list}>
            {artifacts.map((artifact) => (
              <div key={artifact.slug} className={styles.rowBlock}>
                <div className={styles.row}>
                  <button
                    type="button"
                    className={styles.rowMain}
                    onClick={() =>
                      openSlug === artifact.slug ? setOpenSlug(null) : openItem(artifact.slug)
                    }
                  >
                    <span>
                      {openSlug === artifact.slug ? titleOf(content, artifact.slug) : artifact.slug}
                    </span>
                    {artifact.screenshot_count > 0 && (
                      <span className={styles.screenshotCount}>
                        {artifact.screenshot_count} screenshot
                        {artifact.screenshot_count === 1 ? "" : "s"}
                      </span>
                    )}
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => void onRevealArtifact(project, tab, artifact.slug)}
                  >
                    Reveal
                  </button>
                </div>
                {openSlug === artifact.slug && (
                  <div className={styles.detail}>
                    {content === null ? (
                      <p role="status" className={styles.status}>
                        Reading…
                      </p>
                    ) : (
                      <>
                        {typeof content.meta["source_meeting"] === "string" && (
                          <p className={styles.provenance}>
                            From{" "}
                            <span className="mono">{String(content.meta["source_meeting"])}</span>
                            {typeof content.meta["type"] === "string" && (
                              <span className="pill">{String(content.meta["type"])}</span>
                            )}
                            {typeof content.meta["kind"] === "string" && (
                              <span className="pill">{String(content.meta["kind"])}</span>
                            )}
                          </p>
                        )}
                        <Markdown markdown={content.markdown} images={images} />
                      </>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
