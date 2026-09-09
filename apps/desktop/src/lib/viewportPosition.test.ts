import { describe, expect, it } from "vitest";
import { clampToViewport } from "./viewportPosition";

/**
 * The selection popover is placed at the pointer-release point and must stay
 * inside the window. Every expectation below is a hand-computed coordinate for
 * a 200x40 popover in a 1000x800 window with an 8 px margin and an 8 px gap,
 * unless the case says otherwise -- never recomputed from the inputs.
 */
describe("clampToViewport", () => {
  it("centres the box on the anchor and drops it below by the gap when everything fits", () => {
    const placement = clampToViewport(
      { x: 500, y: 300 },
      { width: 200, height: 40 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 400, top: 308 });
  });

  it("slides a box that would cross the right edge back inside the margin", () => {
    const placement = clampToViewport(
      { x: 950, y: 300 },
      { width: 200, height: 40 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 792, top: 308 });
  });

  it("slides a box that would cross the left edge back inside the margin", () => {
    const placement = clampToViewport(
      { x: 50, y: 300 },
      { width: 200, height: 40 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 8, top: 308 });
  });

  it("flips the box above the anchor when it would cross the bottom edge", () => {
    const placement = clampToViewport(
      { x: 500, y: 780 },
      { width: 200, height: 40 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 400, top: 732 });
  });

  it("slides the box up when there is room neither below nor above the anchor", () => {
    const placement = clampToViewport(
      { x: 500, y: 50 },
      { width: 200, height: 40 },
      { width: 1000, height: 60 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 400, top: 12 });
  });

  it("keeps the top inside the margin when the box is taller than the room below the anchor", () => {
    const placement = clampToViewport(
      { x: 500, y: 4 },
      { width: 200, height: 40 },
      { width: 1000, height: 60 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 400, top: 12 });
  });

  it("pins a box wider than the viewport to the left margin instead of a negative coordinate", () => {
    const placement = clampToViewport(
      { x: 500, y: 300 },
      { width: 1200, height: 40 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 8, top: 308 });
  });

  it("places an unmeasured zero-size box at the anchor plus the gap", () => {
    const placement = clampToViewport(
      { x: 500, y: 300 },
      { width: 0, height: 0 },
      { width: 1000, height: 800 },
      { margin: 8, gap: 8 },
    );

    expect(placement).toEqual({ left: 500, top: 308 });
  });

  it("defaults the margin and the gap to 8 px when no options are given", () => {
    const placement = clampToViewport(
      { x: 500, y: 300 },
      { width: 200, height: 40 },
      { width: 1000, height: 800 },
    );

    expect(placement).toEqual({ left: 400, top: 308 });
  });
});
