import { useCallback, useEffect, useState } from "react";
import { api, onJobsUpdated, safeUnlisten } from "../api";
import type { JobSnapshot } from "../types";

/**
 * Holds the current-session job list and subscribes to `jobs://updated`
 * (FR-8, FR-14). Upserts by id: a known id is replaced in place so a job
 * visibly transitions `queued -> running -> done`; an unknown id is
 * appended rather than dropped, and submission order is preserved.
 */
export function useJobs() {
  const [jobs, setJobs] = useState<JobSnapshot[]>([]);

  const upsert = useCallback((job: JobSnapshot) => {
    setJobs((prev) => {
      const index = prev.findIndex((existing) => existing.id === job.id);
      if (index === -1) {
        return [...prev, job];
      }
      const next = prev.slice();
      next[index] = job;
      return next;
    });
  }, []);

  // `enqueue`'s own `Pending` snapshots were already emitted as
  // `jobs://updated` *before* any ingest work started -- if the pipeline
  // has since advanced (or even finished) by the time the `enqueue_paths`
  // invoke response reaches JS, blindly upserting would revert an
  // already-progressed row back to `Pending` (or, for a terminal job,
  // leave it stuck there for the rest of the session). Only append when
  // the id is genuinely new; a known id's later transitions arrive
  // exclusively through the `jobs://updated` listener above.
  const insertIfUnknown = useCallback((job: JobSnapshot) => {
    setJobs((prev) => {
      if (prev.some((existing) => existing.id === job.id)) {
        return prev;
      }
      return [...prev, job];
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;

    onJobsUpdated(upsert).then((fn) => {
      if (cancelled) {
        fn();
      } else {
        unlisten = fn;
      }
    });

    return () => {
      cancelled = true;
      safeUnlisten(unlisten);
    };
  }, [upsert]);

  const enqueue = useCallback(
    async (paths: string[]) => {
      const snapshots = await api.enqueuePaths(paths);
      snapshots.forEach(insertIfUnknown);
      return snapshots;
    },
    [insertIfUnknown],
  );

  return { jobs, enqueue, upsert };
}
