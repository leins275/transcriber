import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultList } from "./VaultList";
import { DEFAULT_VAULT_SORT } from "../lib/vaultSort";
import type { VaultMeetingView } from "../types";

function buildEntry(id: string, meeting_name: string): VaultMeetingView {
  return {
    id,
    project: "ELS",
    meeting_name,
    meeting_dir: `D:\\Meetings\\ELS\\${meeting_name}`,
    has_source: true,
    has_transcript: true,
  };
}

const actions = {
  onOpen: () => {},
  sort: DEFAULT_VAULT_SORT,
  onSortChange: () => {},
};

/** The body rows: every row but the header's. */
function bodyRows(): HTMLElement[] {
  return screen.getAllByRole("row").slice(1);
}

describe("VaultList", () => {
  it("renders every entry in the order given, keyed by id, none lost", () => {
    const entries = [
      buildEntry("a", "260812 - One"),
      buildEntry("b", "260811 - Two"),
      buildEntry("c", "260810 - Three"),
    ];
    render(<VaultList entries={entries} {...actions} />);
    const rows = bodyRows();
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("One"),
      expect.stringContaining("Two"),
      expect.stringContaining("Three"),
    ]);
  });

  it("renders an empty table without error when there are no entries", () => {
    render(<VaultList entries={[]} {...actions} />);
    expect(bodyRows()).toHaveLength(0);
  });

  it("orders its columns as the naming convention does, transcript last", () => {
    render(<VaultList entries={[]} {...actions} />);
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      expect.stringContaining("Project"),
      expect.stringContaining("Date"),
      expect.stringContaining("Name"),
      expect.stringContaining("Type"),
      expect.stringContaining("Transcript"),
    ]);
  });

  it("marks the sorted column and applies the sort it is given", () => {
    const entries = [buildEntry("a", "260812 - Beta"), buildEntry("b", "260811 - Alpha")];
    render(
      <VaultList entries={entries} {...actions} sort={{ column: "name", direction: "asc" }} />,
    );
    expect(bodyRows().map((row) => row.textContent)).toEqual([
      expect.stringContaining("Alpha"),
      expect.stringContaining("Beta"),
    ]);
    expect(screen.getByRole("columnheader", { name: /^name/i })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    expect(screen.getByRole("columnheader", { name: /^date/i })).toHaveAttribute(
      "aria-sort",
      "none",
    );
  });

  it("asks for the next sort when a header is clicked", async () => {
    const onSortChange = vi.fn();
    const user = userEvent.setup();
    render(<VaultList entries={[]} {...actions} onSortChange={onSortChange} />);

    await user.click(screen.getByRole("button", { name: /^type/i }));

    expect(onSortChange).toHaveBeenCalledWith({ column: "type", direction: "asc" });
  });
});
