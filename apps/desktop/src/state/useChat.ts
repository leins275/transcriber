import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatSourceView, ChatStoredMessage, ChatSummaryView, ChatWireMessage } from "../types";

/** One rendered chat turn; sources hang off the assistant turn they cite. */
export type ChatDisplayMessage = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSourceView[];
};

/** A conversation title from its first question (the redesign's rule);
 * renameable afterwards. */
function titleFrom(question: string): string {
  const flat = question.trim().replace(/\s+/g, " ");
  return flat.length <= 60 ? flat : `${flat.slice(0, 57)}…`;
}

/**
 * The chat tab's state: the open conversation, the in-flight stream, and
 * the project's saved history.
 *
 * Conversations persist in the vault (`<PROJECT>/chats/`) through the
 * chats commands: every completed turn saves the whole conversation, and
 * the history list refreshes from disk. A turn counter guards against late
 * events from a superseded turn (the Rust side auto-cancels the old stream
 * when a new one starts, but a few of its events may already be in flight).
 */
export function useChat(project: string | null) {
  const [messages, setMessages] = useState<ChatDisplayMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [history, setHistory] = useState<ChatSummaryView[]>([]);
  const turnRef = useRef(0);

  const refreshHistory = useCallback(() => {
    if (project === null) {
      setHistory([]);
      return;
    }
    api
      .listChats(project)
      .then((listed) => setHistory(listed ?? []))
      .catch(() => {
        // No chats dir yet / older backend: an empty history, not an error.
        setHistory([]);
      });
  }, [project]);

  // A different project (or none) is a different conversation set.
  useEffect(() => {
    turnRef.current += 1;
    setMessages([]);
    setStreaming(false);
    setError(null);
    setConversationId(null);
    setTitle(null);
    refreshHistory();
    return () => {
      // Leaving the tab stops whatever is still generating.
      turnRef.current += 1;
      void api.cancelChat();
    };
  }, [project, refreshHistory]);

  const persist = useCallback(
    (conversation: ChatDisplayMessage[]) => {
      if (project === null || conversation.length === 0) return;
      const firstQuestion = conversation.find((message) => message.role === "user");
      const resolvedTitle = title ?? titleFrom(firstQuestion?.content ?? "Conversation");
      const stored: ChatStoredMessage[] = conversation.map((message) => ({
        role: message.role,
        content: message.content,
        sources: message.sources ?? [],
      }));
      api
        .saveChat(project, { id: conversationId, title: resolvedTitle, messages: stored })
        .then((summary) => {
          setConversationId(summary.id);
          setTitle(summary.title);
          refreshHistory();
        })
        .catch(() => {
          // Persistence is best-effort: the conversation stays on screen.
        });
    },
    [project, conversationId, title, refreshHistory],
  );

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

      // The answer accumulates locally too, so the save-on-done path never
      // depends on React having flushed the last delta.
      let answerText = "";
      let answerSources: ChatSourceView[] | undefined;
      const priorTurns = messages;

      api
        .chatStream(wire, project, (event) => {
          if (turnRef.current !== turn) return; // a superseded turn's stragglers
          if (event.type === "delta") {
            answerText += event.text;
            appendToAnswer((last) => ({ ...last, content: last.content + event.text }));
          } else if (event.type === "sources") {
            const sources: ChatSourceView[] = event.sources.map((hit) => ({
              entry_id: hit.entry_id,
              kind: hit.kind,
              meeting_name: hit.meeting_name,
              timestamp: hit.timestamp,
              start_sec: hit.start_sec,
            }));
            answerSources = sources;
            appendToAnswer((last) => ({ ...last, sources }));
          } else if (event.type === "done") {
            if (event.finish_reason === "length") {
              setError("The answer hit the output limit and may be incomplete.");
            }
            setStreaming(false);
            // The turn is complete: persist the whole conversation.
            persist([
              ...priorTurns,
              { role: "user", content: question },
              { role: "assistant", content: answerText, sources: answerSources },
            ]);
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
    [project, messages, persist],
  );

  const stop = useCallback(() => {
    void api.cancelChat();
    setStreaming(false);
  }, []);

  const newConversation = useCallback(() => {
    turnRef.current += 1;
    void api.cancelChat();
    setMessages([]);
    setStreaming(false);
    setError(null);
    setConversationId(null);
    setTitle(null);
  }, []);

  const openConversation = useCallback(
    (chatId: string) => {
      if (project === null) return;
      turnRef.current += 1;
      void api.cancelChat();
      api
        .readChat(project, chatId)
        .then((conversation) => {
          setConversationId(conversation.id);
          setTitle(conversation.title);
          setMessages(
            conversation.messages.map((message) => ({
              role: message.role === "user" ? "user" : "assistant",
              content: message.content,
              sources: message.sources.length > 0 ? message.sources : undefined,
            })),
          );
          setStreaming(false);
          setError(null);
        })
        .catch((caught: unknown) => {
          const message =
            typeof caught === "object" && caught !== null && "message" in caught
              ? String((caught as { message: unknown }).message)
              : String(caught);
          setError(message);
        });
    },
    [project],
  );

  const renameConversation = useCallback(
    (chatId: string, nextTitle: string) => {
      if (project === null) return;
      api
        .renameChat(project, chatId, nextTitle)
        .then(() => {
          if (chatId === conversationId) setTitle(nextTitle);
          refreshHistory();
        })
        .catch(() => refreshHistory());
    },
    [project, conversationId, refreshHistory],
  );

  const deleteConversation = useCallback(
    (chatId: string) => {
      if (project === null) return;
      api
        .deleteChat(project, chatId)
        .then(() => {
          if (chatId === conversationId) {
            setConversationId(null);
            setTitle(null);
            setMessages([]);
          }
          refreshHistory();
        })
        .catch(() => refreshHistory());
    },
    [project, conversationId, refreshHistory],
  );

  return {
    messages,
    streaming,
    error,
    conversationId,
    history,
    send,
    stop,
    newConversation,
    openConversation,
    renameConversation,
    deleteConversation,
  };
}
