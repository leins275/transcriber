import { useEffect, useState } from "react";
import styles from "./VoiceMemoryPanel.module.css";
import { formatCount, formatDuration, formatTimestamp } from "../lib/format";
import type {
  VoiceMeetingView,
  VoiceSampleView,
  VoiceStatusView,
  VoiceSummaryView,
} from "../types";

export type VoiceMemoryPanelProps = {
  project: string;
  /** The open meeting's folder name: its row is marked, and its
   * machine-given names can be confirmed from here. */
  currentMeeting: string;
  /** Reads the memory as this project sees it. Reading is also what brings
   * the service's index up to date, so the panel never asks for a refresh. */
  onLoad: (project: string) => Promise<VoiceStatusView>;
  /** Vouches for the open meeting's machine-given names. */
  onConfirm: () => Promise<void>;
  /** Bumped whenever something the memory is built from may have changed (a
   * label saved, a speaker pass finished): the panel reads again. */
  reloadToken: number;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/** Why a sample is set aside, in the operator's words. */
export function setAsideReason(sample: VoiceSampleView): string {
  switch (sample.quality) {
    case "unconfirmed":
      return "named automatically, not confirmed";
    case "partial":
      return "named on only part of this voice";
    case "short":
      return `too little speech (${formatDuration(sample.speech_sec)})`;
    case "conflict":
      return sample.conflicts_with
        ? `sounds like ${sample.conflicts_with}`
        : "sounds like someone else";
    case "ok":
      return "";
  }
}

/** One person's line: what recognition has to go on, and where it came from. */
export function voiceDetail(voice: VoiceSummaryView): string {
  if (voice.samples === 0) {
    return voice.set_aside > 0
      ? `no usable sample — ${formatCount(voice.set_aside, "sample")} set aside`
      : "no voice sample yet";
  }
  const parts = [
    `${formatCount(voice.samples, "sample")} · ${formatDuration(voice.speech_sec)} of speech`,
  ];
  if (voice.other_projects.length > 0) {
    const others = voice.other_projects.join(", ");
    parts.push(voice.here > 0 ? `here and in ${others}` : `known from ${others}`);
  }
  if (voice.set_aside > 0) parts.push(`${voice.set_aside} set aside`);
  return parts.join(" · ");
}

function meetingDetail(meeting: VoiceMeetingView): string {
  switch (meeting.state) {
    case "no_transcript":
      return "no transcript";
    case "no_voices":
      return "no voice data — run Identify speakers";
    case "unnamed":
      return "voices found, nobody named yet";
    case "named":
      return meeting.voices
        .map((sample) =>
          sample.quality === "ok" ? sample.name : `${sample.name} (${setAsideReason(sample)})`,
        )
        .join(", ");
  }
}

function meetingMark(meeting: VoiceMeetingView): "used" | "aside" | "none" {
  if (meeting.voices.some((sample) => sample.quality === "ok")) return "used";
  if (meeting.voices.length > 0) return "aside";
  return "none";
}

/** What the read just did: the invalidation, made visible. */
function freshness(status: VoiceStatusView): string {
  const elsewhere =
    status.rescanned_elsewhere > 0
      ? ` · ${formatCount(status.rescanned_elsewhere, "meeting")} re-read in other projects`
      : "";
  if (status.rescanned.length > 0) {
    return `Just re-read ${formatCount(status.rescanned.length, "changed meeting")}${elsewhere}`;
  }
  const last =
    status.updated_at === null
      ? ""
      : ` · last change read ${formatTimestamp(new Date(status.updated_at * 1000).toISOString())}`;
  return `Up to date${last}${elsewhere}`;
}

/**
 * The voice memory as the open recording's project sees it.
 *
 * Three questions, top to bottom: is what I am looking at current (the
 * status line says what the read just had to re-read), who can a meeting
 * here be named for (one line per person, with where their samples come
 * from and how many were set aside), and what does each meeting of this
 * project contribute (and when nothing, why).
 *
 * There is no refresh button on purpose. The service compares every
 * meeting's files with what it has on each read and re-reads the ones that
 * changed; the panel reads on open and whenever `reloadToken` moves, and
 * shows which meetings that read picked up.
 *
 * Owns its own load; otherwise presentational (no invoke, no listen).
 */
export function VoiceMemoryPanel({
  project,
  currentMeeting,
  onLoad,
  onConfirm,
  reloadToken,
}: VoiceMemoryPanelProps) {
  const [status, setStatus] = useState<VoiceStatusView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmed, setConfirmed] = useState(0);

  useEffect(() => {
    let cancelled = false;
    onLoad(project)
      .then((loaded) => {
        if (cancelled) return;
        setStatus(loaded);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(messageOf(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [onLoad, project, reloadToken, confirmed]);

  async function confirm() {
    if (confirming) return;
    setConfirming(true);
    try {
      await onConfirm();
      setConfirmed((count) => count + 1);
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setConfirming(false);
    }
  }

  const current = status?.meetings.find((meeting) => meeting.name === currentMeeting);
  const unconfirmedHere =
    current?.voices.some((sample) => sample.quality === "unconfirmed") ?? false;

  return (
    <section className={styles.panel} aria-label="Voice memory">
      <div className={styles.head}>
        <h3 className={styles.title}>Voice memory</h3>
        <span className={styles.freshness} role="status">
          {status ? freshness(status) : error ? "Unavailable" : "Reading the voice memory…"}
        </span>
      </div>

      <p className={styles.lead}>
        Returning voices are recognized from the names you gave by hand, in any project. The memory
        is read from the meetings themselves: change a name anywhere and the next look picks it up.
      </p>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {status && unconfirmedHere && (
        <div className={styles.confirm}>
          <p className={styles.confirmText}>
            Names in this meeting were given automatically. They are not used as references until
            you confirm or change them.
          </p>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={confirming}
            onClick={confirm}
          >
            {confirming ? "Confirming…" : "Confirm these names"}
          </button>
        </div>
      )}

      {status && (
        <>
          {status.roster_only && (
            <p className={styles.note}>
              This project&apos;s roster is strict: only its names are offered here.
            </p>
          )}
          {status.voices.length > 0 ? (
            <ul className={styles.list} aria-label="Known voices">
              {status.voices.map((voice) => (
                <li key={voice.name} className={styles.row}>
                  <span className={voice.samples > 0 ? styles.markUsed : styles.markNone}>
                    {voice.samples > 0 ? "✓" : "—"}
                  </span>
                  <span className={styles.name}>{voice.name}</span>
                  <span className={styles.detail}>{voiceDetail(voice)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.note}>
              Nobody is known yet. Name the speakers of a meeting that went through Identify
              speakers, and they will be recognized in the next one.
            </p>
          )}

          <details className={styles.meetings}>
            <summary className={styles.meetingsSummary}>
              What this project&apos;s meetings contribute ({status.meetings.length})
            </summary>
            <ul className={styles.list} aria-label="Meetings">
              {status.meetings.map((meeting) => {
                const mark = meetingMark(meeting);
                return (
                  <li key={meeting.name} className={styles.row}>
                    <span
                      className={
                        mark === "used"
                          ? styles.markUsed
                          : mark === "aside"
                            ? styles.markAside
                            : styles.markNone
                      }
                    >
                      {mark === "used" ? "✓" : mark === "aside" ? "!" : "—"}
                    </span>
                    <span className={styles.name}>
                      {meeting.name}
                      {meeting.name === currentMeeting && (
                        <span className={styles.tag}>this meeting</span>
                      )}
                      {status.rescanned.includes(meeting.name) && (
                        <span className={styles.tag}>re-read just now</span>
                      )}
                    </span>
                    <span className={styles.detail}>{meetingDetail(meeting)}</span>
                  </li>
                );
              })}
            </ul>
          </details>
        </>
      )}
    </section>
  );
}
