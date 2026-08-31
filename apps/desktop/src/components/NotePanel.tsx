import { useCallback, useEffect, useState } from "react";
import styles from "./NotePanel.module.css";
import { Markdown } from "./Markdown";
import type { NoteView } from "../types";

export type NotePanelProps = {
  entryId: string;
  onLoad: (entryId: string) => Promise<NoteView>;
  onSave: (entryId: string, markdown: string) => Promise<void>;
  /** Reports what this panel currently shows (`null` when nothing), so the
   * page's Copy button can act on the visible tab. */
  onContentChange?: (markdown: string | null) => void;
  /** Reports whether an unsaved draft exists, so the page can guard
   * navigation away from it. */
  onDirtyChange?: (dirty: boolean) => void;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/**
 * A meeting's `note.md` — the operator's own markdown note, written right
 * here. View mode renders it like the summary; edit mode is a plain
 * textarea with an explicit Save (atomic-write semantics stay legible; no
 * autosave) and a Preview toggle reusing the same `Markdown` renderer.
 */
export function NotePanel({
  entryId,
  onLoad,
  onSave,
  onContentChange,
  onDirtyChange,
}: NotePanelProps) {
  const [note, setNote] = useState<NoteView | null>(null);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [draft, setDraft] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    onLoad(entryId)
      .then((loaded) => {
        if (cancelled) return;
        setNote(loaded);
        onContentChange?.(loaded.markdown ?? null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(messageOf(caught));
        onContentChange?.(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entryId, onLoad, onContentChange]);

  const dirty = mode === "edit" && draft !== (note?.markdown ?? "");
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  const beginEdit = useCallback(() => {
    setDraft(note?.markdown ?? "");
    setPreviewing(false);
    setSaveError(null);
    setMode("edit");
  }, [note]);

  const cancelEdit = useCallback(() => {
    setMode("view");
    setSaveError(null);
  }, []);

  const save = useCallback(() => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    onSave(entryId, draft)
      .then(() => {
        setNote((prev) => (prev ? { ...prev, markdown: draft } : prev));
        onContentChange?.(draft);
        setMode("view");
      })
      .catch((caught: unknown) => {
        setSaveError(messageOf(caught));
      })
      .finally(() => {
        setSaving(false);
      });
  }, [saving, onSave, entryId, draft, onContentChange]);

  if (loading) {
    return (
      <p role="status" className={styles.status}>
        Looking for a note…
      </p>
    );
  }

  if (error) {
    return (
      <p role="alert" className="alert">
        {error}
      </p>
    );
  }

  if (mode === "edit") {
    return (
      <div className={styles.editor}>
        {previewing ? (
          <Markdown markdown={draft} />
        ) : (
          <textarea
            aria-label="Meeting note"
            className={styles.textarea}
            value={draft}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key === "s") {
                event.preventDefault();
                save();
              }
            }}
          />
        )}
        <div className={styles.editorActions}>
          <button type="button" className="btn" disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button type="button" className="btn" disabled={saving} onClick={cancelEdit}>
            Cancel
          </button>
          <button type="button" className="btn" onClick={() => setPreviewing((prev) => !prev)}>
            {previewing ? "Keep editing" : "Preview"}
          </button>
        </div>
        {saveError ? (
          <p role="alert" className={`alert ${styles.editorError}`}>
            {saveError}
          </p>
        ) : null}
      </div>
    );
  }

  if (note?.markdown) {
    return (
      <div className={styles.view}>
        <Markdown markdown={note.markdown} />
        <button type="button" className="btn" onClick={beginEdit}>
          Edit
        </button>
      </div>
    );
  }

  return (
    <div className={styles.empty}>
      <p className={styles.emptyLead}>No note for this meeting yet.</p>
      <button type="button" className="btn" onClick={beginEdit}>
        Add note
      </button>
      <p className={styles.emptyDetail}>
        Your own markdown, saved to <span className="mono">{note?.path ?? "note.md"}</span> in the
        meeting folder.
      </p>
    </div>
  );
}
