/**
 * Groups transcript segments into speaker turns for the reading view.
 *
 * Whisper emits segments, not turns: a segment is a chunk of audio the model
 * decoded together, typically a few seconds, and a person speaking for a
 * minute produces a dozen of them. Rendering one paragraph per segment reads
 * as stutter. Grouping them into turns is what makes a transcript read like a
 * conversation.
 *
 * There is no diarization behind this. Two rules decide where a turn ends:
 *
 *   1. **A change of assigned speaker.** Once the operator has labelled
 *      segments, their labels are the truth and the grouping follows them
 *      exactly — that is the whole point of labelling.
 *   2. **A silence.** Before anything is labelled, a gap of at least
 *      `gapSeconds` between one segment ending and the next beginning is the
 *      only signal available that the floor may have changed. It is a guess,
 *      and it is presented as one: turns start unassigned, and the operator
 *      corrects them.
 *
 * Rule 1 outranks rule 2, so a labelled turn is never split by a pause in
 * the middle of someone's sentence.
 */
import type { TranscriptSegmentView } from "../types";

/** One run of consecutive segments attributed to a single speaker. */
export type Turn = {
  /** Stable across regrouping: the id of the segment the turn starts at. */
  id: string;
  segmentIds: string[];
  start: number;
  end: number;
  text: string;
  /** `null` for a turn nobody has attributed yet — a real state, shown as
   * an invitation to label rather than as a fabricated "Speaker 1". */
  speaker: string | null;
};

/**
 * The pause, in seconds, taken as a possible change of speaker. Long enough
 * that a breath mid-sentence does not split a turn, short enough that an
 * actual handover usually does.
 */
export const DEFAULT_GAP_SECONDS = 2;

export type GroupOptions = {
  gapSeconds?: number;
};

/**
 * Groups `segments` into turns, honouring `speakers` (a
 * `segment id -> name` map) above pause detection.
 *
 * Segments whose text is only whitespace are dropped: they carry no reading
 * value and would otherwise open turns of their own. The underlying
 * transcript keeps them.
 */
export function groupIntoTurns(
  segments: TranscriptSegmentView[],
  speakers: Record<string, string>,
  options: GroupOptions = {},
): Turn[] {
  const gapSeconds = options.gapSeconds ?? DEFAULT_GAP_SECONDS;
  const readable = segments.filter((segment) => segment.text.trim().length > 0);

  const turns: Turn[] = [];
  let current: Turn | null = null;
  let previousEnd: number | null = null;

  for (const segment of readable) {
    const id = String(segment.id);
    const speaker = speakers[id] ?? null;
    const silence = previousEnd !== null && segment.start - previousEnd >= gapSeconds;
    const speakerChanged = current !== null && current.speaker !== speaker;
    // A silence only starts a new turn while the speech around it is
    // unattributed; once labelled, the labels decide.
    const startsNewTurn =
      current === null ||
      speakerChanged ||
      (speaker === null && current.speaker === null && silence);

    if (startsNewTurn) {
      current = {
        id,
        segmentIds: [id],
        start: segment.start,
        end: segment.end,
        text: segment.text.trim(),
        speaker,
      };
      turns.push(current);
    } else if (current !== null) {
      current.segmentIds.push(id);
      current.end = segment.end;
      current.text = `${current.text} ${segment.text.trim()}`.trim();
    }
    previousEnd = segment.end;
  }

  return turns;
}

/**
 * Every distinct speaker name in use, in the order they first speak.
 *
 * First-speech order rather than alphabetical: this list is offered when
 * attributing a turn, and the person who has just been speaking is far more
 * likely to be the answer than the one whose name sorts first.
 */
export function speakerNames(turns: Turn[]): string[] {
  const seen: string[] = [];
  for (const turn of turns) {
    if (turn.speaker !== null && !seen.includes(turn.speaker)) {
      seen.push(turn.speaker);
    }
  }
  return seen;
}

/**
 * Assigns `speaker` to every segment of `turn`, returning the new map.
 *
 * Pure: the caller decides when to persist. Passing `null` clears the
 * attribution rather than storing an empty name.
 */
export function assignSpeaker(
  speakers: Record<string, string>,
  turn: Turn,
  speaker: string | null,
): Record<string, string> {
  const next = { ...speakers };
  for (const segmentId of turn.segmentIds) {
    if (speaker === null || speaker.trim().length === 0) {
      delete next[segmentId];
    } else {
      next[segmentId] = speaker.trim();
    }
  }
  return next;
}

/**
 * Renames a speaker everywhere they appear — the "renames every segment"
 * rule from the design. Renaming onto a name already in use merges the two,
 * which is what an operator who has typed the same person's name twice
 * actually wants.
 */
export function renameSpeaker(
  speakers: Record<string, string>,
  from: string,
  to: string,
): Record<string, string> {
  const trimmed = to.trim();
  if (trimmed.length === 0) return speakers;
  const next: Record<string, string> = {};
  for (const [segmentId, name] of Object.entries(speakers)) {
    next[segmentId] = name === from ? trimmed : name;
  }
  return next;
}

/**
 * The turns whose text matches `query`, case-insensitively. An empty or
 * whitespace-only query matches everything, so clearing the search box
 * restores the full transcript rather than emptying it.
 */
export function filterTurns(turns: Turn[], query: string): Turn[] {
  const needle = query.trim().toLowerCase();
  if (needle.length === 0) return turns;
  return turns.filter(
    (turn) =>
      turn.text.toLowerCase().includes(needle) ||
      (turn.speaker !== null && turn.speaker.toLowerCase().includes(needle)),
  );
}
