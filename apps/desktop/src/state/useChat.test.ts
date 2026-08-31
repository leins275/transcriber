import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ChatEventView } from "../types";

// The api module is the designed test seam: driving a real Tauri `Channel`
// through `mockIPC` is fragile, and every other collaborator is pure state.
vi.mock("../api", () => ({
  api: {
    chatStream: vi.fn(),
    cancelChat: vi.fn().mockResolvedValue(undefined),
  },
}));

import { api } from "../api";
import { useChat } from "./useChat";

const chatStream = vi.mocked(api.chatStream);
const cancelChat = vi.mocked(api.cancelChat);

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

  it("a length stop surfaces as a truncation warning", async () => {
    const release = scriptStream([
      { type: "delta", text: "cut" },
      { type: "done", finish_reason: "length" },
    ]);
    const { result } = renderHook(() => useChat("ELS"));

    await act(async () => {
      result.current.send("q");
      release();
    });

    expect(result.current.error).toMatch(/output limit/i);
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

  it("unmounting cancels whatever is still generating", async () => {
    scriptStream([]);
    const { result, unmount } = renderHook(() => useChat("ELS"));
    await act(async () => {
      result.current.send("q");
    });

    unmount();

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
