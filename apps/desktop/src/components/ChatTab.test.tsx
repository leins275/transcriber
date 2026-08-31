import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatTab } from "./ChatTab";
import type { ChatDisplayMessage } from "../state/useChat";
import type { IndexStatusView } from "../types";

function buildIndex(overrides: Partial<IndexStatusView> = {}): IndexStatusView {
  return {
    project: "GIS",
    updated_at_sec: 1756640000,
    indexing: false,
    progress: null,
    indexed_count: 4,
    total_count: 5,
    meetings: [
      { name: "260827 - Team sync", state: "indexed", chunks: 142 },
      { name: "260810 - First meeting", state: "no_transcript", chunks: 0 },
      { name: "260825 - Demo 3", state: "pending", chunks: 0 },
    ],
    ...overrides,
  };
}

function renderTab(props: Partial<React.ComponentProps<typeof ChatTab>> = {}) {
  const defaults = {
    projects: ["GIS", "ELS"],
    project: "GIS",
    onProjectChange: () => {},
    messages: [] as ChatDisplayMessage[],
    streaming: false,
    error: null,
    history: [],
    conversationId: null,
    onSend: () => {},
    onStop: () => {},
    onNewConversation: () => {},
    onOpenConversation: () => {},
    onRenameConversation: () => {},
    onDeleteConversation: () => {},
    onLoadIndex: () => Promise.resolve(buildIndex()),
    onReindex: () => Promise.resolve(),
    onOpenSource: () => {},
    onAddToNotes: () => Promise.resolve(),
  };
  return render(<ChatTab {...defaults} {...props} />);
}

describe("ChatTab", () => {
  it("shows the empty state with suggestions, and a suggestion sends itself", async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    renderTab({ onSend });

    expect(screen.getByText("Ask about GIS")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /release deadline/i }));

    expect(onSend).toHaveBeenCalledWith("What was decided about the release deadline?");
  });

  it("switches projects through the chip", async () => {
    const onProjectChange = vi.fn();
    const user = userEvent.setup();
    renderTab({ onProjectChange });

    await user.selectOptions(screen.getByRole("combobox", { name: /chat project/i }), "ELS");

    expect(onProjectChange).toHaveBeenCalledWith("ELS");
  });

  it("summarizes the index and expands the per-meeting panel", async () => {
    const user = userEvent.setup();
    renderTab();

    expect(await screen.findByText(/index: 4 of 5 meetings/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show" }));

    const panel = screen.getByLabelText("GIS index");
    expect(within(panel).getByText(/142 fragments/)).toBeInTheDocument();
    expect(within(panel).getByText(/no transcript — outside the index/)).toBeInTheDocument();
    expect(within(panel).getByText(/awaiting indexing/)).toBeInTheDocument();
  });

  it("the chip's Refresh queues a re-index", async () => {
    const onReindex = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderTab({ onReindex });

    await screen.findByText(/index: 4 of 5/i);
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(onReindex).toHaveBeenCalled());
  });

  it("renders an answer with numbered sources; a live source opens, a dead one does not", async () => {
    const onOpenSource = vi.fn();
    const user = userEvent.setup();
    renderTab({
      onOpenSource,
      messages: [
        { role: "user", content: "когда дедлайн?" },
        {
          role: "assistant",
          content: "15 сентября [S1].",
          sources: [
            {
              entry_id: "v-1",
              kind: "transcript",
              meeting_name: "260824 - Third client meeting",
              timestamp: "41:12",
              start_sec: 2472,
            },
            {
              entry_id: null,
              kind: "summary",
              meeting_name: "260825 - Deleted meeting",
              timestamp: null,
              start_sec: null,
            },
          ],
        },
      ],
    });

    expect(screen.getByText(/15 сентября/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "260824 - Third client meeting" }));
    expect(onOpenSource).toHaveBeenCalledWith("v-1");
    expect(
      screen.queryByRole("button", { name: "260825 - Deleted meeting" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("260825 - Deleted meeting")).toBeInTheDocument();
  });

  it("adds the answer to the first live source's meeting notes", async () => {
    const onAddToNotes = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderTab({
      onAddToNotes,
      messages: [
        { role: "user", content: "q" },
        {
          role: "assistant",
          content: "The answer.",
          sources: [
            {
              entry_id: "v-7",
              kind: "transcript",
              meeting_name: "260824 - Sync",
              timestamp: null,
              start_sec: null,
            },
          ],
        },
      ],
    });

    await user.click(screen.getByRole("button", { name: /add to meeting notes/i }));

    expect(onAddToNotes).toHaveBeenCalledWith("v-7", expect.stringContaining("The answer."));
  });

  it("opens a saved conversation from the history row", async () => {
    const onOpenConversation = vi.fn();
    const user = userEvent.setup();
    renderTab({
      history: [
        {
          id: "chat-1",
          title: "Deadline and design",
          updated_at_ms: 1756640000000,
          question_count: 4,
        },
      ],
      onOpenConversation,
    });

    await user.click(screen.getByRole("button", { name: /history/i }));
    await user.click(screen.getByRole("button", { name: /^deadline and design/i }));

    expect(onOpenConversation).toHaveBeenCalledWith("chat-1");
  });

  it("sends the draft and shows Stop while streaming", async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    const { rerender } = renderTab({ onSend });

    await user.type(screen.getByRole("textbox", { name: /ask about gis/i }), "вопрос{Enter}");
    expect(onSend).toHaveBeenCalledWith("вопрос");

    rerender(
      <ChatTab
        projects={["GIS"]}
        project="GIS"
        onProjectChange={() => {}}
        messages={[
          { role: "user", content: "вопрос" },
          { role: "assistant", content: "печатаю" },
        ]}
        streaming
        error={null}
        history={[]}
        conversationId={null}
        onSend={() => {}}
        onStop={() => {}}
        onNewConversation={() => {}}
        onOpenConversation={() => {}}
        onRenameConversation={() => {}}
        onDeleteConversation={() => {}}
        onLoadIndex={() => Promise.resolve(buildIndex())}
        onReindex={() => Promise.resolve()}
        onOpenSource={() => {}}
        onAddToNotes={() => Promise.resolve()}
      />,
    );
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  });

  it("a disabled reason disables the composer and explains itself", () => {
    renderTab({ disabledReason: "Chat needs the local language model — download it in Settings." });

    expect(screen.getByRole("textbox", { name: /ask about gis/i })).toBeDisabled();
    expect(screen.getByText(/needs the local language model/i)).toBeInTheDocument();
  });

  it("explains an empty vault instead of a dead tab", () => {
    renderTab({ projects: [], project: null });

    expect(screen.getByText(/chat needs at least one project/i)).toBeInTheDocument();
  });
});
