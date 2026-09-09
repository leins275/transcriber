import { useEffect, useRef, useState, type FocusEvent } from "react";
import { SpeakerNameField } from "./SpeakerNameField";
import styles from "./SpeakerTag.module.css";

export type SpeakerTagProps = {
  /** `null` for a turn nobody has attributed yet. */
  speaker: string | null;
  /** Names already in use in this transcript, offered for reuse. */
  known: string[];
  /** How many turns of this transcript `speaker` currently holds, this one
   * included. `0` for an unattributed turn. Drives the scope question: with
   * one turn both answers write the same map, so it is not worth asking. */
  turnsHeld?: number;
  /** Names remembered across the whole project — offered while typing (a
   * datalist), never as buttons: a project can hold many more people than
   * this call does. */
  suggestions?: string[];
  /** Present ⇒ the project keeps a roster and only these names may be given
   * to a turn, so the editor becomes a picker over them. Undefined ⇒ the
   * project names freely and the editor stays a text box. */
  roster?: string[];
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
 * actually called Anna*. The design refuses to guess — confirming a changed
 * name asks which it was, naming the blast radius as a number ("Only this
 * turn" vs. "All 3 turns of Speaker 2"), and writes nothing until the
 * operator picks. The narrow, non-destructive choice is the focused one, so
 * a reflexive second Enter can never rename everybody.
 *
 * Nothing is ever written on the way out either: losing focus with an
 * unconfirmed edit on an existing speaker discards it. The exception is a
 * turn nobody has claimed — there is no ambiguity and nothing to lose, so a
 * typed name still commits on blur.
 *
 * A project roster changes only where a name may come from — the editor
 * becomes a picker over the project's names instead of a text box — never
 * which of the two acts an edit is, nor the scope question it raises.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function SpeakerTag({
  speaker,
  known,
  turnsHeld = 1,
  suggestions = [],
  roster,
  onAssign,
  onRename,
}: SpeakerTagProps) {
  const [mode, setMode] = useState<"idle" | "editing" | "choosing">("idle");
  const [draft, setDraft] = useState(speaker ?? "");
  const narrowChoiceRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // The editor focuses itself on mount (`SpeakerNameField autoFocus`); only
    // the hand-off to the scope question is this component's to make.
    if (mode === "choosing") narrowChoiceRef.current?.focus();
  }, [mode]);

  function commit(value: string) {
    const trimmed = value.trim();
    if (trimmed.length === 0) {
      // Clearing the box unattributes this turn rather than storing a
      // nameless speaker.
      setMode("idle");
      onAssign(null);
      return;
    }
    if (speaker === null) {
      setMode("idle");
      onAssign(trimmed);
      return;
    }
    if (trimmed === speaker) {
      setMode("idle");
      return;
    }
    if (turnsHeld <= 1) {
      // Only this turn holds the name: both scopes would write the same map,
      // and asking would be noise.
      setMode("idle");
      onAssign(trimmed);
      return;
    }
    setMode("choosing");
  }

  function cancel() {
    setDraft(speaker ?? "");
    setMode("idle");
  }

  function assignThisTurn() {
    const trimmed = draft.trim();
    setMode("idle");
    onAssign(trimmed);
  }

  function renameEverywhere() {
    const trimmed = draft.trim();
    setMode("idle");
    if (speaker !== null) onRename(speaker, trimmed);
  }

  function handleBlur(event: FocusEvent<HTMLSpanElement>) {
    // Focus moving between the input and the chooser stays inside the tag and
    // decides nothing.
    if (event.currentTarget.contains(event.relatedTarget)) return;
    // Only the free-text box needs a blur to commit: it has no other way
    // out. A picker commits the moment a name is chosen, and reading its blur
    // as a commit would turn abandoning the edit — Escape, which unmounts the
    // focused select — into an assignment.
    if (mode === "choosing" || speaker !== null || roster !== undefined) {
      cancel();
      return;
    }
    commit(draft);
  }

  if (mode !== "idle") {
    return (
      <span
        className={styles.tag}
        onBlur={handleBlur}
        onKeyDown={(event) => {
          // On the wrapper, not the editor, so Escape abandons from the
          // chooser too — and so a picker, which is handed no Escape of its
          // own here, abandons the same way.
          if (event.key === "Escape") {
            event.preventDefault();
            cancel();
          }
        }}
      >
        <SpeakerNameField
          value={draft}
          onChange={setDraft}
          onCommit={commit}
          // No onCancel and no commitOnBlur: Escape and what leaving the
          // editor means are both decided on the wrapper above, the only
          // place that can see the scope chooser as well.
          ariaLabel={speaker === null ? "Name this speaker" : "Edit speaker for this turn"}
          className={styles.input}
          suggestions={suggestions}
          roster={roster}
          autoFocus
        />
        {mode === "choosing" && speaker !== null ? (
          <span className={styles.scope}>
            <button
              ref={narrowChoiceRef}
              type="button"
              className={styles.scopeButton}
              onClick={assignThisTurn}
            >
              Only this turn
            </button>
            <button type="button" className={styles.scopeButton} onClick={renameEverywhere}>
              All {turnsHeld} turns of {speaker}
            </button>
          </span>
        ) : (
          <span className={styles.hint}>
            {roster === undefined
              ? speaker === null
                ? "Enter to name"
                : turnsHeld <= 1
                  ? "this turn only · Enter to save"
                  : "Enter, then choose the scope"
              : speaker === null
                ? "from the project roster"
                : turnsHeld <= 1
                  ? "this turn only · from the project roster"
                  : "choose a name, then the scope · from the project roster"}
          </span>
        )}
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
          setMode("editing");
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
