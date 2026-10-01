import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectRosterPanel } from "./ProjectRosterPanel";
import type { ProjectRosterView } from "../types";

/**
 * The roster editor's pick-list over the speakers database: how a person
 * known in one project is put on another project's roster under the same
 * spelling, without retyping the name.
 */

function renderPanel(props: Partial<React.ComponentProps<typeof ProjectRosterPanel>> = {}) {
  const defaults = {
    roster: { mode: "roster", names: ["Anna"] } as ProjectRosterView,
    siblingNames: [] as string[],
    databaseNames: ["Anna", "Nikita", "Olga"],
    onSave: () => Promise.resolve(),
    onClose: () => {},
  };
  return render(<ProjectRosterPanel {...defaults} {...props} />);
}

const PICKER = "From the speakers database";

function offeredNames(): string[] {
  const picker = screen.getByRole("combobox", { name: PICKER });
  return within(picker)
    .getAllByRole("option")
    .slice(1)
    .map((option) => option.textContent ?? "");
}

describe("ProjectRosterPanel speakers-database picker", () => {
  it("offers the database's people who are not on the roster yet", () => {
    renderPanel();

    expect(offeredNames()).toEqual(["Nikita", "Olga"]);
  });

  it("treats a name already listed under another casing as on the roster", () => {
    renderPanel({ roster: { mode: "open", names: ["nikita"] }, databaseNames: ["Nikita", "Olga"] });

    expect(offeredNames()).toEqual(["Olga"]);
  });

  it("does not render the picker when the database has nobody new", () => {
    renderPanel({ databaseNames: ["Anna"] });

    expect(screen.queryByRole("combobox", { name: PICKER })).not.toBeInTheDocument();
  });

  it("does not render the picker when no database names were handed in", () => {
    renderPanel({ databaseNames: undefined });

    expect(screen.queryByRole("combobox", { name: PICKER })).not.toBeInTheDocument();
  });

  it("adds the picked person to the draft and stops offering them", async () => {
    const user = userEvent.setup();
    renderPanel();
    const add = screen.getByRole("button", { name: "Add to roster" });
    expect(add).toBeDisabled();

    await user.selectOptions(screen.getByRole("combobox", { name: PICKER }), "Nikita");
    await user.click(add);

    expect(screen.getByRole("button", { name: "Remove Nikita" })).toBeInTheDocument();
    expect(offeredNames()).toEqual(["Olga"]);
    expect(screen.getByRole("combobox", { name: PICKER })).toHaveValue("");
  });

  it("saves the picked name with exactly the database's spelling", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(() => Promise.resolve());
    renderPanel({ onSave });

    await user.selectOptions(screen.getByRole("combobox", { name: PICKER }), "Olga");
    await user.click(screen.getByRole("button", { name: "Add to roster" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({ mode: "roster", names: ["Anna", "Olga"] }),
    );
  });

  it("offers a person again once they are removed from the draft", async () => {
    const user = userEvent.setup();
    renderPanel({ databaseNames: ["Anna", "Nikita"] });

    await user.click(screen.getByRole("button", { name: "Remove Anna" }));

    expect(offeredNames()).toEqual(["Anna", "Nikita"]);
  });

  it("keeps the seed button beside the picker", () => {
    renderPanel({ siblingNames: ["Maxim"] });

    expect(
      screen.getByRole("button", { name: "Add names already used in this project" }),
    ).toBeEnabled();
  });
});
