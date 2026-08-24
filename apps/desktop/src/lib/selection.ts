/**
 * Maps a text selection over the transcript onto the segments it covers.
 *
 * Attribution is stored per segment (`speakers.json` is a `segment id ->
 * name` map), so a pointer selection has to be resolved to whole segments
 * before anything can be assigned. Two rules make that resolution honest:
 *
 *   1. **Snap outward.** A segment the selection covers any character of is
 *      included whole — that is what will actually be stored, and dropping a
 *      partly covered segment would silently lose the edge of what the
 *      operator dragged over. A boundary that merely *touches* a segment,
 *      covering none of it, is not coverage; see `coversText`.
 *   2. **Document order.** The result follows the order the segments are
 *      rendered in, which is transcript order, so a selection spanning the
 *      end of one turn and the start of the next comes back as one run.
 *
 * Everything here reads structure, never geometry: no `getBoundingClientRect`,
 * no layout. That keeps it correct under jsdom and cheap enough to run on
 * every pointer-up of an hour-long transcript.
 */

/** The attribute the viewer stamps on each rendered segment span. */
const SEGMENT_ATTRIBUTE = "data-segment-id";

/**
 * Whether `range` covers at least one character of `span`'s text.
 *
 * Sitting *on* a segment is not covering it: a drag released just past the
 * end of a sentence is normalized by the browser to offset 0 of the next
 * segment's text node, and a drag begun just after one to the trailing
 * offset of the previous. Both are boundary touches over zero characters,
 * and attributing them would hand the operator's speaker to a sentence they
 * never dragged over. `intersectsNode` alone answers "true" to both, which
 * is the whole reason this exists.
 *
 * The real question is answered by clamping the span's contents to the range
 * and asking the result for its text — boundary points only, still no
 * geometry. That costs more than `intersectsNode`, so `intersectsNode`
 * screens first: it is never false for a span that is genuinely covered, and
 * it is false for nearly every span of an hour-long transcript.
 *
 * `overlap` is the caller's scratch range, reused down a whole scan rather
 * than allocated per span — a live range per span of a 1000-segment
 * transcript is measurably slow.
 */
function coversText(range: Range, span: HTMLElement, overlap: Range): boolean {
  if (!range.intersectsNode(span)) return false;

  overlap.selectNodeContents(span);
  // Either clamp collapses the overlap when the range falls entirely on one
  // side of the span, which is exactly the empty answer wanted there.
  if (range.compareBoundaryPoints(Range.START_TO_START, overlap) > 0) {
    overlap.setStart(range.startContainer, range.startOffset);
  }
  if (range.compareBoundaryPoints(Range.END_TO_END, overlap) < 0) {
    overlap.setEnd(range.endContainer, range.endOffset);
  }
  return overlap.toString().length > 0;
}

/**
 * Resolves a selection boundary to an index into `spans`.
 *
 * The common case is a boundary inside a segment's own text, which `closest`
 * answers directly — but only once it is confirmed to cover any of it. A
 * boundary can also land on turn chrome (the timestamp, the speaker cell,
 * the whitespace between two spans) because the operator started the drag in
 * the margin, or sit on a segment it covers by nothing at all; then the
 * answer is the nearest segment inwards that the range does cover. `-1`
 * means the selection covers no segment at all.
 */
function boundaryIndex(range: Range, spans: HTMLElement[], edge: "start" | "end"): number {
  const node = edge === "start" ? range.startContainer : range.endContainer;
  const element = node instanceof Element ? node : node.parentElement;
  const span = element?.closest<HTMLElement>(`[${SEGMENT_ATTRIBUTE}]`) ?? null;
  const direct = span === null ? -1 : spans.indexOf(span);
  const overlap = spans[0].ownerDocument.createRange();
  if (direct !== -1 && coversText(range, spans[direct], overlap)) return direct;

  // Whatever the boundary landed on is covered by nothing, so the search
  // carries on inwards from it — `direct + 1` is 0 when there was no hit.
  if (edge === "start") {
    for (let index = direct + 1; index < spans.length; index += 1) {
      if (coversText(range, spans[index], overlap)) return index;
    }
    return -1;
  }
  for (let index = direct === -1 ? spans.length - 1 : direct - 1; index >= 0; index -= 1) {
    if (coversText(range, spans[index], overlap)) return index;
  }
  return -1;
}

/**
 * The ids of the segments `range` overlaps inside `root`, in document order.
 *
 * Returns `[]` for a collapsed selection (a caret is not a selection) and for
 * a selection that touches no segment text — the toolbar, a timestamp, an
 * empty transcript. Pure: it reads the DOM and nothing else.
 */
export function segmentIdsFromRange(range: Range, root: HTMLElement): string[] {
  if (range.collapsed) return [];

  const spans = Array.from(root.querySelectorAll<HTMLElement>(`[${SEGMENT_ATTRIBUTE}]`));
  if (spans.length === 0) return [];

  const start = boundaryIndex(range, spans, "start");
  const end = boundaryIndex(range, spans, "end");
  if (start === -1 || end === -1) return [];

  const from = Math.min(start, end);
  const to = Math.max(start, end);
  return spans
    .slice(from, to + 1)
    .map((span) => span.getAttribute(SEGMENT_ATTRIBUTE))
    .filter((id): id is string => id !== null);
}
