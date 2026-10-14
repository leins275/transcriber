import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultPanel } from "./VaultPanel";
import { EMPTY_TAG_FILTER } from "../lib/tagFilter";
import { DEFAULT_VAULT_SORT } from "../lib/vaultSort";
import type { JobSnapshot, LedgerJobView, VaultMeetingView } from "../types";

function buildEntry(overrides: Partial<VaultMeetingView> = {}): VaultMeetingView {
  return {
    id: "a",
    project: "ELS",
    meeting_name: "260812 - Security issue",
    meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
    has_source: true,
    has_transcript: true,
    ...overrides,
  };
}

function buildJob(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    id: "job-1",
    source_path: "C:\\Downloads\\ELS - 260822 - Incident review.mp4",
    job_type: "transcribe",
    file_name: "ELS - 260822 - Incident review.mp4",
    state: "running",
    classification: "sorted",
    meeting_dir: "D:\\Meetings\\ELS\\260822 - Incident review",
    source_dest: null,
    transcript_path: null,
    progress: 0.4,
    message: null,
    error_kind: null,
    created_at: "2026-08-22T15:00:00Z",
    ...overrides,
  };
}

/** The filter/sort pair is controlled (owned by App in production, so it
 * survives the panel's unmount); this harness plays App's role for tests
 * that drive the picker and the column headers. */
function ControlledPanel(props: Partial<React.ComponentProps<typeof VaultPanel>>) {
  const [filter, setFilter] = useState(props.filter ?? "");
  const [sort, setSort] = useState(props.sort ?? DEFAULT_VAULT_SORT);
  const [tagFilter, setTagFilter] = useState(props.tagFilter ?? EMPTY_TAG_FILTER);
  const [search, setSearch] = useState(props.search ?? "");
  const defaults = {
    entries: [buildEntry()],
    jobs: [] as JobSnapshot[],
    onOpen: () => {},
    chatTab: <div data-testid="chat-tab-slot" />,
    onRevealJob: () => {},
    onCancelJob: () => {},
    onLoadServiceLog: () => Promise.resolve<LedgerJobView[]>([]),
    onSearch: () => Promise.resolve([]),
  };
  return (
    <VaultPanel
      {...defaults}
      {...props}
      filter={filter}
      onFilterChange={setFilter}
      sort={sort}
      onSortChange={setSort}
      tagFilter={tagFilter}
      onTagFilterChange={setTagFilter}
      search={search}
      onSearchChange={setSearch}
    />
  );
}

function renderPanel(props: Partial<React.ComponentProps<typeof VaultPanel>> = {}) {
  return render(<ControlledPanel {...props} />);
}

describe("VaultPanel", () => {
  it("is one Recordings region, not a Jobs panel and a Vault panel", () => {
    renderPanel();
    expect(screen.getByRole("region", { name: /recordings/i })).toBeInTheDocument();
  });

  it("counts recordings, and says how many are in flight", () => {
    renderPanel({
      entries: [buildEntry({ id: "a" }), buildEntry({ id: "b" })],
      jobs: [buildJob()],
    });
    expect(screen.getByText(/2 recordings · 1 in flight/i)).toBeInTheDocument();
  });

  it("omits the in-flight count when nothing is running", () => {
    renderPanel();
    expect(screen.queryByText(/in flight/i)).not.toBeInTheDocument();
  });

  it("pins a live job above the list", () => {
    renderPanel({ jobs: [buildJob()] });
    expect(screen.getByText("ELS - 260822 - Incident review.mp4")).toBeInTheDocument();
  });

  it("drops a finished job from the pinned section — it is a recording now", () => {
    renderPanel({ jobs: [buildJob({ state: "done" })] });
    expect(screen.queryByText("ELS - 260822 - Incident review.mp4")).not.toBeInTheDocument();
  });

  it("keeps a rejected job visible, since it never became a recording", () => {
    renderPanel({ jobs: [buildJob({ state: "rejected", message: "unsupported extension" })] });
    expect(screen.getByText("ELS - 260822 - Incident review.mp4")).toBeInTheDocument();
  });

  it("cancels a running job by id", async () => {
    const onCancelJob = vi.fn();
    const user = userEvent.setup();
    renderPanel({ jobs: [buildJob({ id: "job-7" })], onCancelJob });

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onCancelJob).toHaveBeenCalledWith("job-7");
  });

  it("renders one flat table — no group headings, no project pages", () => {
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
        buildEntry({ id: "c", project: null, meeting_name: "260810 - loose file" }),
      ],
    });

    // Everything visible at once, unsorted included, with no structure to
    // click through first.
    expect(screen.getByText("Els meeting")).toBeInTheDocument();
    expect(screen.getByText("Gis meeting")).toBeInTheDocument();
    expect(screen.getByText("loose file")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open project/i })).not.toBeInTheDocument();
  });

  it("re-orders the table when a column header is clicked", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "GIS", meeting_name: "260812 - Gis meeting" }),
        buildEntry({ id: "b", project: null, meeting_name: "260811 - loose file" }),
        buildEntry({ id: "c", project: "ELS", meeting_name: "260810 - Els meeting" }),
      ],
    });
    const names = () =>
      screen
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getByRole("button").textContent);
    expect(names()).toEqual(["Gis meeting", "loose file", "Els meeting"]);

    await user.click(screen.getByRole("button", { name: /^project/i }));

    // Sorted, not filtered: everything is still on screen, unsorted last.
    expect(names()).toEqual(["Els meeting", "Gis meeting", "loose file"]);
    expect(screen.getByRole("columnheader", { name: /^project/i })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("narrows to one project through the picker", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
      ],
    });

    await user.selectOptions(screen.getByRole("combobox", { name: /project/i }), "GIS");

    expect(screen.getByText("Gis meeting")).toBeInTheDocument();
    expect(screen.queryByText("Els meeting")).not.toBeInTheDocument();
  });

  it("narrows to unsorted recordings through the same picker", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "c", project: null, meeting_name: "260810 - loose file" }),
      ],
    });

    await user.selectOptions(screen.getByRole("combobox", { name: /project/i }), "Unsorted");

    expect(screen.getByText("loose file")).toBeInTheDocument();
    expect(screen.queryByText("Els meeting")).not.toBeInTheDocument();
  });

  it("narrows to recordings carrying a chosen tag, any of them by default", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", meeting_name: "260812 - Weekly - Standup, Planning" }),
        buildEntry({ id: "b", meeting_name: "260811 - Budget - Planning" }),
        buildEntry({ id: "c", meeting_name: "260810 - Audit" }),
        buildEntry({ id: "d", meeting_name: "260809 - Retro - Standup" }),
      ],
    });
    const chips = within(screen.getByRole("group", { name: /tags/i }));
    // Nothing chosen yet: no any/all switch, every row on screen.
    expect(screen.queryByRole("radiogroup", { name: /match/i })).not.toBeInTheDocument();
    expect(screen.getByText("Audit")).toBeInTheDocument();

    await user.click(chips.getByRole("button", { name: "Standup" }));
    await user.click(chips.getByRole("button", { name: "Planning" }));

    expect(chips.getByRole("button", { name: "Standup" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Weekly")).toBeInTheDocument();
    expect(screen.getByText("Budget")).toBeInTheDocument();
    expect(screen.getByText("Retro")).toBeInTheDocument();
    expect(screen.queryByText("Audit")).not.toBeInTheDocument();
  });

  it("requires every chosen tag once the match switch is set to All", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", meeting_name: "260812 - Weekly - Standup, Planning" }),
        buildEntry({ id: "b", meeting_name: "260811 - Budget - Planning" }),
        buildEntry({ id: "d", meeting_name: "260809 - Retro - Standup" }),
      ],
    });
    const chips = within(screen.getByRole("group", { name: /tags/i }));
    await user.click(chips.getByRole("button", { name: "Standup" }));
    await user.click(chips.getByRole("button", { name: "Planning" }));

    const match = within(screen.getByRole("radiogroup", { name: /match/i }));
    expect(match.getByRole("radio", { name: "Any" })).toHaveAttribute("aria-checked", "true");
    await user.click(match.getByRole("radio", { name: "All" }));

    expect(screen.getByText("Weekly")).toBeInTheDocument();
    expect(screen.queryByText("Budget")).not.toBeInTheDocument();
    expect(screen.queryByText("Retro")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^clear$/i }));

    expect(screen.getByText("Budget")).toBeInTheDocument();
    expect(screen.getByText("Retro")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: /match/i })).not.toBeInTheDocument();
  });

  it("offers only the tags of the project in view, and lets a stale choice lapse", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Weekly - Standup" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Budget - Planning" }),
      ],
    });
    const chips = () => within(screen.getByRole("group", { name: /tags/i }));
    await user.click(chips().getByRole("button", { name: "Standup" }));
    expect(screen.queryByText("Budget")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: /project/i }), "GIS");

    // Standup is not on offer under GIS, so the choice no longer narrows.
    expect(chips().queryByRole("button", { name: "Standup" })).not.toBeInTheDocument();
    expect(screen.getByText("Budget")).toBeInTheDocument();
  });

  it("offers no tag row when no recording in view carries a tag", () => {
    renderPanel();
    expect(screen.queryByRole("group", { name: /tags/i })).not.toBeInTheDocument();
  });

  it("offers no filter row for a single project — there is nothing to choose", () => {
    renderPanel();
    expect(screen.queryByRole("combobox", { name: /project/i })).not.toBeInTheDocument();
  });

  it("puts the project picker on the search input's row, not on a row of its own", () => {
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
      ],
    });

    const box = screen.getByRole("searchbox", { name: /search recordings/i });
    const picker = screen.getByRole("combobox", { name: /project/i });

    // Siblings in one container, the input first: one flex row.
    expect(picker.parentElement).toBe(box.parentElement);
    expect(box.nextElementSibling).toBe(picker);
  });

  it("hides the project picker while a search is active, as before", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
      ],
    });

    await user.type(screen.getByRole("searchbox", { name: /search recordings/i }), "sync");

    expect(screen.queryByRole("combobox", { name: /project/i })).not.toBeInTheDocument();
  });

  it("opens a recording by id when its row is clicked", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderPanel({
      entries: [buildEntry({ id: "v-42", meeting_name: "260812 - Security issue" })],
      onOpen,
    });

    await user.click(screen.getByText("Security issue"));

    expect(onOpen).toHaveBeenCalledWith("v-42");
  });

  it("loads the service log only once its tab is opened", async () => {
    const onLoadServiceLog = vi.fn().mockResolvedValue([]);
    const user = userEvent.setup();
    renderPanel({ onLoadServiceLog });

    expect(onLoadServiceLog).not.toHaveBeenCalled();

    await user.click(screen.getByRole("tab", { name: /service log/i }));

    expect(onLoadServiceLog).toHaveBeenCalled();
  });

  it("still mounts with an empty vault, so the service log stays reachable", () => {
    renderPanel({ entries: [] });
    expect(screen.getByRole("region", { name: /recordings/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /service log/i })).toBeInTheDocument();
  });

  it("an active search replaces the list, and clearing it restores the list", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [buildEntry({ id: "a" }), buildEntry({ id: "b", meeting_name: "260813 - Other" })],
      onSearch: () => Promise.resolve([]),
    });
    expect(screen.getByRole("table")).toBeInTheDocument();

    const box = screen.getByRole("searchbox", { name: /search recordings/i });
    await user.type(box, "дедлайн");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await user.clear(box);
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});
