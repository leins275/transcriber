import { useCallback, useEffect, useRef, useState } from "react";
import { api, onJobsUpdated, safeUnlisten } from "../api";
import type { JobSnapshot } from "../types";

/** How long a cancelled job stays in the list before it is dropped. Long
 * enough to confirm the click landed, short enough that the queue is not
 * cluttered with rows the operator already asked to go away. */
export const CANCELLED_JOB_LINGER_MS = 5000;

/** A cancelled job, as the wire collapses it: F2's `cancelled` status
 * arrives as `failed` with the literal message `"cancelled"` (the Rust
 * side attaches that message precisely so the UI can tell the two apart --
 * see `service/mod.rs`). */
function isCancelled(job: JobSnapshot): boolean {
  return job.state === "failed" && job.message === "cancelled";
}

/**
 * Holds the current-session job list and subscribes to `jobs://updated`
 * (FR-8, FR-14). Upserts by id: a known id is replaced in place so a job
 * visibly transitions `queued -> running -> done`; an unknown id is
 * appended rather than dropped, and submission order is preserved.
 *
 * A job the operator cancelled is the one terminal state with nothing left
 * to say -- unlike a failure it needs no attention, and unlike `done` it
 * produced nothing to open -- so it lingers briefly (acknowledging the
 * click) and then drops out of the list on its own.
 */
export function useJobs() {
  const [jobs, setJobs] = useState<JobSnapshot[]>([]);

  // One pending-removal timer per cancelled job id, so a repeated
  // `jobs://updated` for the same cancelled job never stacks a second
  // timer (or resets the first one's clock).
  const removalTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const removeAfterLinger = useCallback((jobId: string) => {
    if (removalTimers.current.has(jobId)) return;
    const handle = setTimeout(() => {
      removalTimers.current.delete(jobId);
      setJobs((prev) => prev.filter((job) => job.id !== jobId));
    }, CANCELLED_JOB_LINGER_MS);
    removalTimers.current.set(jobId, handle);
  }, []);

  useEffect(() => {
    const timers = removalTimers.current;
    return () => {
      timers.forEach((handle) => clearTimeout(handle));
      timers.clear();
    };
  }, []);

  const upsert = useCallback(
    (job: JobSnapshot) => {
      setJobs((prev) => {
        const index = prev.findIndex((existing) => existing.id === job.id);
        if (index === -1) {
          return [...prev, job];
        }
        const next = prev.slice();
        next[index] = job;
        return next;
      });
      if (isCancelled(job)) {
        removeAfterLinger(job.id);
      }
    },
    [removeAfterLinger],
  );

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
