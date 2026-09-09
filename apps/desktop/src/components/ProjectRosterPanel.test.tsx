import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectRosterPanel } from "./ProjectRosterPanel";
import type { ProjectRosterView } from "../types";

function buildRoster(overrides: Partial<ProjectRosterView> = {}): ProjectRosterView {
  return { mode: "open", names: ["Anna", "Maxim"], ...overrides };
}

function renderPanel(props: Partial<React.ComponentProps<typeof ProjectRosterPanel>> = {}) {
  const defaults = {
    roster: buildRoster(),
    siblingNames: [] as string[],
    onSave: () => Promise.resolve(),
    onClose: () => {},
  };
  return render(<ProjectRosterPanel {...defaults} {...props} />);
}

const ANYONE = /anyone/i;
const ONLY_THE_ROSTER = /only the roster below/i;
const SEED_BUTTON = "Add names already used in this project";

describe("ProjectRosterPanel", () => {
  it("checks the mode the project has persisted", () => {
    renderPanel({ roster: buildRoster({ mode: "roster" }) });

    expect(screen.getByRole("radio", { name: ONLY_THE_ROSTER })).toBeChecked();
    expect(screen.getByRole("radio", { name: ANYONE })).not.toBeChecked();
  });

  it("groups the two modes under one question", () => {
    renderPanel();

    expect(
      screen.getByRole("group", { name: "Who can be named in this project" }),
    ).toBeInTheDocument();
  });

  it("offers a Remove button for every name in the roster", () => {
    renderPanel({ roster: buildRoster({ names: ["Anna", "Maxim"] }) });

    expect(screen.getByRole("button", { name: "Remove Anna" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove Maxim" })).toBeInTheDocument();
  });

  it("offers the add field, the seed button and the save and cancel actions", () => {
    renderPanel();

    expect(screen.getByRole("textbox", { name: "Add a name" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: SEED_BUTTON })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("appends the trimmed typed name when Enter is pressed in the add field", async () => {
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ names: ["Anna"] }) });

    await user.type(screen.getByRole("textbox", { name: "Add a name" }), "  Olga {Enter}");

    expect(screen.getAllByRole("button", { name: "Remove Olga" })).toHaveLength(1);
  });

  it("appends the typed name when the Add button is pressed", async () => {
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ names: ["Anna"] }) });

    await user.type(screen.getByRole("textbox", { name: "Add a name" }), "Olga");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByRole("button", { name: "Remove Olga" })).toBeInTheDocument();
  });

  it("ignores a typed name the roster already holds under a different casing", async () => {
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ names: ["Anna", "Olga"] }) });

    await user.type(screen.getByRole("textbox", { name: "Add a name" }), "olga{Enter}");

    expect(screen.getAllByRole("button", { name: /^Remove Olga$/i })).toHaveLength(1);
  });

  it("drops exactly the name whose Remove button was pressed", async () => {
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ names: ["Anna", "Maxim"] }) });

    await user.click(screen.getByRole("button", { name: "Remove Anna" }));

    expect(screen.queryByRole("button", { name: "Remove Anna" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove Maxim" })).toBeInTheDocument();
  });

  it("seeds only the names used in the project that the roster does not already hold", async () => {
    const user = userEvent.setup();
    renderPanel({
      roster: buildRoster({ names: ["Maxim"] }),
      siblingNames: ["Maxim", "Olga"],
    });

    await user.click(screen.getByRole("button", { name: SEED_BUTTON }));

    expect(screen.getByRole("button", { name: "Remove Olga" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove Maxim" })).toHaveLength(1);
  });

  it("disables the seed button when every name used in the project is already listed", () => {
    renderPanel({
      roster: buildRoster({ names: ["Anna", "Maxim"] }),
      siblingNames: ["maxim", "Anna"],
    });

    expect(screen.getByRole("button", { name: SEED_BUTTON })).toBeDisabled();
  });

  it("hands the switched mode and the edited names to the save", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ mode: "open", names: ["Anna"] }), onSave });

    await user.click(screen.getByRole("radio", { name: ONLY_THE_ROSTER }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(onSave).toHaveBeenCalledWith({ mode: "roster", names: ["Anna"] });
  });

  it("closes the panel once the save has landed", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSave: () => Promise.resolve(), onClose });

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("keeps the panel open and shows the backend's refusal when the save is rejected", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onSave: () => Promise.reject({ message: "boom" }), onClose });

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("discards the draft on cancel without saving", async () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ roster: buildRoster({ names: ["Anna"] }), onSave, onClose });

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });
});
