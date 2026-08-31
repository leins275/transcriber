import { useEffect, useId, useRef, useState } from "react";
import styles from "./SpeakerTag.module.css";

export type SpeakerTagProps = {
  /** `null` for a turn nobody has attributed yet. */
  speaker: string | null;
  /** Names already in use in this transcript, offered for reuse. */
  known: string[];
  /** Names remembered across the whole project — offered while typing (a
   * datalist), never as buttons: a project can hold many more people than
   * this call does. */
  suggestions?: string[];
  /** Attribute this turn to `name`, or clear it with `null`. */
  onAssign: (name: string | null) => void;
  /** Rename `from` to `to` everywhere in the transcript. */
  onRename: (from: string, to: string) => void;
};

/**
 * The speaker label above a turn, and the control for setting it.
 *
 * Editing one carries an ambiguity worth resolving out loud: typing over
 * "Speaker 2" could mean *this turn was someone else* or *Speaker 2 is
 * actually called Anna*. The design resolves it as the latter — "renames
 * every segment" — so editing an existing name renames that speaker
 * throughout, and attributing this turn to someone else is a separate act:
 * picking from the names already in use, or adding a new one.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function SpeakerTag({
  speaker,
  known,
  suggestions = [],
  onAssign,
  onRename,
}: SpeakerTagProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(speaker ?? "");
  const inputRef = useRef<HTMLInputElement>(null);
  const suggestionsId = useId();

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  function commit() {
    const trimmed = draft.trim();
    setEditing(false);
    if (trimmed.length === 0) {
      // Clearing the box unattributes this turn rather than storing a
      // nameless speaker.
      onAssign(null);
      return;
    }
    if (speaker === null) {
      onAssign(trimmed);
      return;
    }
    if (trimmed !== speaker) {
      onRename(speaker, trimmed);
    }
  }

  function cancel() {
    setDraft(speaker ?? "");
    setEditing(false);
  }

  if (editing) {
    return (
      <span className={styles.tag}>
        <input
          ref={inputRef}
          className={styles.input}
          value={draft}
          aria-label={speaker === null ? "Name this speaker" : `Rename ${speaker}`}
          list={suggestions.length > 0 ? suggestionsId : undefined}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "Escape") {
              event.preventDefault();
              cancel();
            }
          }}
        />
        {suggestions.length > 0 && (
          <datalist id={suggestionsId}>
            {suggestions.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        )}
        <span className={styles.hint}>
          {speaker === null ? "Enter to name" : "renames every segment · Enter to save"}
        </span>
      </span>
    );
  }

  return (
    <span className={styles.tag}>
      <button
        type="button"
        className={speaker === null ? styles.unassigned : styles.name}
        onClick={() => {
          setDraft(speaker ?? "");
          setEditing(true);
        }}
      >
        {speaker ?? "Add speaker"}
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M17 3a2.8 2.8 0 1 1 4 4L7 21l-5 1 1-5Z"></path>
        </svg>
      </button>
      {/* Reuse before retyping: on a two-person call the other name is
          almost always the answer, and typing it again invites a typo that
          would silently create a third speaker. */}
      {known
        .filter((name) => name !== speaker)
        .map((name) => (
          <button
            key={name}
            type="button"
            className={styles.other}
            // Named for the action, not just the person: two buttons
            // reading "Maxim" — one renaming him, one attributing this turn
            // to him — are indistinguishable to anything that cannot see
            // the layout.
            aria-label={`Attribute this turn to ${name}`}
            onClick={() => onAssign(name)}
          >
            {name}
          </button>
        ))}
    </span>
  );
}
