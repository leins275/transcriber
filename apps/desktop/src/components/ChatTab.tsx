import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./ChatTab.module.css";
import { Markdown } from "./Markdown";
import type { ChatDisplayMessage } from "../state/useChat";
import type { ChatSourceView, ChatSummaryView, IndexStatusView } from "../types";

/** The empty state's ready-made questions (9a); clicking one sends it. */
const SUGGESTIONS = [
  "What was decided about the release deadline?",
  "What open questions remain from the last meeting?",
  "All action items assigned to me",
];

export type ChatTabProps = {
  /** Real project codes only — the unsorted bucket has no chat. */
  projects: string[];
  /** The selected project; lifted to App so it survives tab switches. */
  project: string | null;
  onProjectChange: (project: string) => void;
  messages: ChatDisplayMessage[];
  streaming: boolean;
  error: string | null;
  history: ChatSummaryView[];
  conversationId: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onNewConversation: () => void;
  onOpenConversation: (chatId: string) => void;
  onRenameConversation: (chatId: string, title: string) => void;
  onDeleteConversation: (chatId: string) => void;
  /** Loads one project's index state; the tab polls it while indexing. */
  onLoadIndex: (project: string) => Promise<IndexStatusView>;
  /** Queues an incremental re-index (the chip's Refresh / panel's button). */
  onReindex: () => Promise<void>;
  /** Opens a cited meeting, exactly like a library row. */
  onOpenSource: (entryId: string) => void;
  /** Appends an answer to the cited meeting's note.md. */
  onAddToNotes: (entryId: string, markdown: string) => Promise<void>;
  /** Why chat cannot run right now (no LLM model, service down). */
  disabledReason?: string;
};

function formatUpdatedAt(sec: number): string {
  return new Date(sec * 1000).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatHistoryDate(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

const INDEX_POLL_MS = 2500;

/** The index chip + its expandable panel (9c). Owns its own load/poll via
 * the injected loader, like SummaryPanel owns its read. */
function IndexStrip({
  project,
  onLoadIndex,
  onReindex,
}: {
  project: string;
  onLoadIndex: (project: string) => Promise<IndexStatusView>;
  onReindex: () => Promise<void>;
}) {
  const [status, setStatus] = useState<IndexStatusView | null>(null);
  const [open, setOpen] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      onLoadIndex(project)
        .then((loaded) => {
          if (cancelled) return;
          setStatus(loaded);
          // Poll while a pass runs so "Indexing…" resolves on its own.
          if (loaded.indexing) timer = window.setTimeout(load, INDEX_POLL_MS);
        })
        .catch(() => {
          if (!cancelled) setStatus(null); // older service: chip simply hides
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [project, onLoadIndex]);

  const refresh = useCallback(() => {
    setRefreshError(null);
    onReindex()
      .then(() =>
        onLoadIndex(project).then((loaded) => {
          setStatus(loaded);
          if (loaded.indexing) {
            // Re-arm the poll through a state change: simplest is a reload
            // loop keyed off the chip's own render; one manual chain here.
            const tick = () =>
              onLoadIndex(project).then((next) => {
                setStatus(next);
                if (next.indexing) window.setTimeout(tick, INDEX_POLL_MS);
              });
            window.setTimeout(tick, INDEX_POLL_MS);
          }
        }),
      )
      .catch((caught: unknown) => setRefreshError(messageOf(caught)));
  }, [onLoadIndex, onReindex, project]);

  if (status === null) return null;

  return (
    <div className={styles.indexStrip}>
      <div className={styles.indexChip}>
        <span className={styles.indexSummary} role="status">
          {status.indexing
            ? status.progress !== null
              ? `Indexing… ${Math.round(status.progress * 100)}%`
              : "Indexing queued…"
            : `Index: ${status.indexed_count} of ${status.total_count} meetings`}
        </span>
        <span className={styles.chipDivider} />
        <button
          type="button"
          className={styles.chipLink}
          aria-expanded={open}
          onClick={() => setOpen((wasOpen) => !wasOpen)}
        >
          {open ? "Hide" : "Show"}
        </button>
        <span className={styles.chipDivider} />
        <button type="button" className={styles.chipLink} onClick={refresh}>
          Refresh
        </button>
      </div>
      {refreshError && (
        <p role="alert" className="alert">
          {refreshError}
        </p>
      )}
      {open && (
        <div className={styles.indexPanel} aria-label={`${project} index`}>
          <div className={styles.indexPanelHead}>
            <span className={styles.indexPanelTitle}>{project} index</span>
            {status.updated_at_sec !== null && (
              <span className={styles.indexPanelUpdated}>
                updated {formatUpdatedAt(status.updated_at_sec)}
              </span>
            )}
          </div>
          <ul className={styles.indexList}>
            {status.meetings.map((meeting) => (
              <li key={meeting.name} className={styles.indexRow}>
                <span
                  className={
                    meeting.state === "indexed" ? styles.indexStateDone : styles.indexStateOther
                  }
                >
                  {meeting.state === "indexed" ? "✓" : meeting.state === "pending" ? "…" : "—"}
                </span>
                <span className={styles.indexMeetingName}>{meeting.name}</span>
                <span className={styles.indexMeetingDetail}>
                  {meeting.state === "indexed"
                    ? `${meeting.chunks} fragment${meeting.chunks === 1 ? "" : "s"}`
                    : meeting.state === "pending"
                      ? "awaiting indexing"
                      : "no transcript — outside the index"}
                </span>
              </li>
            ))}
          </ul>
          <p className={styles.indexHint}>
            The index keeps itself current after every transcription and note save; Refresh catches
            up a vault changed outside the app. Unchanged meetings are skipped.
          </p>
        </div>
      )}
    </div>
  );
}

/** One answer's sources block + actions (9b). */
function AnswerFooter({
  message,
  onOpenSource,
  onAddToNotes,
}: {
  message: ChatDisplayMessage;
  onOpenSource: (entryId: string) => void;
  onAddToNotes: (entryId: string, markdown: string) => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const [noted, setNoted] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const sources = message.sources ?? [];
  const noteTarget = sources.find((source): source is ChatSourceView & { entry_id: string } =>
    Boolean(source.entry_id),
  );

  const copy = () => {
    navigator.clipboard
      .writeText(message.content)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      })
      .catch((caught: unknown) => setActionError(messageOf(caught)));
  };

  const addToNotes = () => {
    if (!noteTarget) return;
    const stamp = new Date().toLocaleDateString(undefined, { day: "numeric", month: "short" });
    onAddToNotes(noteTarget.entry_id, `### From project chat (${stamp})\n\n${message.content}`)
      .then(() => {
        setNoted(true);
        window.setTimeout(() => setNoted(false), 2000);
      })
      .catch((caught: unknown) => setActionError(messageOf(caught)));
  };

  return (
    <>
      {sources.length > 0 && (
        <div className={styles.sources}>
          <div className={styles.sourcesLabel}>Sources</div>
          {sources.map((source, index) => (
            <div key={`${source.meeting_name}-${index}`} className={styles.sourceRow}>
              <span className={styles.sourceNumber}>{index + 1}</span>
              {source.entry_id ? (
                <button
                  type="button"
                  className={styles.sourceLink}
                  onClick={() => onOpenSource(source.entry_id as string)}
                >
                  {source.meeting_name}
                </button>
              ) : (
                <span className={styles.sourceGone}>{source.meeting_name}</span>
              )}
              {source.timestamp && (
                <span className={`${styles.sourceTime} mono`}>{source.timestamp}</span>
              )}
            </div>
          ))}
        </div>
      )}
      <div className={styles.answerActions}>
        <button type="button" className={styles.chipLink} onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
        {noteTarget && (
          <button type="button" className={styles.chipLink} onClick={addToNotes}>
            {noted ? "Added to notes" : "Add to meeting notes"}
          </button>
        )}
      </div>
      {actionError && (
        <p role="alert" className="alert">
          {actionError}
        </p>
      )}
    </>
  );
}

/**
 * The library's Chat tab (redesign turn 9): ask the local model about one
 * project's meetings. The project is chosen right here, the index's state
 * is a status line (not a mystery), and conversations persist in the
 * vault's `chats/` folder — the History row above the composer opens them.
 *
 * Presentational apart from its callbacks: no invoke, no listen, no fetch.
 */
export function ChatTab({
  projects,
  project,
  onProjectChange,
  messages,
  streaming,
  error,
  history,
  conversationId,
  onSend,
  onStop,
  onNewConversation,
  onOpenConversation,
  onRenameConversation,
  onDeleteConversation,
  onLoadIndex,
  onReindex,
  onOpenSource,
  onAddToNotes,
  disabledReason,
}: ChatTabProps) {
  const [draft, setDraft] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  // Keep the newest tokens in view while an answer streams.
  useEffect(() => {
    if (streaming && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  const disabled = streaming || Boolean(disabledReason) || project === null;

  const submit = () => {
    const text = draft.trim();
    if (!text || disabled) return;
    setDraft("");
    setHistoryOpen(false);
    onSend(text);
  };

  if (projects.length === 0) {
    return (
      <p className={styles.noProjects}>
        Chat needs at least one project. A recording named{" "}
        <span className="mono">ELS - 260812 - Weekly sync.mp4</span> files itself under project{" "}
        <span className="mono">ELS</span>.
      </p>
    );
  }

  return (
    <div className={styles.tab}>
      <div className={styles.controlRow}>
        <label className={styles.projectChip}>
          <span className={styles.projectKicker}>Project</span>
          <select
            className={styles.projectSelect}
            aria-label="Chat project"
            value={project ?? ""}
            onChange={(event) => onProjectChange(event.target.value)}
          >
            {project === null && <option value="">Choose…</option>}
            {projects.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </label>
        <span className={styles.switchHint}>Switching projects starts a new conversation</span>
        {project !== null && (
          <div className={styles.controlRight}>
            <IndexStrip project={project} onLoadIndex={onLoadIndex} onReindex={onReindex} />
          </div>
        )}
      </div>

      {project === null ? (
        <p className={styles.noProjects}>Choose a project to ask about its meetings.</p>
      ) : messages.length === 0 ? (
        <div className={styles.empty}>
          <div className={styles.emptyTitle}>Ask about {project}</div>
          <p className={styles.emptyLead}>
            Answers come from this project&apos;s transcripts, summaries and notes, generated by the
            local language model. Nothing leaves this machine.
          </p>
          <div className={styles.suggestions}>
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                className={styles.suggestion}
                disabled={disabled}
                onClick={() => onSend(suggestion)}
              >
                <span className={styles.suggestionArrow}>→</span>
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div ref={logRef} role="log" aria-live="polite" className={styles.log}>
          {messages.map((message, index) => (
            <div key={index} className={message.role === "user" ? styles.question : styles.answer}>
              {message.role === "user" ? (
                <p className={styles.questionText}>{message.content}</p>
              ) : (
                <>
                  {message.content ? (
                    <Markdown markdown={message.content} />
                  ) : (
                    streaming &&
                    index === messages.length - 1 && (
                      <p role="status" className={styles.status}>
                        Answering…
                      </p>
                    )
                  )}
                  {(message.content || !streaming) && (
                    <AnswerFooter
                      message={message}
                      onOpenSource={onOpenSource}
                      onAddToNotes={onAddToNotes}
                    />
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {project !== null && (history.length > 0 || messages.length > 0) && (
        <div className={styles.historyRow}>
          <button
            type="button"
            className={styles.chipLink}
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((wasOpen) => !wasOpen)}
          >
            History <span className={styles.historyCount}>{history.length}</span> ▾
          </button>
          <button type="button" className={styles.chipLink} onClick={onNewConversation}>
            New conversation
          </button>
        </div>
      )}

      {historyOpen && project !== null && (
        <div className={styles.historyPanel} aria-label={`Conversations about ${project}`}>
          {history.length === 0 ? (
            <p className={styles.status}>No saved conversations yet.</p>
          ) : (
            history.map((conversation) => (
              <div
                key={conversation.id}
                className={
                  conversation.id === conversationId
                    ? `${styles.historyItem} ${styles.historyItemActive}`
                    : styles.historyItem
                }
              >
                <button
                  type="button"
                  className={styles.historyOpen}
                  onClick={() => {
                    onOpenConversation(conversation.id);
                    setHistoryOpen(false);
                  }}
                >
                  <span className={styles.historyTitle}>{conversation.title}</span>
                  <span className={styles.historyMeta}>
                    {formatHistoryDate(conversation.updated_at_ms)} · {conversation.question_count}{" "}
                    question{conversation.question_count === 1 ? "" : "s"}
                  </span>
                </button>
                <button
                  type="button"
                  className={styles.chipLink}
                  aria-label={`Rename ${conversation.title}`}
                  onClick={() => {
                    const next = window.prompt("Conversation title", conversation.title);
                    if (next && next.trim()) onRenameConversation(conversation.id, next.trim());
                  }}
                >
                  Rename
                </button>
                <button
                  type="button"
                  className={styles.chipLink}
                  aria-label={`Delete ${conversation.title}`}
                  onClick={() => {
                    if (window.confirm(`Delete "${conversation.title}"?`)) {
                      onDeleteConversation(conversation.id);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            ))
          )}
        </div>
      )}

      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <textarea
          className={styles.input}
          aria-label={project ? `Ask about ${project}` : "Ask about a project"}
          placeholder={
            messages.length > 0 ? "Follow up…" : project ? `Ask about ${project}…` : "Ask…"
          }
          value={draft}
          disabled={Boolean(disabledReason) || project === null}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        {streaming ? (
          <button type="button" className="btn btn-secondary" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="submit" className="btn" disabled={disabled || !draft.trim()}>
            Send
          </button>
        )}
      </form>
      {disabledReason && <p className={styles.hint}>{disabledReason}</p>}
    </div>
  );
}
