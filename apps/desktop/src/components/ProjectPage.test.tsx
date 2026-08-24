import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectPage } from "./ProjectPage";
import type { VaultMeetingView } from "../types";

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

function renderPage(props: Partial<React.ComponentProps<typeof ProjectPage>> = {}) {
  const defaults = {
    project: "ELS",
    entries: [buildEntry()],
    onBack: () => {},
    onOpen: () => {},
    onReveal: () => {},
  };
  return render(<ProjectPage {...defaults} {...props} />);
}

describe("ProjectPage", () => {
  it("keeps the breadcrumb back to the library, with the project as the current crumb", async () => {
    const onBack = vi.fn();
    const user = userEvent.setup();
    renderPage({ project: "GIS", onBack });

    const crumbs = screen.getByRole("navigation", { name: /breadcrumb/i });
    expect(within(crumbs).getByText("GIS")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "GIS" })).toBeInTheDocument();

    await user.click(within(crumbs).getByRole("button", { name: /recordings/i }));

    expect(onBack).toHaveBeenCalled();
  });

  it("lists the project's recordings, and says how many there are", () => {
    renderPage({
      entries: [
        buildEntry({ id: "a", meeting_name: "260812 - Security issue" }),
        buildEntry({ id: "b", meeting_name: "260811 - Weekly sync" }),
      ],
    });

    expect(screen.getByText("Security issue")).toBeInTheDocument();
    expect(screen.getByText("Weekly sync")).toBeInTheDocument();
    expect(screen.getByText(/2 recordings/i)).toBeInTheDocument();
  });

  it("shows recordings and nothing else: no tabs over artifacts or reports", () => {
    renderPage();

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /action items/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /facts/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /reports/i })).not.toBeInTheDocument();
  });

  it("offers no project-essence export — that surface is gone", () => {
    renderPage();

    expect(screen.queryByRole("button", { name: /essence/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/export project essence/i)).not.toBeInTheDocument();
  });

  it("opens a recording by id, the same flow as the library list", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    renderPage({ entries: [buildEntry({ id: "v-42" })], onOpen });

    await user.click(screen.getByRole("button", { name: /^transcript$/i }));

    expect(onOpen).toHaveBeenCalledWith("v-42");
  });

  it("reveals a recording by id", async () => {
    const onReveal = vi.fn();
    const user = userEvent.setup();
    renderPage({ entries: [buildEntry({ id: "v-42" })], onReveal });

    await user.click(screen.getByRole("button", { name: /^reveal$/i }));

    expect(onReveal).toHaveBeenCalledWith("v-42");
  });

  it("explains an empty project instead of rendering an empty page", () => {
    renderPage({ entries: [] });

    expect(screen.getByRole("region", { name: /project/i })).toBeInTheDocument();
    expect(screen.getByText(/no recordings/i)).toBeInTheDocument();
  });
});
