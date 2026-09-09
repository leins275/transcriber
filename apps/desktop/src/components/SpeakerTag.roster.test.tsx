import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpeakerTag } from "./SpeakerTag";

function renderTag(props: Partial<React.ComponentProps<typeof SpeakerTag>> = {}) {
  const defaults = {
    speaker: null,
    known: [],
    onAssign: () => {},
    onRename: () => {},
  };
  return render(<SpeakerTag {...defaults} {...props} />);
}

/** The names an operator can actually choose: the placeholder option carries
 * no name (blank value, or disabled), so it is not one of them. */
function offeredNames(picker: HTMLElement) {
  return within(picker)
    .getAllByRole("option")
    .map((option) => option as HTMLOptionElement)
    .filter((option) => option.value !== "" && !option.disabled)
    .map((option) => option.value);
}

describe("SpeakerTag with a project roster", () => {
  it("suggests the project's remembered names while typing when the project keeps no roster", async () => {
    const user = userEvent.setup();
    const { container } = renderTag({ suggestions: ["Даниил", "Anna"] });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    const field = screen.getByLabelText("Name this speaker");
    const listId = field.getAttribute("list");
    expect(listId).toBeTruthy();
    expect(
      Array.from(container.querySelectorAll(`datalist[id="${listId}"] option`), (option) =>
        option.getAttribute("value"),
      ),
    ).toEqual(["Даниил", "Anna"]);
  });

  it("names an unattributed speaker with any typed name when the project keeps no roster", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ suggestions: ["Даниил"], onAssign });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.type(screen.getByLabelText("Name this speaker"), "Olga{Enter}");

    expect(onAssign).toHaveBeenCalledWith("Olga");
  });

  it("offers only the roster names, and no typing at all, when naming an unattributed speaker", async () => {
    // Strict roster mode: a new name is added in the roster editor, never
    // invented at the transcript -- that is the whole point of the mode.
    const user = userEvent.setup();
    renderTag({ roster: ["Anna", "Maxim"], suggestions: ["Даниил"] });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(offeredNames(screen.getByLabelText("Name this speaker"))).toEqual(["Anna", "Maxim"]);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("attributes an unattributed turn to the roster name the operator picks", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ roster: ["Anna", "Maxim"], onAssign });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Name this speaker" }), "Anna");

    expect(onAssign).toHaveBeenCalledWith("Anna");
  });

  it("returns to the tag once a roster name has been picked", async () => {
    const user = userEvent.setup();
    renderTag({ roster: ["Anna", "Maxim"] });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Name this speaker" }), "Anna");

    expect(screen.queryByLabelText("Name this speaker")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add speaker/i })).toBeInTheDocument();
  });

  it("shows the current speaker as the pick already made when the tag is opened", async () => {
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", roster: ["Anna", "Maxim"] });

    await user.click(screen.getByRole("button", { name: "Maxim" }));

    expect(screen.getByRole("combobox", { name: "Rename Maxim" })).toHaveValue("Maxim");
  });

  it("renames the speaker throughout when another roster name is picked", async () => {
    // Editing an existing name means "this speaker is actually called Anna",
    // not "this one turn was Anna" -- a roster only changes where the name
    // comes from, never which of the two acts it is.
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", roster: ["Anna", "Maxim"], onAssign, onRename });

    await user.click(screen.getByRole("button", { name: "Maxim" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Rename Maxim" }), "Anna");

    expect(onRename).toHaveBeenCalledWith("Maxim", "Anna");
    expect(onAssign).not.toHaveBeenCalled();
  });

  it("keeps a name from before the roster as the current pick while still offering the roster", async () => {
    // A pre-roster label or a service pre-name is left alone: the control must
    // never display someone else's name for this speaker.
    const user = userEvent.setup();
    renderTag({ speaker: "Olga", roster: ["Anna", "Maxim"] });

    await user.click(screen.getByRole("button", { name: "Olga" }));

    const picker = screen.getByRole("combobox", { name: "Rename Olga" });
    expect(picker).toHaveValue("Olga");
    expect(offeredNames(picker)).toEqual(["Anna", "Maxim", "Olga"]);
  });

  it("leaves the speaker untouched when the roster pick is abandoned with Escape", async () => {
    const onAssign = vi.fn();
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", roster: ["Anna", "Maxim"], onAssign, onRename });

    await user.click(screen.getByRole("button", { name: "Maxim" }));
    screen.getByRole("combobox", { name: "Rename Maxim" }).focus();
    await user.keyboard("{Escape}");

    expect(onRename).not.toHaveBeenCalled();
    expect(onAssign).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Maxim" })).toBeInTheDocument();
  });

  it("still attributes a turn to a name already speaking in this call while in roster mode", async () => {
    const onAssign = vi.fn();
    const user = userEvent.setup();
    renderTag({ speaker: "Maxim", known: ["Maxim", "Anna"], roster: ["Anna", "Maxim"], onAssign });

    await user.click(screen.getByRole("button", { name: "Attribute this turn to Anna" }));

    expect(onAssign).toHaveBeenCalledWith("Anna");
  });

  it("tells the operator that the offered names come from the project roster", async () => {
    const user = userEvent.setup();
    renderTag({ roster: ["Anna", "Maxim"] });

    await user.click(screen.getByRole("button", { name: /add speaker/i }));

    expect(screen.getByText(/roster/i)).toBeInTheDocument();
    expect(screen.queryByText(/enter to name/i)).not.toBeInTheDocument();
  });
});
