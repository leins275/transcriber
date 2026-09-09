/**
 * Places a floating box near a pointer anchor without letting it leave the
 * window.
 *
 * The selection speaker popover opens where the drag was released, which is
 * wherever the operator happened to stop — including the last 50 px of the
 * right or the bottom edge, where a `left: x; top: y` placement pushes the
 * speaker buttons and the name box outside the window and out of reach.
 * Deciding that in CSS is not possible: it needs the box's measured size and
 * the window's size, so it is decided here, in one pure function with no DOM
 * and no React, and applied as inline coordinates by the caller.
 *
 * The order of operations is:
 *
 *   1. **Prefer centred below.** `left = anchor.x - width / 2`,
 *      `top = anchor.y + gap` — the placement the CSS transform used to give,
 *      and the one that reads as "this menu belongs to what I just selected".
 *   2. **Flip above before sliding up.** When the box would cross the bottom
 *      edge it is moved to `anchor.y - gap - height`, provided that still
 *      clears the top margin. Sliding up instead would park the popover on
 *      top of the very text just highlighted, and that highlight is the only
 *      cue of what is about to be attributed. Flipping keeps it visible;
 *      sliding is the fallback for when neither side has room.
 *   3. **Clamp both axes last.** `Math.max(margin, Math.min(value, max))` per
 *      axis, the lower bound applied second so it wins when the range is
 *      empty — a box wider or taller than the viewport lands at `margin`
 *      rather than at a negative coordinate.
 *
 * An unmeasured box (`{ width: 0, height: 0 }`, what the caller has before
 * its first layout read) is placed at the anchor plus the gap and clamped the
 * same way, so even the first paint sits inside the window.
 */

/** A point in viewport (client) coordinates. */
export type Point = { x: number; y: number };

/** The size of a box, in CSS pixels. */
export type Size = { width: number; height: number };

/** Viewport coordinates for a `position: fixed` box. */
export type Placement = { left: number; top: number };

/** Distance kept between the box and every viewport edge, in px. */
const DEFAULT_MARGIN = 8;

/** Distance kept between the anchor and the near edge of the box, in px. */
const DEFAULT_GAP = 8;

export type ClampOptions = {
  /** Distance kept from every viewport edge. Defaults to 8 px. */
  margin?: number;
  /** Distance kept from the anchor, above or below it. Defaults to 8 px. */
  gap?: number;
};

/**
 * Where to place a box of `size` anchored at `anchor` inside `viewport`.
 *
 * Returns coordinates for the box's top-left corner. Both are finite and
 * never below `margin`.
 */
export function clampToViewport(
  anchor: Point,
  size: Size,
  viewport: Size,
  options?: ClampOptions,
): Placement {
  const margin = options?.margin ?? DEFAULT_MARGIN;
  const gap = options?.gap ?? DEFAULT_GAP;

  const left = anchor.x - size.width / 2;

  const below = anchor.y + gap;
  const above = anchor.y - gap - size.height;
  const overflowsBottom = below + size.height > viewport.height - margin;
  const top = overflowsBottom && above >= margin ? above : below;

  return {
    left: clamp(left, margin, viewport.width - margin - size.width),
    top: clamp(top, margin, viewport.height - margin - size.height),
  };
}

/**
 * `value` held between `min` and `max`, with `min` winning an empty range.
 *
 * A box larger than the viewport makes `max` smaller than `min`; pinning it
 * to the top-left margin keeps the part of it the operator needs first on
 * screen, where clamping to `max` would push it off the opposite edge.
 */
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, max));
}
