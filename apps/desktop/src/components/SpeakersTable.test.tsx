import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpeakersTable } from "./SpeakersTable";
import { DEFAULT_SPEAKER_SORT } from "../lib/speakerSort";
import type { SpeakerView } from "../types";

function person(overrides: Partial<SpeakerView> = {}): SpeakerView {
  return {
    name: "Anna",
    aliases: [],
    bio: "",
    registered: true,
    projects: ["ELS"],
    meetings: 1,
    labelled_segments: 10,
    hand_segments: 10,
    speech_sec: 60,
    voice_samples: 1,
    voice_set_aside: 0,
    ...overrides,
  };
}

const PEOPLE: SpeakerView[] = [
  person({
    name: "Nikita",
    aliases: ["Никита"],
    projects: ["ELS", "GIS"],
    meetings: 34,
    speech_sec: 12345,
    voice_samples: 26,
    voice_set_aside: 5,
  }),
  person({ name: "Anna", meetings: 7, speech_sec: 252, voice_samples: 2 }),
  person({ name: "Boris", registered: false, projects: [], meetings: 0, speech_sec: 0 }),
];

/** The filter and the sort are controlled (App owns them so they survive a
 * speaker's page); this harness plays App's role. */
function Harness(props: Partial<React.ComponentProps<typeof SpeakersTable>>) {
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState(DEFAULT_SPEAKER_SORT);
  return (
    <SpeakersTable
      onLoad={() => Promise.resolve(PEOPLE)}
      onAdd={() => Promise.resolve()}
      onOpen={() => {}}
      {...props}
      filter={filter}
      onFilterChange={setFilter}
      sort={sort}
      onSortChange={setSort}
    />
  );
}

/** The names in the order the table shows them. */
async function shownNames(): Promise<string[]> {
  const table = await screen.findByRole("table");
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getByRole("button").textContent ?? "");
}

describe("SpeakersTable", () => {
  it("lists everybody alphabetically, with aliases, projects and counts", async () => {
    render(<Harness />);

    expect(await shownNames()).toEqual(["Anna", "Boris", "Nikita"]);
    const row = screen.getByRole("button", { name: "Nikita" }).closest("tr") as HTMLElement;
    expect(within(row).getByText("also Никита")).toBeInTheDocument();
    expect(within(row).getByText("ELS, GIS")).toBeInTheDocument();
    expect(within(row).getByText("34")).toBeInTheDocument();
    expect(within(row).getByText("3h 25m")).toBeInTheDocument();
    expect(within(row).getByText(/5 set aside/)).toBeInTheDocument();
  });

  it("says nothing about set-aside samples when there are none", async () => {
    render(<Harness />);

    const row = (await screen.findByRole("button", { name: "Anna" })).closest("tr") as HTMLElement;
    expect(within(row).queryByText(/set aside/)).not.toBeInTheDocument();
  });

  it("sorts by a column header, and flips on the second click", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await shownNames();

    await user.click(screen.getByRole("button", { name: /^Meetings/ }));
    expect(await shownNames()).toEqual(["Nikita", "Anna", "Boris"]);
    expect(screen.getByRole("columnheader", { name: /^Meetings/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );

    await user.click(screen.getByRole("button", { name: /^Meetings/ }));
    expect(await shownNames()).toEqual(["Boris", "Anna", "Nikita"]);
  });

  it("filters over names and aliases", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await shownNames();

    await user.type(screen.getByRole("searchbox", { name: "Filter speakers" }), "никит");

    expect(await shownNames()).toEqual(["Nikita"]);
  });

  it("says so when the filter matches nobody", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await shownNames();

    await user.type(screen.getByRole("searchbox", { name: "Filter speakers" }), "zzz");

    expect(screen.getByText(/No speaker matches/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens a speaker's page by their canonical name", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<Harness onOpen={onOpen} />);

    await user.click(await screen.findByRole("button", { name: "Nikita" }));

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("Nikita");
  });

  it("shows an empty state when nobody is known", async () => {
    render(<Harness onLoad={() => Promise.resolve([])} />);

    expect(await screen.findByText(/Nobody is known yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("adds a speaker under the trimmed name, then reads the database again", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn(() => Promise.resolve());
    const onLoad = vi
      .fn<() => Promise<SpeakerView[]>>()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([person({ name: "Olga", projects: [], meetings: 0 })]);
    render(<Harness onAdd={onAdd} onLoad={onLoad} />);
    await screen.findByText(/Nobody is known yet/);

    await user.click(screen.getByRole("button", { name: "Add speaker" }));
    await user.type(screen.getByRole("textbox", { name: "Speaker name" }), "  Olga {Enter}");

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith("Olga"));
    expect(await screen.findByRole("button", { name: "Olga" })).toBeInTheDocument();
    expect(onLoad).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("textbox", { name: "Speaker name" })).not.toBeInTheDocument();
  });

  it("refuses to add a blank name", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn(() => Promise.resolve());
    render(<Harness onAdd={onAdd} />);
    await shownNames();

    await user.click(screen.getByRole("button", { name: "Add speaker" }));
    await user.type(screen.getByRole("textbox", { name: "Speaker name" }), "   ");

    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("keeps the add form open and shows why when adding fails", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn(() => Promise.reject({ kind: "invalid_argument", message: "too long" }));
    render(<Harness onAdd={onAdd} />);
    await shownNames();

    await user.click(screen.getByRole("button", { name: "Add speaker" }));
    await user.type(screen.getByRole("textbox", { name: "Speaker name" }), "Olga");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("too long");
    expect(screen.getByRole("textbox", { name: "Speaker name" })).toHaveValue("Olga");
  });

  it("reports a failed read instead of an empty database", async () => {
    render(
      <Harness
        onLoad={() => Promise.reject({ kind: "service_unavailable", message: "service is down" })}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("service is down");
    expect(screen.queryByText(/Nobody is known yet/)).not.toBeInTheDocument();
  });
});
