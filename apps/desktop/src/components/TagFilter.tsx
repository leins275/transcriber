import styles from "./TagFilter.module.css";
import {
  EMPTY_TAG_FILTER,
  hasTag,
  toggleTag,
  type TagFilter,
  type TagMatch,
} from "../lib/tagFilter";

export type TagFilterProps = {
  /** The tags on offer — every tag the rows in view carry, A→Z. */
  available: string[];
  /** The current choice, owned by the caller so it outlives this panel. */
  filter: TagFilter;
  onChange: (filter: TagFilter) => void;
};

const MATCH_LABELS: { match: TagMatch; label: string; hint: string }[] = [
  { match: "any", label: "Any", hint: "a recording needs one of the chosen tags (or)" },
  { match: "all", label: "All", hint: "a recording needs every chosen tag (and)" },
];

/**
 * The second view control over the library table: narrow to recordings by
 * tag. Every tag in view is a toggle chip; once one is chosen, a switch
 * decides whether the chips combine as "or" (`Any`) or "and" (`All`), and
 * a Clear button takes the whole choice back.
 *
 * Presentational only: no invoke, no listen, no fetch.
 */
export function TagFilterBar({ available, filter, onChange }: TagFilterProps) {
  const chosen = filter.tags.length > 0;

  return (
    <div className={styles.bar} role="group" aria-label="Tags">
      <span className={styles.kicker}>Tags</span>
      <div className={styles.chips}>
        {available.map((tag) => {
          const pressed = hasTag(filter, tag);
          return (
            <button
              key={tag}
              type="button"
              className={styles.chip}
              aria-pressed={pressed}
              onClick={() => onChange(toggleTag(filter, tag))}
            >
              {tag}
            </button>
          );
        })}
      </div>
      {chosen && (
        <div className={styles.match} role="radiogroup" aria-label="Match">
          {MATCH_LABELS.map(({ match, label, hint }) => (
            <button
              key={match}
              type="button"
              role="radio"
              aria-checked={filter.match === match}
              className={styles.matchButton}
              title={hint}
              onClick={() => onChange({ ...filter, match })}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {chosen && (
        <button
          type="button"
          className={`btn btn-ghost ${styles.clear}`}
          onClick={() => onChange({ ...EMPTY_TAG_FILTER, match: filter.match })}
        >
          Clear
        </button>
      )}
    </div>
  );
}
