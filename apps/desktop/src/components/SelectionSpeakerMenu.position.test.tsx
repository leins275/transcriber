import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SelectionSpeakerMenu } from "./SelectionSpeakerMenu";

// jsdom has no layout engine: every element measures 0x0 and the window is
// whatever we say it is. Stubbing the rect and the window size stands in for
// the absent browser, not for any collaborator of the component.
const MENU_BOX = { width: 200, height: 40 };
let measure: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  window.innerWidth = 1000;
  window.innerHeight = 800;
  measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    ...MENU_BOX,
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: MENU_BOX.width,
    bottom: MENU_BOX.height,
    toJSON: () => ({}),
  } as DOMRect);
});

afterEach(() => {
  vi.restoreAllMocks();
  window.innerWidth = 1024;
  window.innerHeight = 768;
});

function menuProps(anchor: { x: number; y: number }) {
  return {
    known: ["Maxim", "Anna"],
    anchor,
    onAssign: () => {},
    onDismiss: () => {},
  };
}

function renderMenuAt(anchor: { x: number; y: number }) {
  return render(<SelectionSpeakerMenu {...menuProps(anchor)} />);
}

function popover() {
  return screen.getByRole("group", { name: /attribute the selected text/i });
}

describe("SelectionSpeakerMenu placement", () => {
  it("slides left so a selection near the right edge keeps the whole popover on screen", () => {
    renderMenuAt({ x: 950, y: 300 });

    expect(popover().style.left).toBe("792px");
    expect(popover().style.top).toBe("308px");
  });

  it("flips above the anchor when there is no room below", () => {
    // Sliding up alone would cover the very text just selected; the highlight
    // is the only cue of what is about to be attributed.
    renderMenuAt({ x: 500, y: 780 });

    expect(popover().style.top).toBe("732px");
  });

  it("centres on the anchor when the window has room on every side", () => {
    renderMenuAt({ x: 500, y: 300 });

    expect(popover().style.left).toBe("400px");
  });

  it("re-places itself when the selection moves to a new anchor", () => {
    const { rerender } = renderMenuAt({ x: 500, y: 300 });

    rerender(<SelectionSpeakerMenu {...menuProps({ x: 950, y: 300 })} />);

    expect(popover().style.left).toBe("792px");
  });

  it("re-places itself against the new window size after a resize", () => {
    renderMenuAt({ x: 500, y: 300 });

    window.innerWidth = 600;
    fireEvent(window, new Event("resize"));

    expect(popover().style.left).toBe("392px");
  });

  it("stops measuring once unmounted", () => {
    const { unmount } = renderMenuAt({ x: 500, y: 300 });
    unmount();
    measure.mockClear();

    fireEvent(window, new Event("resize"));

    expect(measure).not.toHaveBeenCalled();
  });
});
