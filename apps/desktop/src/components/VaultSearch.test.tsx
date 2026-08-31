import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { VaultSearch } from "./VaultSearch";
import type { SearchResultView } from "../types";

function buildResult(overrides: Partial<SearchResultView> = {}): SearchResultView {
  return {
    entry_id: "v-1",
    kind: "transcript",
    meeting_name: "260831 - Weekly sync",
    project: "ACME",
    snippet: "…обсуждали дедлайн по проекту…",
    score: 0.5,
    start_sec: 12,
    timestamp: "0:12",
    ...overrides,
  };
}

/** Plays App's role: the query is controlled state in production. */
function ControlledSearch(props: {
  onSearch: (query: string) => Promise<SearchResultView[]>;
  onOpen?: (entryId: string) => void;
}) {
  const [query, setQuery] = useState("");
  return (
    <VaultSearch
      query={query}
      onQueryChange={setQuery}
      onSearch={props.onSearch}
      onOpen={props.onOpen ?? (() => {})}
    />
  );
}

/** Instant keystrokes: every character lands well inside one debounce
 * window, so the burst must collapse into exactly one search. */
function fastUser() {
  return userEvent.setup({ delay: null });
}

describe("VaultSearch", () => {
  it("debounces a typing burst into one search for the final query", async () => {
    const onSearch = vi.fn().mockResolvedValue([buildResult()]);
    render(<ControlledSearch onSearch={onSearch} />);

    await fastUser().type(screen.getByRole("searchbox", { name: /search recordings/i }), "дедлайн");
    // Still inside the debounce window: nothing fired yet.
    expect(onSearch).not.toHaveBeenCalled();

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1), { timeout: 2000 });
    expect(onSearch).toHaveBeenCalledWith("дедлайн");
  });

  it("renders hits with kind pills and opens one by entry id", async () => {
    const onOpen = vi.fn();
    const onSearch = vi
      .fn()
      .mockResolvedValue([buildResult(), buildResult({ kind: "note", entry_id: "v-2" })]);
    render(<ControlledSearch onSearch={onSearch} onOpen={onOpen} />);

    await fastUser().type(screen.getByRole("searchbox", { name: /search recordings/i }), "дедлайн");

    expect(await screen.findByText("Transcript", undefined, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.getByText("Note")).toBeInTheDocument();

    await fastUser().click(screen.getAllByRole("button")[0]);
    expect(onOpen).toHaveBeenCalledWith("v-1");
  });

  it("shows the empty state for a query with no matches", async () => {
    const onSearch = vi.fn().mockResolvedValue([]);
    render(<ControlledSearch onSearch={onSearch} />);

    await fastUser().type(screen.getByRole("searchbox", { name: /search recordings/i }), "nothing");

    expect(
      await screen.findByText(/no matches/i, undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
  });

  it("surfaces a search failure as an alert", async () => {
    const onSearch = vi.fn().mockRejectedValue({ kind: "service", message: "service exploded" });
    render(<ControlledSearch onSearch={onSearch} />);

    await fastUser().type(screen.getByRole("searchbox", { name: /search recordings/i }), "boom");

    expect(await screen.findByRole("alert", undefined, { timeout: 2000 })).toHaveTextContent(
      /service exploded/i,
    );
  });

  it("a one-character query searches nothing and renders no results block", async () => {
    const onSearch = vi.fn();
    render(<ControlledSearch onSearch={onSearch} />);

    await fastUser().type(screen.getByRole("searchbox", { name: /search recordings/i }), "x");
    // Give the debounce window a chance to (wrongly) fire.
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onSearch).not.toHaveBeenCalled();
    expect(screen.queryByText(/no matches/i)).not.toBeInTheDocument();
  });
});
