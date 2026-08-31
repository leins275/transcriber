import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatWireMessage, SearchResultView } from "../types";

/** One rendered chat turn; sources hang off the assistant turn they cite. */
export type ChatDisplayMessage = {
  role: "user" | "assistant";
  content: string;
  sources?: SearchResultView[];
};

/**
 * The project chat's state: history, the in-flight stream, send/stop.
 *
 * History is React state only, reset when `project` changes and never
 * persisted -- a chat transcript is a scratch conversation, not a vault
 * artifact. A turn counter guards against late events from a superseded
 * turn (the Rust side auto-cancels the old stream when a new one starts,
 * but a few of its events may already be in flight).
 */
export function useChat(project: string | null) {
  const [messages, setMessages] = useState<ChatDisplayMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const turnRef = useRef(0);

  // A different project (or none) is a different conversation.
  useEffect(() => {
    turnRef.current += 1;
    setMessages([]);
    setStreaming(false);
    setError(null);
    return () => {
      // Leaving the page stops whatever is still generating.
      turnRef.current += 1;
      void api.cancelChat();
    };
  }, [project]);

  const send = useCallback(
    (text: string) => {
      const question = text.trim();
      if (!question || project === null) return;
      const turn = ++turnRef.current;
      setError(null);
      setStreaming(true);

      const wire: ChatWireMessage[] = [
        ...messages.map((message) => ({ role: message.role, content: message.content })),
        { role: "user" as const, content: question },
      ];
      setMessages((prev) => [
        ...prev,
        { role: "user", content: question },
        { role: "assistant", content: "" },
      ]);

      const appendToAnswer = (updater: (last: ChatDisplayMessage) => ChatDisplayMessage) => {
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const next = prev.slice();
          next[next.length - 1] = updater(next[next.length - 1]);
          return next;
        });
      };

      api
        .chatStream(wire, project, (event) => {
          if (turnRef.current !== turn) return; // a superseded turn's stragglers
          if (event.type === "delta") {
            appendToAnswer((last) => ({ ...last, content: last.content + event.text }));
          } else if (event.type === "sources") {
            appendToAnswer((last) => ({ ...last, sources: event.sources }));
          } else if (event.type === "done") {
            if (event.finish_reason === "length") {
              setError("The answer hit the output limit and may be incomplete.");
            }
            setStreaming(false);
          } else {
            setError(event.message);
            setStreaming(false);
          }
        })
        .then(() => {
          if (turnRef.current === turn) setStreaming(false);
        })
        .catch((caught: unknown) => {
          if (turnRef.current !== turn) return;
          const message =
            typeof caught === "object" && caught !== null && "message" in caught
              ? String((caught as { message: unknown }).message)
              : String(caught);
          setError(message);
          setStreaming(false);
        });
    },
    [project, messages],
  );

  const stop = useCallback(() => {
    void api.cancelChat();
    setStreaming(false);
  }, []);

  return { messages, streaming, error, send, stop };
}
