import { useCallback, useState } from "react";
import { api } from "../api";
import { sortVaultEntries } from "../lib/vaultGroups";
import type {
  ActionItemsView,
  ItemScreenshotView,
  ItemScreenshotsView,
  MeetingUpdate,
  SummaryView,
  TranscriptView,
  VaultMeetingView,
} from "../types";

/**
 * Holds the vault listing and the per-meeting actions over it, so `App.tsx`
 * composes rather than re-implements them — the same split `useJobs` makes
 * for the session's job feed.
 *
 * `update` and `remove` edit the list **in place** instead of re-fetching —
 * a one-row change needs no full vault rescan. The Rust side keeps a renamed
 * meeting's id stable and returns its updated view, which is exactly enough
 * to patch one row; the list is then re-sorted locally with the backend's
 * own ordering rule, since a rename can change a meeting's date. Full
 * refreshes also keep ids stable for meetings still on disk (`list_vault`
 * reuses the ids it issued before), so the recording page that is open
 * during the after-each-job refresh keeps its entry instead of bouncing the
 * operator back to the library.
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

  const readSummary = useCallback(
    (entryId: string): Promise<SummaryView> => api.readSummary(entryId),
    [],
  );

  const readActionItems = useCallback(
    (entryId: string): Promise<ActionItemsView> => api.readActionItems(entryId),
    [],
  );

  const captureItemScreenshots = useCallback(
    (entryId: string, itemDirName: string): Promise<ItemScreenshotsView> =>
      api.captureItemScreenshots(entryId, itemDirName),
    [],
  );

  const readItemScreenshots = useCallback(
    (entryId: string, itemDirName: string): Promise<ItemScreenshotView[]> =>
      api.readItemScreenshots(entryId, itemDirName),
    [],
  );

  const saveSpeakers = useCallback(
    (entryId: string, assignments: Record<string, string>) =>
      api.setSpeakerLabels(entryId, assignments),
    [],
  );

  const loadServiceLog = useCallback(() => api.listServiceJobs(), []);

  return {
    entries,
    refresh,
    reveal,
    readTranscript,
    readSummary,
    readActionItems,
    captureItemScreenshots,
    readItemScreenshots,
    saveSpeakers,
    update,
    remove,
    loadServiceLog,
  };
}
