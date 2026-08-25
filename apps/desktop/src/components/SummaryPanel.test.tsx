import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const noGenerate = () => {};

describe("SummaryPanel", () => {
  it("renders a summary that exists", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary({ markdown: "# Decisions\n\nShip it." }))}
        onGenerate={noGenerate}
      />,
    );

    expect(await screen.findByText(/Ship it\./)).toBeInTheDocument();
  });

  it("names the exact path a summary would live at when there is none", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary())}
        onGenerate={noGenerate}
      />,
    );

    expect(await screen.findByText(/no summary for this meeting yet/i)).toBeInTheDocument();
    expect(screen.getByText(/summary\.md/)).toBeInTheDocument();
  });

  it("generates from the empty state's own button", async () => {
    const onGenerate = vi.fn();
    const user = userEvent.setup();
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary())}
        onGenerate={onGenerate}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Generate summary" }));

    expect(onGenerate).toHaveBeenCalled();
  });

  it("renders the Generate button busy while a summarize job runs", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary())}
        onGenerate={noGenerate}
        busy
      />,
    );

    expect(await screen.findByRole("button", { name: "Summarizing…" })).toBeDisabled();
  });

  it("reports its content up for the page's per-tab Copy", async () => {
    const onContentChange = vi.fn();
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.resolve(buildSummary({ markdown: "# Decisions\n\nShip it." }))}
        onGenerate={noGenerate}
        onContentChange={onContentChange}
      />,
    );
    await screen.findByText(/Ship it\./);

    expect(onContentChange).toHaveBeenLastCalledWith("# Decisions\n\nShip it.");
  });

  it("reloads when the reload token bumps (a summarize job finished)", async () => {
    const onLoad = vi.fn().mockResolvedValue(buildSummary());
    const { rerender } = render(
      <SummaryPanel entryId="v-1" onLoad={onLoad} onGenerate={noGenerate} reloadToken={0} />,
    );
    await screen.findByText(/no summary/i);

    rerender(
      <SummaryPanel entryId="v-1" onLoad={onLoad} onGenerate={noGenerate} reloadToken={1} />,
    );
    await screen.findByText(/no summary/i);

    expect(onLoad).toHaveBeenCalledTimes(2);
  });

  it("surfaces a read failure instead of pretending there is no summary", async () => {
    render(
      <SummaryPanel
        entryId="v-1"
        onLoad={() => Promise.reject({ kind: "io", message: "permission denied" })}
        onGenerate={noGenerate}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/permission denied/i);
    expect(screen.queryByText(/no summary for this meeting yet/i)).not.toBeInTheDocument();
  });

  it("reloads when a different recording is opened", async () => {
    const onLoad = vi.fn().mockResolvedValue(buildSummary());
    const { rerender } = render(
      <SummaryPanel entryId="v-1" onLoad={onLoad} onGenerate={noGenerate} />,
    );
    await screen.findByText(/no summary/i);

    rerender(<SummaryPanel entryId="v-2" onLoad={onLoad} onGenerate={noGenerate} />);
    await screen.findByText(/no summary/i);

    expect(onLoad).toHaveBeenCalledTimes(2);
    expect(onLoad).toHaveBeenLastCalledWith("v-2");
  });
});
