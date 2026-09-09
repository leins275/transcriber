import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SelectionSpeakerMenu } from "./SelectionSpeakerMenu";

/**
 * The selection popover under a *project roster* (strict mode): the free-text
 * box is replaced by a pick-list over the project's allowed names, so a typo
 * can no longer invent a speaker. Open mode -- no roster -- is the existing
 * `SelectionSpeakerMenu.test.tsx` suite's territory; only the one contrast
 * case that proves the roster prop is what switches modes lives here.
 */

type MenuProps = Partial<React.ComponentProps<typeof SelectionSpeakerMenu>> & {
  roster?: string[];
};

function renderMenu(props: MenuProps = {}) {
  const defaults = {
    known: ["Maxim", "Anna"],
    anchor: { x: 120, y: 240 },
    onAssign: () => {},
    onDismiss: () => {},
  };
  return render(<SelectionSpeakerMenu {...defaults} {...props} />);
}

function rosterPicker() {
  return screen.getByRole("combobox", {
    name: "Attribute selection to a speaker",
  }) as HTMLSelectElement;
}

describe("SelectionSpeakerMenu with a project roster", () => {
  it("offers the project roster instead of a box to type a name into", () => {
    renderMenu({ roster: ["Anna", "Maxim"] });

    // The leading valueless option is the placeholder: it names nobody, so
    // choosing it can attribute nothing.
    expect(Array.from(rosterPicker().options, (option) => option.value)).toEqual([
      "",
      "Anna",
      "Maxim",
    ]);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("attributes the selection to the roster name that was picked", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ roster: ["Anna", "Maxim"], onAssign });

    await user.selectOptions(rosterPicker(), "Maxim");

    expect(onAssign).toHaveBeenCalledWith("Maxim");
    expect(onAssign).toHaveBeenCalledTimes(1);
  });

  it("offers nothing to pick when the project roster is still empty", () => {
    renderMenu({ roster: [] });

    const options = within(rosterPicker()).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toBeDisabled();
  });

  it("attributes nothing when the empty roster's only option is chosen", () => {
    // The guard against wiring change straight through to onAssign: an empty
    // roster would then silently attribute the selection to "".
    const onAssign = vi.fn();
    renderMenu({ roster: [], onAssign });

    fireEvent.change(rosterPicker(), { target: { value: "" } });

    expect(onAssign).not.toHaveBeenCalled();
  });

  it("still attributes the selection to a known name in one click", async () => {
    // Reuse before picking: the transcript's own speakers stay buttons in
    // roster mode too.
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ roster: ["Anna", "Maxim"], onAssign });

    await user.click(screen.getByRole("button", { name: "Attribute selection to Anna" }));

    expect(onAssign).toHaveBeenCalledWith("Anna");
  });

  it("takes a freely typed name when the project has no roster", async () => {
    // Open mode: the absence of a roster is what keeps today's free text.
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderMenu({ onAssign });

    await user.type(
      screen.getByRole("textbox", { name: "Attribute selection to a new speaker" }),
      "Олег{Enter}",
    );

    expect(onAssign).toHaveBeenCalledWith("Олег");
  });
});
