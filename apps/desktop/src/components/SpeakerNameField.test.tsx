import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpeakerNameField } from "./SpeakerNameField";

type FieldProps = React.ComponentProps<typeof SpeakerNameField>;

/**
 * The field is controlled the way its two parents drive it: the caller owns
 * the text and hands it back on every keystroke. Rendering it inside a tiny
 * stateful holder keeps the tests about the control's behaviour rather than
 * about React's controlled-input plumbing.
 */
function renderField(props: Partial<FieldProps> = {}, initialValue = "") {
  function Holder() {
    const [value, setValue] = useState(initialValue);
    const defaults = {
      ariaLabel: "Name this speaker",
      onCommit: () => {},
    };
    return <SpeakerNameField {...defaults} {...props} value={value} onChange={setValue} />;
  }

  return render(<Holder />);
}

function optionValues(select: HTMLElement) {
  return Array.from(select.querySelectorAll("option"), (option) => option.value);
}

function datalistValues(container: HTMLElement, control: HTMLElement) {
  const listId = control.getAttribute("list");
  if (listId === null) return null;
  return Array.from(container.querySelectorAll(`datalist[id="${listId}"] option`), (option) =>
    option.getAttribute("value"),
  );
}

describe("SpeakerNameField", () => {
  it("offers the project's remembered names as typing suggestions", () => {
    // Open mode: the name is free text, and the sibling-meeting scan only
    // suggests -- it never limits what can be typed.
    const { container } = renderField({ suggestions: ["Даниил", "Anna"] });

    // The `list` attribute upgrades the input's role from textbox to combobox.
    const input = screen.getByRole("combobox", { name: "Name this speaker" });
    expect(datalistValues(container, input)).toEqual(["Даниил", "Anna"]);
  });

  it("renders no suggestion list when the project remembers no names", () => {
    const { container } = renderField({ suggestions: [] });

    const input = screen.getByRole("textbox", { name: "Name this speaker" });
    expect(datalistValues(container, input)).toBeNull();
  });

  it("commits the typed name on Enter", async () => {
    const onCommit = vi.fn();
    const user = userEvent.setup();
    renderField({ onCommit });

    await user.type(screen.getByRole("textbox", { name: "Name this speaker" }), "Olga{Enter}");

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith("Olga");
  });

  it("commits an emptied box unchanged, leaving the parent to decide it means nobody", async () => {
    // SpeakerTag reads a blank commit as "unattribute this turn" and the
    // selection popover reads it as "changed my mind": the shared control
    // reports the text and judges none of it.
    const onCommit = vi.fn();
    const user = userEvent.setup();
    renderField({ onCommit }, "Maxim");

    await user.clear(screen.getByRole("textbox", { name: "Name this speaker" }));
    await user.keyboard("{Enter}");

    expect(onCommit).toHaveBeenCalledWith("");
  });

  it("cancels the typed name on Escape", async () => {
    const onCommit = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    renderField({ onCommit, onCancel });

    await user.type(screen.getByRole("textbox", { name: "Name this speaker" }), "Olga{Escape}");

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("commits on blur when the parent asked for it", async () => {
    const onCommit = vi.fn();
    const user = userEvent.setup();
    renderField({ onCommit, commitOnBlur: true });

    const input = screen.getByRole("textbox", { name: "Name this speaker" });
    await user.type(input, "Olga");
    fireEvent.blur(input);

    expect(onCommit).toHaveBeenCalledWith("Olga");
  });

  it("keeps a blurred box uncommitted unless the parent asked for it", async () => {
    const onCommit = vi.fn();
    const user = userEvent.setup();
    renderField({ onCommit });

    const input = screen.getByRole("textbox", { name: "Name this speaker" });
    await user.type(input, "Olga");
    fireEvent.blur(input);

    expect(onCommit).not.toHaveBeenCalled();
  });

  it("focuses the free-text box when asked to", () => {
    renderField({ autoFocus: true });

    expect(screen.getByRole("textbox", { name: "Name this speaker" })).toHaveFocus();
  });

  it("offers exactly the roster names behind a placeholder, and no free text", () => {
    // Roster mode is strict: the roster is the whole vocabulary, so a name
    // that is not on it cannot be typed into a turn.
    renderField({ roster: ["Anna", "Maxim"] });

    const select = screen.getByRole("combobox", { name: "Name this speaker" });
    expect(optionValues(select)).toEqual(["", "Anna", "Maxim"]);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("commits the roster name that was picked", async () => {
    const onCommit = vi.fn();
    const user = userEvent.setup();
    renderField({ roster: ["Anna", "Maxim"], onCommit });

    await user.selectOptions(screen.getByRole("combobox", { name: "Name this speaker" }), "Anna");

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith("Anna");
  });

  it("shows a name from outside the roster as the current one", () => {
    // A label from before the roster existed, or a pre-name the service
    // recognised: the picker must never display someone else's name in its
    // place.
    renderField({ roster: ["Anna", "Maxim"], ariaLabel: "Rename Olga" }, "Olga");

    const select = screen.getByRole("combobox", { name: "Rename Olga" });
    expect(select).toHaveValue("Olga");
    expect(optionValues(select)).toEqual(["", "Anna", "Maxim", "Olga"]);
  });

  it("cancels the picker on Escape", async () => {
    const onCommit = vi.fn();
    const onCancel = vi.fn();
    renderField({ roster: ["Anna", "Maxim"], onCommit, onCancel });

    fireEvent.keyDown(screen.getByRole("combobox", { name: "Name this speaker" }), {
      key: "Escape",
    });

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("offers nothing to pick when the roster is empty, and says so", () => {
    // An empty roster in strict mode is a dead end by design: the way out is
    // the roster editor, not a name typed here.
    renderField({ roster: [] });

    const select = screen.getByRole("combobox", { name: "Name this speaker" });
    const options = Array.from(select.querySelectorAll("option"));
    expect(options).toHaveLength(1);
    expect(options[0]).toBeDisabled();
    expect(options[0]).toHaveTextContent(/empty/i);
  });

  it("focuses the picker when asked to", () => {
    renderField({ roster: ["Anna", "Maxim"], autoFocus: true });

    expect(screen.getByRole("combobox", { name: "Name this speaker" })).toHaveFocus();
  });
});
