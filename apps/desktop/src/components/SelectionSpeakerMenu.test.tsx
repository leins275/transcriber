import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SelectionSpeakerMenu } from "./SelectionSpeakerMenu";

function renderMenu(props: Partial<React.ComponentProps<typeof SelectionSpeakerMenu>> = {}) {
  const defaults = {
    known: ["Maxim", "Anna"],
    anchor: { x: 120, y: 240 },
    onAssign: () => {},
    onDismiss: () => {},
  };
  return render(<SelectionSpeakerMenu {...defaults} {...props} />);
}

function knownButtons() {
  return screen
    .getAllByRole("button", { name: /attribute selection to/i })
    .map((button) => button.textContent);
}

describe("SelectionSpeakerMenu", () => {
  it("offers every known name in the order given, plus a new name", () => {
    // Reuse before retyping, and in first-speech order -- the same rule
    // SpeakerTag follows, so the two controls never disagree about who is
    // "the other speaker".
    renderMenu({ known: ["Maxim", "Anna"] });

    expect(knownButtons()).toEqual(["Maxim", "Anna"]);
    expect(screen.getByRole("textbox", { name: /new speaker/i })).toBeInTheDocument();
  });

  it("attributes the selection to a known name on click", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ onAssign });

    await user.click(screen.getByRole("button", { name: /attribute selection to anna/i }));

    expect(onAssign).toHaveBeenCalledWith("Anna");
  });

  it("attributes the selection to a new, trimmed name on Enter", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ onAssign });

    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "  Olga  {Enter}");

    expect(onAssign).toHaveBeenCalledWith("Olga");
  });

  it("ignores a blank new name rather than storing a nameless speaker", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ onAssign });

    await user.type(screen.getByRole("textbox", { name: /new speaker/i }), "   {Enter}");

    expect(onAssign).not.toHaveBeenCalled();
  });

  it("dismisses on Escape, wherever the keystroke lands", async () => {
    // The popover hangs off a text selection, not off a focused trigger, so
    // Escape has to be heard at the document -- there is no element the
    // operator is guaranteed to be in.
    const onAssign = vi.fn();
    const onDismiss = vi.fn();
    const user = userEvent.setup();
    renderMenu({ onAssign, onDismiss });

    await user.keyboard("{Escape}");

    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onAssign).not.toHaveBeenCalled();
  });

  it("dismisses on a pointer press outside, and stays open for one inside", () => {
    const onDismiss = vi.fn();
    renderMenu({ onDismiss });

    fireEvent.pointerDown(screen.getByRole("button", { name: /attribute selection to maxim/i }));
    expect(onDismiss).not.toHaveBeenCalled();

    fireEvent.pointerDown(document.body);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("dismisses when the transcript is scrolled out from under it", () => {
    // The popover is positioned in viewport coordinates, so a scroll leaves
    // it pointing at text that is no longer there. Closing beats chasing.
    const onDismiss = vi.fn();
    renderMenu({ onDismiss });

    fireEvent.scroll(document.body);

    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("stops listening once unmounted", () => {
    const onDismiss = vi.fn();
    const { unmount } = renderMenu({ onDismiss });

    unmount();
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.pointerDown(document.body);
    fireEvent.scroll(document.body);

    expect(onDismiss).not.toHaveBeenCalled();
  });
});
