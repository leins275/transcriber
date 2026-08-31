import { useEffect, useRef, useState } from "react";
import styles from "./VaultSearch.module.css";
import type { SearchResultView } from "../types";

export type VaultSearchProps = {
  /** The query, lifted to App like the project filter so it survives this
   * panel's unmount when a recording or Settings opens. */
  query: string;
  onQueryChange: (query: string) => void;
  /** Runs the actual search; App wires it to the service. */
  onSearch: (query: string) => Promise<SearchResultView[]>;
  /** Opens the hit's recording, exactly like a list row. */
  onOpen: (entryId: string) => void;
};

const DEBOUNCE_MS = 300;
const MIN_QUERY_CHARS = 2;

const KIND_LABELS: Record<SearchResultView["kind"], string> = {
  transcript: "Transcript",
  summary: "Summary",
  note: "Note",
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * Content search over the whole vault -- transcripts, summaries and notes,
 * ranked by the service's hybrid index. Different job than the project
 * *filter* (which narrows the list): this finds what was said.
 *
 * Presentational apart from `onSearch`: no invoke, no listen, no fetch.
 */
export function VaultSearch({ query, onQueryChange, onSearch, onOpen }: VaultSearchProps) {
  const [results, setResults] = useState<SearchResultView[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Guards a slow response from overwriting a newer query's results.
  const latest = useRef(query);

  useEffect(() => {
    latest.current = query;
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_CHARS) {
      setResults([]);
      setSearching(false);
      setError(null);
      return;
    }
    setSearching(true);
    setError(null);
    const timer = window.setTimeout(() => {
      onSearch(trimmed)
        .then((found) => {
          if (latest.current !== query) return; // stale
          setResults(found ?? []);
          setSearching(false);
        })
        .catch((caught: unknown) => {
          if (latest.current !== query) return;
          setError(messageOf(caught));
          setResults([]);
          setSearching(false);
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, onSearch]);

  const active = query.trim().length >= MIN_QUERY_CHARS;

  return (
    <div className={styles.search}>
      <input
        type="search"
        className={styles.input}
        aria-label="Search recordings"
        placeholder="Search transcripts, summaries and notes…"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
      />
      {active && (
        <div className={styles.results} aria-label="Search results">
          {searching ? (
            <p role="status" className={styles.status}>
              Searching…
            </p>
          ) : error ? (
            <p role="alert" className="alert">
              {error}
            </p>
          ) : results.length === 0 ? (
            <p className={styles.status}>No matches.</p>
          ) : (
            <ol className={styles.list}>
              {results.map((result, index) => (
                <li key={`${result.entry_id}-${result.kind}-${index}`}>
                  <button
                    type="button"
                    className={styles.row}
                    onClick={() => onOpen(result.entry_id)}
                  >
                    <span className={styles.rowHead}>
                      <span className={styles.name}>{result.meeting_name}</span>
                      {result.project && <span className="pill">{result.project}</span>}
                      <span className={styles.kind}>{KIND_LABELS[result.kind]}</span>
                      {result.timestamp && (
                        <span className={`${styles.time} mono`}>{result.timestamp}</span>
                      )}
                    </span>
                    <span className={styles.snippet}>{result.snippet}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
