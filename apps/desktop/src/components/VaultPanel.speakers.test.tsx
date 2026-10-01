import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VaultPanel, type VaultTab } from "./VaultPanel";
import { DEFAULT_VAULT_SORT } from "../lib/vaultSort";
import type { VaultMeetingView } from "../types";

const ENTRY: VaultMeetingView = {
  id: "a",
  project: "ELS",
  meeting_name: "260812 - Security issue",
  meeting_dir: "D:\\Meetings\\ELS\\260812 - Security issue",
  has_source: true,
  has_transcript: true,
};

function Panel(props: Partial<React.ComponentProps<typeof VaultPanel>>) {
  return (
    <VaultPanel
      entries={[ENTRY]}
      jobs={[]}
      filter=""
      onFilterChange={() => {}}
      sort={DEFAULT_VAULT_SORT}
      onSortChange={() => {}}
      search=""
      onSearchChange={() => {}}
      onSearch={() => Promise.resolve([])}
      onOpen={() => {}}
      chatTab={<div data-testid="chat-slot" />}
      speakersTab={<div data-testid="speakers-slot" />}
      onRevealJob={() => {}}
      onCancelJob={() => {}}
      onLoadServiceLog={() => Promise.resolve([])}
      {...props}
    />
  );
}

describe("VaultPanel Speakers tab", () => {
  it("sits right after Recordings", () => {
    render(<Panel />);

    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(tabs[0]).toMatch(/^Recordings/);
    expect(tabs.slice(1)).toEqual(["Speakers", "Chat", "Service log"]);
  });

  it("mounts the speakers content only once its tab is opened", async () => {
    const user = userEvent.setup();
    render(<Panel />);
    expect(screen.queryByTestId("speakers-slot")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Speakers" }));

    expect(screen.getByTestId("speakers-slot")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Speakers" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("is not offered when no speakers content is composed in", () => {
    render(<Panel speakersTab={undefined} />);

    expect(screen.queryByRole("tab", { name: "Speakers" })).not.toBeInTheDocument();
  });

  it("opens on the tab its owner names, and reports every switch", async () => {
    const user = userEvent.setup();
    const onTabChange = vi.fn();
    function Owned() {
      const [tab, setTab] = useState<VaultTab>("speakers");
      return (
        <Panel
          tab={tab}
          onTabChange={(next) => {
            setTab(next);
            onTabChange(next);
          }}
        />
      );
    }
    render(<Owned />);
    expect(screen.getByTestId("speakers-slot")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Chat" }));

    expect(onTabChange).toHaveBeenCalledWith("chat");
    expect(screen.getByTestId("chat-slot")).toBeInTheDocument();
  });
});
