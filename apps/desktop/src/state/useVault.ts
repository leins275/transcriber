import { useCallback, useState } from "react";
import { api } from "../api";
import { sortVaultEntries } from "../lib/vaultGroups";
import type { MeetingUpdate, TranscriptView, VaultMeetingView } from "../types";

/**
 * Holds the vault listing and the per-meeting actions over it, so `App.tsx`
 * composes rather than re-implements them — the same split `useJobs` makes
 * for the session's job feed.
 *
 * `update` and `remove` edit the list **in place** instead of re-fetching.
 * That is deliberate: `list_vault` reissues every entry id on each call, so a
 * refresh after every rename would invalidate the id of the row the operator
 * is looking at and collapse whatever they had expanded. The Rust side keeps
 * a renamed meeting's id stable and returns its updated view, which is
 * exactly enough to patch one row; the list is then re-sorted locally with
 * the backend's own ordering rule, since a rename can change a meeting's date.
 */
export function useVault() {
  const [entries, setEntries] = useState<VaultMeetingView[]>([]);

  /** Re-reads the whole vault. A failure leaves whatever was already shown
   * in place rather than clearing it — a transient read error should not
   * look like an emptied vault. */
  const refresh = useCallback(async () => {
    try {
      const listed = await api.listVault();
      setEntries(listed ?? []);
    } catch {
      // Intentionally swallowed: see above.
    }
  }, []);

  const reveal = useCallback((entryId: string) => api.revealVaultEntry(entryId), []);

  const readTranscript = useCallback(
    (entryId: string): Promise<TranscriptView> => api.readTranscript(entryId),
    [],
  );

  const update = useCallback(async (entryId: string, meetingUpdate: MeetingUpdate) => {
    const updated = await api.updateVaultEntry(entryId, meetingUpdate);
    setEntries((prev) =>
      sortVaultEntries(prev.map((entry) => (entry.id === entryId ? updated : entry))),
    );
  }, []);

  const remove = useCallback(async (entryId: string) => {
    await api.deleteVaultEntry(entryId);
    setEntries((prev) => prev.filter((entry) => entry.id !== entryId));
  }, []);

  const loadServiceLog = useCallback(() => api.listServiceJobs(), []);

  return { entries, refresh, reveal, readTranscript, update, remove, loadServiceLog };
}
