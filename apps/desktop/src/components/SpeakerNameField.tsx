import { useEffect, useId, useRef } from "react";
import styles from "./SpeakerNameField.module.css";

export type SpeakerNameFieldProps = {
  /** The name as it currently stands. The parent owns it — this control
   * keeps no copy of its own. */
  value: string;
  /** Every keystroke, and the picked name in roster mode. */
  onChange: (value: string) => void;
  /** The operator is done: Enter, a blur when `commitOnBlur`, or a pick from
   * the roster. The value arrives exactly as it was typed — untrimmed, blank
   * included — because what a blank name means differs per parent (the tag
   * unattributes the turn, the selection popover reads it as a change of
   * mind) and this control judges none of it. */
  onCommit: (value: string) => void;
  /** Escape. Omitted where something else already owns Escape — the
   * selection popover hears it at the document. */
  onCancel?: () => void;
  /** Commit when focus leaves, for a parent whose editor has no other way
   * out (the speaker tag). */
  commitOnBlur?: boolean;
  /** The accessible name, phrased for the action the parent is offering
   * ("Name this speaker", "Rename Maxim", …) — a bare "Speaker" would be
   * ambiguous to anything that cannot see which control it sits in. */
  ariaLabel: string;
  /** The parent's own styling for its name box, so both modes sit in the
   * layout the same way. */
  className?: string;
  /** Names remembered across the project, offered while typing. Open mode
   * only: they suggest, they never limit. */
  suggestions?: string[];
  /** Present ⇒ roster mode: the project allows only these names, so the
   * control becomes a picker over them. Undefined ⇒ open mode, free text. */
  roster?: string[];
  autoFocus?: boolean;
};

/**
 * The one control for naming a speaker, in either of the project's two
 * moods.
 *
 * With no roster it is today's free-text box with the sibling-meeting names
 * hanging off it as a `datalist`. With a roster it is a strict picker: the
 * roster is the project's whole vocabulary, so a name that is not on it
 * cannot be typed into a turn — adding one means opening the roster editor,
 * one click away. Both moods live here rather than in `SpeakerTag` and
 * `SelectionSpeakerMenu` so that neither of those files has to know the
 * difference: they hand over a label, a value and a callback.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function SpeakerNameField({
  value,
  onChange,
  onCommit,
  onCancel,
  commitOnBlur = false,
  ariaLabel,
  className,
  suggestions = [],
  roster,
  autoFocus = false,
}: SpeakerNameFieldProps) {
  const controlRef = useRef<HTMLInputElement | HTMLSelectElement | null>(null);
  const suggestionsId = useId();

  useEffect(() => {
    if (autoFocus) controlRef.current?.focus();
  }, [autoFocus]);

  function handleEscape(event: React.KeyboardEvent) {
    if (event.key !== "Escape" || onCancel === undefined) return;
    // Deliberately not stopped from propagating: the selection popover
    // listens for Escape at the document and has to keep hearing it.
    event.preventDefault();
    onCancel();
  }

  if (roster === undefined) {
    // A `list` pointing at an empty datalist would still upgrade the box to a
    // combobox and promise suggestions that are not there, so an empty scan
    // renders no list at all.
    const listId = suggestions.length > 0 ? suggestionsId : undefined;
    return (
      <>
        <input
          ref={(node) => {
            controlRef.current = node;
          }}
          className={className}
          value={value}
          aria-label={ariaLabel}
          list={listId}
          onChange={(event) => onChange(event.target.value)}
          onBlur={() => {
            if (commitOnBlur) onCommit(value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onCommit(value);
              return;
            }
            handleEscape(event);
          }}
        />
        {listId !== undefined && (
          <datalist id={listId}>
            {suggestions.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        )}
      </>
    );
  }

  const isEmptyRoster = roster.length === 0;
  // A label from before the roster existed, or a name the service pre-named
  // a voice with. It is listed last and left alone: the picker must never
  // show someone else's name in place of the one on the turn. Compared
  // exactly, so a spelling the roster does not carry — "anna" against
  // "Anna" — still shows as itself rather than silently reading as another
  // entry.
  const offRosterValue = value !== "" && !roster.includes(value);

  return (
    <select
      ref={(node) => {
        controlRef.current = node;
      }}
      className={[styles.select, className].filter(Boolean).join(" ")}
      value={value}
      aria-label={ariaLabel}
      onChange={(event) => {
        const picked = event.target.value;
        onChange(picked);
        onCommit(picked);
      }}
      onKeyDown={handleEscape}
    >
      {/* The placeholder names nobody, so choosing it hands the parent a
          blank — which is how an operator takes a name back off a turn. An
          empty roster has nothing behind it, so it says so and is disabled:
          a dead end by design, whose way out is the roster editor. */}
      <option value="" disabled={isEmptyRoster}>
        {isEmptyRoster ? "The project roster is empty" : "Choose a name…"}
      </option>
      {roster.map((name) => (
        <option key={name} value={name}>
          {name}
        </option>
      ))}
      {offRosterValue && <option value={value}>{value}</option>}
    </select>
  );
}
