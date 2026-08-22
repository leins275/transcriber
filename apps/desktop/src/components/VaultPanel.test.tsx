import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultPanel } from "./VaultPanel";
import type { LedgerJobView, TranscriptView, VaultMeetingView } from "../types";

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

const transcript: TranscriptView = {
  entry_id: "a",
  meeting_name: "260812 - Security issue",
  language: null,
  created_at: null,
  duration_sec: null,
  model: null,
  device: null,
  text: "",
  segments: [],
};

function renderPanel(props: Partial<React.ComponentProps<typeof VaultPanel>> = {}) {
  const defaults = {
    entries: [buildEntry()],
    onReveal: () => {},
    onReadTranscript: () => Promise.resolve(transcript),
    onUpdate: () => Promise.resolve(),
    onDelete: () => Promise.resolve(),
    onLoadServiceLog: () => Promise.resolve<LedgerJobView[]>([]),
  };
  return render(<VaultPanel {...defaults} {...props} />);
}

describe("VaultPanel", () => {
  it("exposes a Vault region and the entry count", () => {
    renderPanel({ entries: [buildEntry({ id: "a" }), buildEntry({ id: "b" })] });
    expect(screen.getByRole("region", { name: /vault/i })).toBeInTheDocument();
    expect(screen.getByText(/2 in vault/i)).toBeInTheDocument();
  });

  it("offers Projects, Unsorted and Service log as tabs", () => {
    renderPanel();
    expect(screen.getByRole("tab", { name: /projects/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /unsorted/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /service log/i })).toBeInTheDocument();
  });

  it("opens on Projects and shows only that project's recordings", () => {
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
        buildEntry({ id: "c", project: null, meeting_name: "260810 - loose file" }),
      ],
    });

    expect(screen.getByRole("tab", { name: /projects/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("260812 - Els meeting")).toBeInTheDocument();
    expect(screen.queryByText("260811 - Gis meeting")).not.toBeInTheDocument();
    expect(screen.queryByText("260810 - loose file")).not.toBeInTheDocument();
  });

  it("switches project through the selector", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "b", project: "GIS", meeting_name: "260811 - Gis meeting" }),
      ],
    });

    await user.selectOptions(screen.getByRole("combobox", { name: /project/i }), "GIS");

    expect(screen.getByText("260811 - Gis meeting")).toBeInTheDocument();
    expect(screen.queryByText("260812 - Els meeting")).not.toBeInTheDocument();
  });

  it("shows unsorted recordings only on the Unsorted tab, with its own count", async () => {
    const user = userEvent.setup();
    renderPanel({
      entries: [
        buildEntry({ id: "a", project: "ELS", meeting_name: "260812 - Els meeting" }),
        buildEntry({ id: "c", project: null, meeting_name: "260810 - loose file" }),
      ],
    });

    const unsortedTab = screen.getByRole("tab", { name: /unsorted/i });
    expect(unsortedTab).toHaveTextContent("1");

    await user.click(unsortedTab);

    expect(screen.getByText("260810 - loose file")).toBeInTheDocument();
    expect(screen.queryByText("260812 - Els meeting")).not.toBeInTheDocument();
  });

  it("teaches the naming convention when the vault holds no projects yet", () => {
    renderPanel({ entries: [buildEntry({ id: "c", project: null })] });
    expect(screen.getByText(/no projects yet/i)).toBeInTheDocument();
  });

  it("says so plainly when nothing is unsorted", async () => {
    const user = userEvent.setup();
    renderPanel({ entries: [buildEntry({ project: "ELS" })] });

    await user.click(screen.getByRole("tab", { name: /unsorted/i }));

    expect(screen.getByText(/nothing unsorted/i)).toBeInTheDocument();
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
    expect(screen.getByRole("region", { name: /vault/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /service log/i })).toBeInTheDocument();
  });
});
