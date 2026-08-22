import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SummaryPanel } from "./SummaryPanel";
import type { SummaryView } from "../types";

function buildSummary(overrides: Partial<SummaryView> = {}): SummaryView {
  return {
    entry_id: "v-1",
    path: "D:\\Meetings\\ELS\\260812 - Security issue\\summary.md",
    markdown: null,
    ...overrides,
  };
}

describe("SummaryPanel", () => {
  it("renders a summary that exists", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary({ markdown: "# Decisions\n\nShip it." }))}
      />,
    );

    expect(await screen.findByText(/Ship it\./)).toBeInTheDocument();
  });

  it("names the exact path a summary would live at when there is none", async () => {
    render(<SummaryPanel entryId="v-1" onLoad={() => Promise.resolve(buildSummary())} />);

    expect(await screen.findByText(/no summary for this meeting yet/i)).toBeInTheDocument();
    expect(screen.getByText(/summary\.md/)).toBeInTheDocument();
  });

  it("says plainly that nothing generates summaries yet", async () => {
    render(<SummaryPanel entryId="v-1" onLoad={() => Promise.resolve(buildSummary())} />);

    // An honest empty state, not "coming soon": the operator can act on it.
    expect(await screen.findByText(/needs a language model/i)).toBeInTheDocument();
  });

  it("surfaces a read failure instead of pretending there is no summary", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.reject({ kind: "io", message: "permission denied" })}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/permission denied/i);
    expect(screen.queryByText(/no summary for this meeting yet/i)).not.toBeInTheDocument();
  });

  it("reloads when a different recording is opened", async () => {
    const onLoad = vi.fn().mockResolvedValue(buildSummary());
    const { rerender } = render(<SummaryPanel entryId="v-1" onLoad={onLoad} />);
    await screen.findByText(/no summary/i);

    rerender(<SummaryPanel entryId="v-2" onLoad={onLoad} />);
    await screen.findByText(/no summary/i);

    expect(onLoad).toHaveBeenCalledTimes(2);
    expect(onLoad).toHaveBeenLastCalledWith("v-2");
  });
});
