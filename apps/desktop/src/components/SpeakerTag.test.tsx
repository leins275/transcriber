import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpeakerTag } from "./SpeakerTag";

/**
 * The tag plus a focusable sibling outside it: "focus left the tag" is a real
 * event only when there is somewhere else to go, and jsdom only reports a
 * `relatedTarget` when focus lands on something focusable.
 */
function renderTag(props: Partial<React.ComponentProps<typeof SpeakerTag>> = {}) {
  const defaults = {
    speaker: "Speaker 2" as string | null,
    known: ["Speaker 2", "Maxim"],
    turnsHeld: 3,
    onAssign: () => {},
    onRename: () => {},
  };
  return render(
    <>
      <SpeakerTag {...defaults} {...props} />
      <button type="button">Elsewhere</button>
    </>,
  );
}

function editorInput() {
  return screen.getByRole("textbox", { name: /edit speaker for this turn/i });
}

describe("SpeakerTag", () => {
  it("asks which turns a changed name applies to before writing anything", async () => {
    // The whole point of the feature: typing over "Speaker 2" is ambiguous,
    // so the tag states the blast radius as a number and waits.
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());

    await user.type(editorInput(), "Anna{Enter}");

    expect(screen.getByRole("button", { name: "Only this turn" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All 3 turns of Speaker 2" })).toBeInTheDocument();
    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
  });

  it("gives this turn alone to the new name when the operator picks the narrow scope", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");

    await user.click(screen.getByRole("button", { name: "Only this turn" }));

    expect(onAssign).toHaveBeenCalledTimes(1);
    expect(onAssign).toHaveBeenCalledWith("Anna");
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renames the speaker everywhere when the operator picks the wide scope", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");

    await user.click(screen.getByRole("button", { name: "All 3 turns of Speaker 2" }));

    expect(onRename).toHaveBeenCalledTimes(1);
    expect(onRename).toHaveBeenCalledWith("Speaker 2", "Anna");
    expect(onAssign).not.toHaveBeenCalled();
  });

  it("hands this turn to a name already in use in the transcript", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({
      speaker: "Speaker 2",
      known: ["Speaker 2", "Anna"],
      turnsHeld: 3,
      onAssign,
      onRename,
    });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");

    await user.click(screen.getByRole("button", { name: "Only this turn" }));

    expect(onAssign).toHaveBeenCalledWith("Anna");
    expect(onRename).not.toHaveBeenCalled();
  });

  it("trims the typed name before writing it", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "  Anna  {Enter}");

    await user.click(screen.getByRole("button", { name: "Only this turn" }));

    expect(onAssign).toHaveBeenCalledWith("Anna");
  });

  it("writes nothing when the name is retyped unchanged", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());

    await user.type(editorInput(), "Speaker 2{Enter}");

    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Only this turn" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("opens the chooser on the narrower choice so a reflexive Enter cannot rename everyone", async () => {
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3 });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());

    await user.type(editorInput(), "Anna{Enter}");

    expect(screen.getByRole("button", { name: "Only this turn" })).toHaveFocus();
  });

  it("puts the wide choice one Tab away, and fires it from the keyboard", async () => {
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");
    const wideChoice = screen.getByRole("button", { name: "All 3 turns of Speaker 2" });

    await user.tab();
    expect(wideChoice).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(onRename).toHaveBeenCalledTimes(1);
    expect(onRename).toHaveBeenCalledWith("Speaker 2", "Anna");
  });

  it("promises a scope choice while editing a name several turns hold", async () => {
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3 });

    await user.click(screen.getByRole("button", { name: "Speaker 2" }));

    expect(screen.getByText(/choose the scope/i)).toBeInTheDocument();
    expect(screen.queryByText(/every segment/i)).not.toBeInTheDocument();
  });

  it("commits straight away when nobody else holds the name", async () => {
    // One turn: "only this turn" and "all 1 turns" would write the same map,
    // so asking would be noise.
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", known: ["Maxim"], turnsHeld: 1, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Maxim" }));
    await user.clear(editorInput());

    await user.type(editorInput(), "Anna{Enter}");

    expect(onAssign).toHaveBeenCalledWith("Anna");
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Only this turn" })).not.toBeInTheDocument();
  });

  it("says the edit is this turn only when nobody else holds the name", async () => {
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", known: ["Maxim"], turnsHeld: 1 });

    await user.click(screen.getByRole("button", { name: "Maxim" }));

    expect(screen.getByText(/this turn only/i)).toBeInTheDocument();
  });

  it("abandons the edit on Escape in the input", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna");

    await user.keyboard("{Escape}");

    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("abandons the edit on Escape while the chooser is open", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");

    await user.keyboard("{Escape}");

    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("discards an unanswered chooser when focus leaves the tag", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna{Enter}");

    await user.click(screen.getByRole("button", { name: "Elsewhere" }));

    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("discards an unconfirmed change to an existing name when focus leaves the tag", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());
    await user.type(editorInput(), "Anna");

    await user.click(screen.getByRole("button", { name: "Elsewhere" }));

    expect(onAssign).not.toHaveBeenCalled();
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Speaker 2" })).toBeInTheDocument();
  });

  it("names an unattributed turn on Enter", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: null, known: [], turnsHeld: 0, onAssign });
    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    await user.type(screen.getByRole("textbox", { name: /name this speaker/i }), "Maxim{Enter}");

    expect(onAssign).toHaveBeenCalledWith("Maxim");
  });

  it("names an unattributed turn when focus leaves the tag", async () => {
    // Nothing is at stake on a turn nobody has claimed, so the typed name is
    // kept rather than thrown away.
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: null, known: [], turnsHeld: 0, onAssign });
    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.type(screen.getByRole("textbox", { name: /name this speaker/i }), "Maxim");

    await user.click(screen.getByRole("button", { name: "Elsewhere" }));

    expect(onAssign).toHaveBeenCalledWith("Maxim");
  });

  it("asks only for a name on an unattributed turn", async () => {
    const user = userEvent.setup();
    renderTag({ speaker: null, known: [], turnsHeld: 0 });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(screen.getByText(/enter to name/i)).toBeInTheDocument();
  });

  it("unattributes just this turn when the box is emptied", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Speaker 2", turnsHeld: 3, onAssign, onRename });
    await user.click(screen.getByRole("button", { name: "Speaker 2" }));
    await user.clear(editorInput());

    await user.type(editorInput(), "{Enter}");

    expect(onAssign).toHaveBeenCalledWith(null);
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Only this turn" })).not.toBeInTheDocument();
  });

  it("still attributes this turn to a known name in one click", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({
      speaker: "Speaker 2",
      known: ["Speaker 2", "Maxim"],
      turnsHeld: 3,
      onAssign,
      onRename,
    });

    await user.click(screen.getByRole("button", { name: /attribute this turn to maxim/i }));

    expect(onAssign).toHaveBeenCalledWith("Maxim");
    expect(onRename).not.toHaveBeenCalled();
  });
});
