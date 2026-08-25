/**
 * Derives the one in-flight job the header narrates while the operator is
 * away from the main view (mockup 7a: `Transcribing "ELS - Incident
 * review" · 42%`, click returns to Recordings). Display-only, from the
 * frozen `JobSnapshot` contract — no new IPC data.
 */
import { parseFileName } from "./fileName";
import { parseMeetingName } from "./meetingName";
import type { JobSnapshot, JobType } from "../types";

export type ActiveJobView = {
  /** e.g. `Transcribing “ELS - Incident review”`. */
  label: string;
  /** Whole percent, or `null` while the stage has not reported progress. */
  percent: number | null;
};

/** What each job type is doing — the header's verb, mirroring `JobRow`'s
 * running-state wording. */
const VERBS: Record<JobType, string> = {
  transcribe: "Transcribing",
  summarize: "Summarizing",
  action_items: "Extracting action items",
  export: "Exporting PDF",
};

/** The name worth narrating for a job. A transcribe job carries the dropped
 * *file's* name (`Project - YYMMDD - Title.ext`); a derived job carries the
 * *meeting folder's* name (`YYMMDD - Title`, project in the parent). Either
 * way the date is dropped — it says nothing about which job is running —
 * and an unconventional name is shown as-is (minus a file extension). */
function displayName(job: JobSnapshot): string {
  const fromFile = parseFileName(job.file_name);
  if (fromFile) return `${fromFile.project} - ${fromFile.title}`;
  const fromMeeting = parseMeetingName(job.file_name);
  if (fromMeeting) return fromMeeting.title;
  return job.job_type === "transcribe" ? job.file_name.replace(/\.[^./\\]+$/, "") : job.file_name;
}

/**
 * The job the header shows: the running one first (there is at most one —
 * the worker is serial), else whatever is filing or waiting its turn, so
 * the chip does not blink out between stages of the drop-to-insights chain.
 * `null` when nothing is in flight.
 */
export function activeJobView(jobs: JobSnapshot[]): ActiveJobView | null {
  const job =
    jobs.find((candidate) => candidate.state === "running") ??
    jobs.find((candidate) => candidate.state === "ingesting") ??
    jobs.find((candidate) => candidate.state === "queued" || candidate.state === "pending");
  if (!job) return null;
  const verb = job.state === "ingesting" ? "Filing" : (VERBS[job.job_type] ?? "Working on");
  const percent =
    job.state === "running" && job.progress != null
      ? Math.round(Math.max(0, Math.min(1, job.progress)) * 100)
      : null;
  return { label: `${verb} “${displayName(job)}”`, percent };
}
