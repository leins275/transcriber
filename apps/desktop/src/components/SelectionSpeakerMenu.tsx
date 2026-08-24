import { useEffect, useRef, useState } from "react";
import styles from "./SelectionSpeakerMenu.module.css";

export type SelectionSpeakerMenuProps = {
  /** Names already in use in this transcript, in first-speech order. */
  known: string[];
  /** Viewport point the popover hangs from — the selection's rectangle. */
  anchor: { x: number; y: number };
  /** Attribute the current selection to `name`. */
  onAssign: (name: string) => void;
  /** Close without touching any attribution. */
  onDismiss: () => void;
};

/**
 * The popover offered over a text selection, for attributing exactly that
 * stretch of transcript to a speaker.
 *
 * Unlike `SpeakerTag`, this control has no trigger element: it hangs off a
 * text selection, so there is no button that owns focus and no element the
 * operator is guaranteed to be inside. Escape, click-away and scroll
 * therefore have to be heard at the document, in one effect that also
 * removes them.
 *
 * Nothing is focused on open, deliberately: moving focus into the input
 * would collapse the very selection the operator is looking at, and the
 * highlight is the only thing showing what is about to be attributed.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function SelectionSpeakerMenu({
  known,
  anchor,
  onAssign,
  onDismiss,
}: SelectionSpeakerMenuProps) {
  const [draft, setDraft] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onDismiss();
      }
    }

    function handlePointerDown(event: Event) {
      const target = event.target;
      if (target instanceof Node && menuRef.current?.contains(target)) return;
      onDismiss();
    }

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown);
    // Positioned in viewport coordinates, so a scroll leaves the popover
    // pointing at text that has moved out from under it. Closing is honest;
    // chasing the selection would cost a layout read per frame. Captured,
    // because a scroll inside a pane does not bubble.
    document.addEventListener("scroll", onDismiss, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("scroll", onDismiss, true);
    };
  }, [onDismiss]);

  function commitDraft() {
    const trimmed = draft.trim();
    // A blank box is an operator who changed their mind, not a nameless
    // speaker worth storing.
    if (trimmed.length === 0) return;
    onAssign(trimmed);
  }

  return (
    <div
      ref={menuRef}
      className={styles.menu}
      style={{ left: anchor.x, top: anchor.y }}
      role="group"
      aria-label="Attribute the selected text to a speaker"
    >
      {/* Reuse before retyping, same rule and same order as SpeakerTag: on a
          two-person call the other name is almost always the answer, and
          retyping it invites a typo that would silently create a third
          speaker. */}
      {known.map((name) => (
        <button
          key={name}
          type="button"
          className={styles.name}
          // Named for the action, not just the person -- a bare "Maxim" is
          // ambiguous to anything that cannot see which control it sits in.
          aria-label={`Attribute selection to ${name}`}
          onClick={() => onAssign(name)}
        >
          {name}
        </button>
      ))}
      <span className={styles.new}>
        <input
          className={styles.input}
          value={draft}
          aria-label="Attribute selection to a new speaker"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commitDraft();
            }
          }}
        />
        <span className={styles.hint}>Enter to assign</span>
      </span>
    </div>
  );
}
