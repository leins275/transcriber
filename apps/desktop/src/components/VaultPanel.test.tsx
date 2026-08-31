import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultPanel } from "./VaultPanel";
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

/** The filter/grouped pair is controlled (owned by App in production, so it
 * survives the panel's unmount); this harness plays App's role for tests
 * that drive the picker and the group toggle. */
function ControlledPanel(props: Partial<React.ComponentProps<typeof VaultPanel>>) {
  const [filter, setFilter] = useState(props.filter ?? "");
  const [grouped, setGrouped] = useState(props.grouped ?? false);
  const [search, setSearch] = useState(props.search ?? "");
  const defaults = {
    entries: [buildEntry()],
    jobs: [] as JobSnapshot[],
    onOpen: () => {},
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
      grouped={grouped}
      onGroupedChange={setGrouped}
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

  it("renders one flat list — no group headings, no project pages", () => {
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

  it("groups under project headings when the toggle is on", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
        buildEntry({ id: "c", project: null, meeting_name: "260810 - loose file" }),
      ],
    });

    await user.click(screen.getByRole("checkbox", { name: /group by project/i }));

    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "ELS",
      "GIS",
      "Unsorted",
    ]);
    // Grouped, not filtered: everything is still on screen.
    expect(screen.getByText("Els meeting")).toBeInTheDocument();
    expect(screen.getByText("Gis meeting")).toBeInTheDocument();
    expect(screen.getByText("loose file")).toBeInTheDocument();
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

  it("offers no filter row for a single project — there is nothing to choose", () => {
    renderPanel();
    expect(screen.queryByRole("combobox", { name: /project/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /group by project/i })).not.toBeInTheDocument();
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
    expect(screen.getByRole("list")).toBeInTheDocument();

    const box = screen.getByRole("searchbox", { name: /search recordings/i });
    await user.type(box, "дедлайн");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();

    await user.clear(box);
    expect(screen.getByRole("list")).toBeInTheDocument();
  });
});
