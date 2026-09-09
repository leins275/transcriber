import { useId, useState } from "react";
import styles from "./ProjectRosterPanel.module.css";
import { addRosterName, mergeRosterNames, removeRosterName } from "../lib/roster";
import type { ProjectRosterView, RosterMode } from "../types";

export type ProjectRosterPanelProps = {
  /** The roster as the project has it persisted; the panel edits a copy. */
  roster: ProjectRosterView;
  /** Names the sibling scan already found in this project's meetings —
   * what the seed button offers. Never merged on its own: seeding is the
   * operator's explicit act. */
  siblingNames: string[];
  /** Resolves when the save has landed; rejects with an `AppError`-shaped
   * value whose `message` is shown inline, panel left open. */
  onSave: (roster: ProjectRosterView) => Promise<void>;
  /** Called after a landed save and on Cancel — the panel does not know
   * whether it is a drawer, a dialog or a section. */
  onClose: () => void;
};

/** A roster's contents as one comparable string, so "is this still the roster
 * the draft was seeded from?" survives the loading hook handing us a fresh
 * object for an unchanged roster. NUL separates because a name cannot hold one.
 */
function identityOf(roster: ProjectRosterView): string {
  return [roster.mode, ...roster.names].join("\u0000");
}

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * The editor for one project's speaker roster.
 *
 * Two decisions live in this form. The mode decides whether naming a speaker
 * anywhere in the project is free text or a pick-list ("strict" roster mode),
 * and the list decides what that pick-list holds. Both are drafted locally
 * and only leave the panel on Save, so Cancel is a real discard.
 *
 * The names the project already uses are *not* pulled in automatically: an
 * operator switching to roster mode gets an empty list until they press the
 * seed button, because silently importing every historical misspelling is
 * how a roster stops being an answer to "who is in this project".
 *
 * The draft goes through the same normalization helpers the command applies
 * before writing (`lib/roster.ts`), so the list on screen is the list that
 * will be stored — a name never changes shape on save.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function ProjectRosterPanel({
  roster,
  siblingNames,
  onSave,
  onClose,
}: ProjectRosterPanelProps) {
  const [draft, setDraft] = useState<ProjectRosterView>(roster);
  const [seededFrom, setSeededFrom] = useState<string>(() => identityOf(roster));
  const [typed, setTyped] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const modeName = useId();

  // The panel can be opened before `useProjectRoster` has answered — the hook
  // shows `EMPTY_ROSTER` while the read is in flight. A draft frozen at mount
  // would let an untouched Save write that empty roster over the persisted
  // file, so while the draft is still exactly what we seeded it with, the
  // persisted roster keeps replacing it. Once the operator has changed
  // anything, their draft wins and a late read never overwrites it.
  const persisted = identityOf(roster);
  if (persisted !== seededFrom) {
    setSeededFrom(persisted);
    if (identityOf(draft) === seededFrom) setDraft(roster);
  }

  const seeded = mergeRosterNames(draft, siblingNames);
  const nothingToSeed = seeded.names.length === draft.names.length;

  function setMode(mode: RosterMode) {
    setDraft((prev) => ({ mode, names: prev.names }));
  }

  function addTyped() {
    setDraft((prev) => addRosterName(prev, typed));
    setTyped("");
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft);
      onClose();
    } catch (caught) {
      setError(messageOf(caught));
      setSaving(false);
    }
  }

  return (
    <form className={styles.panel} onSubmit={handleSubmit} aria-label="Project speaker roster">
      <fieldset className={styles.modes}>
        <legend className={styles.legend}>Who can be named in this project</legend>
        <label className={styles.mode}>
          <input
            type="radio"
            name={modeName}
            checked={draft.mode === "open"}
            onChange={() => setMode("open")}
            disabled={saving}
          />
          <span>Anyone — type any name</span>
        </label>
        <label className={styles.mode}>
          <input
            type="radio"
            name={modeName}
            checked={draft.mode === "roster"}
            onChange={() => setMode("roster")}
            disabled={saving}
          />
          <span>Only the roster below</span>
        </label>
      </fieldset>

      <p className={styles.lead}>
        In roster mode every speaker picker in this project offers exactly these names — nobody new
        gets typed into a transcript by accident.
      </p>

      {draft.names.length > 0 ? (
        <ul className={styles.names}>
          {draft.names.map((name) => (
            <li key={name.toLowerCase()} className={styles.name}>
              <span>{name}</span>
              <button
                type="button"
                className="btn btn-ghost"
                aria-label={`Remove ${name}`}
                disabled={saving}
                onClick={() => setDraft((prev) => removeRosterName(prev, name))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.empty}>No names yet.</p>
      )}

      <div className={styles.addRow}>
        <label className={styles.field}>
          <span className={styles.label}>Add a name</span>
          <input
            className={styles.input}
            value={typed}
            placeholder="Anna"
            disabled={saving}
            onChange={(event) => setTyped(event.target.value)}
            onKeyDown={(event) => {
              // Enter belongs to the field, not to the form: adding a name is
              // what the operator means here, saving is a separate press.
              if (event.key !== "Enter") return;
              event.preventDefault();
              addTyped();
            }}
          />
        </label>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={saving || typed.trim().length === 0}
          onClick={addTyped}
        >
          Add
        </button>
      </div>

      <button
        type="button"
        className="btn btn-secondary"
        disabled={saving || nothingToSeed}
        onClick={() => setDraft(seeded)}
      >
        Add names already used in this project
      </button>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className={styles.actions}>
        <button type="submit" className="btn" disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={saving}
          onClick={() => onClose()}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
