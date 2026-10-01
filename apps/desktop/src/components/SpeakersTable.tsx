import { useCallback, useEffect, useMemo, useState } from "react";
import styles from "./SpeakersTable.module.css";
import tableStyles from "./VaultList.module.css";
import rowStyles from "./VaultRow.module.css";
import { formatDuration } from "../lib/format";
import {
  filterSpeakers,
  nextSpeakerSort,
  sortSpeakers,
  type SpeakerSort,
  type SpeakerSortColumn,
} from "../lib/speakerSort";
import type { SpeakerView } from "../types";

export type SpeakersTableProps = {
  /** Reads the speakers database. Called when the table mounts (the tab
   * opening) and again after a speaker is added. */
  onLoad: () => Promise<SpeakerView[]>;
  /** Creates a person under this name; resolves once they are stored. */
  onAdd: (name: string) => Promise<void>;
  /** Opens the speaker's own page, by their canonical name. */
  onOpen: (name: string) => void;
  /** The name filter and the column sort, owned by the caller so they
   * outlive this table while a speaker's page is open. */
  filter: string;
  onFilterChange: (filter: string) => void;
  sort: SpeakerSort;
  onSortChange: (sort: SpeakerSort) => void;
};

const COLUMNS: { column: SpeakerSortColumn; label: string }[] = [
  { column: "name", label: "Name" },
  { column: "projects", label: "Projects" },
  { column: "meetings", label: "Meetings" },
  { column: "speech", label: "Labelled speech" },
  { column: "voice", label: "Voice samples" },
];

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * The speakers database as a table — the Recordings table's sibling, built
 * from the same table and row styles: one row per person, whether they were
 * registered by hand or are only named in meeting labels.
 *
 * Deliberately thin, like a recordings row: a name to scan, the numbers
 * that say how much of this person the vault holds, and one action — open
 * their page. Renaming, aliases, the bio and removal live there.
 *
 * Owns its own load; otherwise presentational (no invoke, no listen).
 */
export function SpeakersTable({
  onLoad,
  onAdd,
  onOpen,
  filter,
  onFilterChange,
  sort,
  onSortChange,
}: SpeakersTableProps) {
  const [people, setPeople] = useState<SpeakerView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState(false);
  const [typed, setTyped] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    onLoad()
      .then((loaded) => {
        if (cancelled) return;
        setPeople(loaded ?? []);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(messageOf(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [onLoad, reload]);

  const shown = useMemo(
    () => sortSpeakers(filterSpeakers(people ?? [], filter), sort),
    [people, filter, sort],
  );

  const closeAdd = useCallback(() => {
    setAdding(false);
    setTyped("");
  }, []);

  async function handleAdd(event: React.FormEvent) {
    event.preventDefault();
    const name = typed.trim();
    if (name === "" || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onAdd(name);
      closeAdd();
      setReload((count) => count + 1);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.speakers}>
      <div className={styles.bar}>
        <input
          type="search"
          className={styles.input}
          aria-label="Filter speakers"
          placeholder="Filter by name"
          value={filter}
          onChange={(event) => onFilterChange(event.target.value)}
        />
        {!adding && (
          <button type="button" className="btn" onClick={() => setAdding(true)}>
            Add speaker
          </button>
        )}
      </div>

      {adding && (
        <form className={styles.addForm} onSubmit={handleAdd} aria-label="Add speaker">
          <label className={styles.field}>
            <span className={styles.label}>Speaker name</span>
            <input
              className={styles.input}
              value={typed}
              autoFocus
              disabled={saving}
              onChange={(event) => setTyped(event.target.value)}
            />
          </label>
          <button type="submit" className="btn" disabled={saving || typed.trim() === ""}>
            {saving ? "Adding…" : "Add"}
          </button>
          <button type="button" className="btn btn-secondary" disabled={saving} onClick={closeAdd}>
            Cancel
          </button>
        </form>
      )}

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {people === null ? (
        !error && (
          <p role="status" className={styles.status}>
            Reading the speakers database…
          </p>
        )
      ) : people.length === 0 ? (
        <p className={styles.status}>
          Nobody is known yet. Name the speakers in a transcript, or add a speaker here, and they
          will be listed with the meetings they take part in.
        </p>
      ) : shown.length === 0 ? (
        <p className={styles.status}>No speaker matches “{filter.trim()}”.</p>
      ) : (
        <div className={tableStyles.wrap}>
          <table className={tableStyles.table}>
            <thead>
              <tr>
                {COLUMNS.map(({ column, label }) => {
                  const active = sort.column === column;
                  const ascending = sort.direction === "asc";
                  return (
                    <th
                      key={column}
                      scope="col"
                      aria-sort={active ? (ascending ? "ascending" : "descending") : "none"}
                    >
                      <button
                        type="button"
                        className={tableStyles.sortButton}
                        data-active={active || undefined}
                        onClick={() => onSortChange(nextSpeakerSort(sort, column))}
                      >
                        {label}
                        <span className={tableStyles.sortMark} aria-hidden="true">
                          {active ? (ascending ? "▲" : "▼") : "⇅"}
                        </span>
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {shown.map((person) => (
                // The whole row opens the speaker for a pointer; the name is
                // the real button, as in the recordings table.
                <tr key={person.name} className={rowStyles.row} onClick={() => onOpen(person.name)}>
                  <td className={rowStyles.nameCell}>
                    <button
                      type="button"
                      className={rowStyles.name}
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpen(person.name);
                      }}
                    >
                      {person.name}
                    </button>
                    {person.aliases.length > 0 && (
                      <span className={styles.aliases}>also {person.aliases.join(", ")}</span>
                    )}
                  </td>
                  <td>
                    <span className={`${rowStyles.project} mono`}>
                      {person.projects.join(", ")}
                    </span>
                  </td>
                  <td className={styles.number}>{person.meetings}</td>
                  <td className={styles.number}>{formatDuration(person.speech_sec)}</td>
                  <td className={styles.number}>
                    {person.voice_samples}
                    {person.voice_set_aside > 0 && (
                      <span className={styles.aside}> · {person.voice_set_aside} set aside</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
