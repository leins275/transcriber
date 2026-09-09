import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import type { RefObject } from "react";
import { clampToViewport } from "../lib/viewportPosition";
import type { Placement, Point } from "../lib/viewportPosition";

/**
 * Viewport coordinates for a `position: fixed` box anchored at `anchor`,
 * kept inside the window.
 *
 * A popover opened where a drag was released only knows whether it fits
 * after it has been rendered: its width and height come out of the font,
 * the names it lists and the operator's zoom. So the placement is a two-step
 * affair -- render at the anchor with an unmeasured (zero) box, then read
 * the rendered size once and place it properly *before the browser paints*,
 * which is what `useLayoutEffect` buys over `useEffect`: no frame at the raw
 * anchor, no visible jump.
 *
 * The measurement happens exactly twice: once per anchor change and once per
 * `resize` event. Nothing polls, and there is no `ResizeObserver` -- the box
 * only ever changes size when the anchor changes with it.
 *
 * @param anchor point in viewport coordinates the box hangs from
 * @param ref the box itself, for the one layout read per placement
 */
export function useClampedPosition(anchor: Point, ref: RefObject<HTMLElement | null>): Placement {
  // The first paint has nothing to measure yet; an unmeasured box is still
  // clamped, so even that paint sits inside the window.
  const [placement, setPlacement] = useState<Placement>(() =>
    clampToViewport(anchor, { width: 0, height: 0 }, windowSize()),
  );

  const measure = useCallback(() => {
    const box = ref.current;
    if (!box) return;
    const rect = box.getBoundingClientRect();
    setPlacement(
      clampToViewport(
        { x: anchor.x, y: anchor.y },
        { width: rect.width, height: rect.height },
        windowSize(),
      ),
    );
    // The anchor is read by value: a caller that rebuilds `{ x, y }` on every
    // render would otherwise re-measure on every render.
  }, [anchor.x, anchor.y, ref]);

  useLayoutEffect(measure, [measure]);

  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return placement;
}

/** The window's inner size, as the box the placement has to stay inside. */
function windowSize() {
  return { width: window.innerWidth, height: window.innerHeight };
}
