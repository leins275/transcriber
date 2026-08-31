import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ChatEventView } from "../types";

// The api module is the designed test seam: driving a real Tauri `Channel`
// through `mockIPC` is fragile, and every other collaborator is pure state.
vi.mock("../api", () => ({
  api: {
    chatStream: vi.fn(),
    cancelChat: vi.fn().mockResolvedValue(undefined),
    listChats: vi.fn().mockResolvedValue([]),
    readChat: vi.fn(),
    saveChat: vi.fn(),
    renameChat: vi.fn().mockResolvedValue(undefined),
    deleteChat: vi.fn().mockResolvedValue(undefined),
  },
}));

import { api } from "../api";
import { useChat } from "./useChat";

const chatStream = vi.mocked(api.chatStream);
const cancelChat = vi.mocked(api.cancelChat);
const listChats = vi.mocked(api.listChats);
const readChat = vi.mocked(api.readChat);
const saveChat = vi.mocked(api.saveChat);

function scriptStream(events: ChatEventView[]) {
  let release: () => void = () => {};
  const done = new Promise<void>((resolve) => {
    release = resolve;
  });
  chatStream.mockImplementation(async (_messages, _project, onEvent) => {
    for (const event of events) onEvent(event);
    await done;
  });
  return release;
}

describe("useChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listChats.mockResolvedValue([]);
    saveChat.mockResolvedValue({
      id: "chat-1",
      title: "saved",
      updated_at_ms: 1,
      question_count: 1,
    });
  });

  it("accumulates deltas into the assistant turn and attaches sources", async () => {
    const release = scriptStream([
      { type: "sources", sources: [] },
      { type: "delta", text: "Hel" },
      { type: "delta", text: "lo" },
      { type: "done", finish_reason: "stop" },
    ]);
    const { result } = renderHook(() => useChat("ELS"));

    await act(async () => {
      result.current.send("вопрос");
      release();
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({ role: "user", content: "вопрос" });
    expect(result.current.messages[1]).toMatchObject({ role: "assistant", content: "Hello" });
    expect(result.current.streaming).toBe(false);
    expect(result.current.error).toBeNull();
    expect(chatStream).toHaveBeenCalledWith(
      [{ role: "user", content: "вопрос" }],
      "ELS",
      expect.any(Function),
    );
  });

  it("persists the conversation after a completed turn, titled by the question", async () => {
    const release = scriptStream([
      { type: "delta", text: "answer" },
      { type: "done", finish_reason: "stop" },
    ]);
    const { result } = renderHook(() => useChat("ELS"));

    await act(async () => {
      result.current.send("когда дедлайн?");
      release();
    });
    await waitFor(() => expect(saveChat).toHaveBeenCalled());

    const [project, conversation] = saveChat.mock.calls[0];
    expect(project).toBe("ELS");
    expect(conversation.id).toBeNull();
    expect(conversation.title).toBe("когда дедлайн?");
    expect(conversation.messages).toHaveLength(2);
    await waitFor(() => expect(result.current.conversationId).toBe("chat-1"));
  });

  it("opens a saved conversation and replaces the transcript", async () => {
    readChat.mockResolvedValue({
      id: "chat-9",
      title: "old talk",
      messages: [
        { role: "user", content: "q", sources: [] },
        {
          role: "assistant",
          content: "a",
          sources: [
            {
              entry_id: "v-1",
              kind: "transcript",
              meeting_name: "260831 - Sync",
              timestamp: "0:12",
              start_sec: 12,
            },
          ],
        },
      ],
    });
    const { result } = renderHook(() => useChat("ELS"));

    await act(async () => {
      result.current.openConversation("chat-9");
    });

    await waitFor(() => expect(result.current.conversationId).toBe("chat-9"));
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[1].sources?.[0].meeting_name).toBe("260831 - Sync");
  });

  it("a new conversation clears the transcript but keeps the history", async () => {
    listChats.mockResolvedValue([
      { id: "chat-1", title: "t", updated_at_ms: 1, question_count: 1 },
    ]);
    const release = scriptStream([{ type: "done", finish_reason: "stop" }]);
    const { result } = renderHook(() => useChat("ELS"));
    await waitFor(() => expect(result.current.history).toHaveLength(1));
    await act(async () => {
      result.current.send("q");
      release();
    });

    act(() => {
      result.current.newConversation();
    });

    expect(result.current.messages).toHaveLength(0);
    expect(result.current.conversationId).toBeNull();
    expect(result.current.history).toHaveLength(1);
  });

  it("an error event lands in error state and stops streaming", async () => {
    const release = scriptStream([{ type: "error", message: "model exploded" }]);
    const { result } = renderHook(() => useChat("ELS"));

    await act(async () => {
      result.current.send("q");
      release();
    });

    expect(result.current.error).toBe("model exploded");
    expect(result.current.streaming).toBe(false);
  });

  it("switching projects resets the conversation and cancels the stream", async () => {
    const release = scriptStream([{ type: "delta", text: "partial" }]);
    const { result, rerender } = renderHook(({ project }) => useChat(project), {
      initialProps: { project: "ELS" as string | null },
    });

    await act(async () => {
      result.current.send("q");
      release();
    });
    expect(result.current.messages).not.toHaveLength(0);

    rerender({ project: "GIS" });

    expect(result.current.messages).toHaveLength(0);
    expect(cancelChat).toHaveBeenCalled();
  });

  it("send is a no-op without a project", async () => {
    const { result } = renderHook(() => useChat(null));

    await act(async () => {
      result.current.send("q");
    });

    expect(chatStream).not.toHaveBeenCalled();
    expect(result.current.messages).toHaveLength(0);
  });
});
