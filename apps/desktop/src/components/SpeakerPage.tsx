import { useEffect, useState } from "react";
import styles from "./SpeakerPage.module.css";
import { setAsideReason } from "./VoiceMemoryPanel";
import { formatDuration, formatTimecode } from "../lib/format";
import { formatMeetingDate, parseEntryName } from "../lib/meetingName";
import type {
  SpeakerDetailView,
  SpeakerMeetingView,
  SpeakerRecord,
  SpeakerUpdate,
  VoiceSampleQuality,
} from "../types";

export type SpeakerPageProps = {
  /** Any name the person answers to. After a rename the caller passes the
   * new one (see `onRenamed`) and the page reads again under it. */
  name: string;
  onLoad: (name: string) => Promise<SpeakerDetailView>;
  /** Applies a rename, an alias list or a bio; resolves to the stored entry. */
  onSave: (name: string, update: SpeakerUpdate) => Promise<SpeakerRecord>;
  /** Removes the registry entry. The caller decides where to go afterwards. */
  onDelete: (name: string) => Promise<void>;
  onBack: () => void;
  /** Opens a recording by its vault entry id. */
  onOpenRecording: (entryId: string) => void;
  /** Reports the person's canonical name after a save changed it, so the
   * caller keeps this page open under the name that now exists. */
  onRenamed: (name: string) => void;
};

function messageOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

/** The project a meeting's folder name is read under: the service files
 * unsorted recordings under the literal `unsorted`, which has no naming
 * convention to parse. */
function meetingTitle(meeting: SpeakerMeetingView): string {
  const project = meeting.project === "unsorted" ? null : meeting.project;
  const parsed = parseEntryName(meeting.meeting, project);
  return parsed ? `${formatMeetingDate(parsed.date)} · ${parsed.title}` : meeting.meeting;
}

/** Why this meeting's voice sample is set aside, in the voice-memory
 * panel's own words. The page knows neither the sample's own length nor
 * whom it was confused with, so those two reasons stay general. */
function setAsideNote(quality: VoiceSampleQuality): string {
  if (quality === "short") return "too little speech";
  return setAsideReason({ label: "", name: "", speech_sec: 0, quality, conflicts_with: null });
}

function PencilIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 20h4L19 9l-4-4L4 16v4z" />
      <path d="M13.5 6.5l4 4" />
    </svg>
  );
}

function MeetingBlock({
  meeting,
  onOpenRecording,
}: {
  meeting: SpeakerMeetingView;
  onOpenRecording: (entryId: string) => void;
}) {
  const title = meetingTitle(meeting);
  const entryId = meeting.entry_id;
  const quality = meeting.voice_quality;
  const setAside = quality !== null && quality !== "ok";
  const meta = [
    `${meeting.hand_segments} of ${meeting.labelled_segments} segments labelled by hand`,
    formatDuration(meeting.speech_sec),
    quality === null ? "no voice sample" : quality === "ok" ? "voice sample in use" : null,
  ].filter((part): part is string => part !== null);

  return (
    <li className={styles.meeting}>
      <div className={styles.meetingHead}>
        <span className="pill">{meeting.project}</span>
        {entryId !== null ? (
          <button
            type="button"
            className={styles.meetingLink}
            onClick={() => onOpenRecording(entryId)}
          >
            {title}
          </button>
        ) : (
          <span className={styles.meetingTitle}>{title}</span>
        )}
      </div>
      <div className={styles.meetingMeta}>
        <span>{meta.join(" · ")}</span>
        {setAside && (
          <span className={styles.setAside}>voice sample set aside: {setAsideNote(quality)}</span>
        )}
      </div>
      {meeting.segments.length > 0 && (
        <div className={styles.segments}>
          {meeting.segments.map((segment) => (
            <div key={segment.id} className={styles.segment}>
              <span className={`${styles.timecode} mono`}>{formatTimecode(segment.start)}</span>
              <span>{segment.text.trim()}</span>
            </div>
          ))}
        </div>
      )}
      {meeting.segments_truncated && (
        <p className={styles.muted}>
          Showing the first {meeting.segments.length} of {meeting.hand_segments}.
          {entryId !== null && " Open the recording for the rest."}
        </p>
      )}
    </li>
  );
}

/**
 * One person of the speakers database, given the whole window.
 *
 * The head says who this is — the name, the other names they are labelled
 * under, and how much of them the vault holds. The main column is the
 * evidence: every meeting they were labelled in, newest first, with the
 * segments the operator named by hand. The aside holds what is edited or
 * read rarely: the bio, the projects, the voice memory and removal.
 *
 * Every edit is its own explicit save (rename, an alias added or removed,
 * the bio), and each one re-reads the person — adding another speaker's
 * name as an alias merges the two, which changes everything on the page.
 * Nothing here rewrites a label in a meeting.
 *
 * Owns its own load; otherwise presentational (no invoke, no listen).
 */
export function SpeakerPage({
  name,
  onLoad,
  onSave,
  onDelete,
  onBack,
  onOpenRecording,
  onRenamed,
}: SpeakerPageProps) {
  const [detail, setDetail] = useState<SpeakerDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  // `null` while the form is closed; the draft otherwise.
  const [renameDraft, setRenameDraft] = useState<string | null>(null);
  const [aliasDraft, setAliasDraft] = useState<string | null>(null);
  // `null` until the operator types: the textarea then shows the stored bio.
  const [bioDraft, setBioDraft] = useState<string | null>(null);
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  useEffect(() => {
    let cancelled = false;
    onLoad(name)
      .then((loaded) => {
        if (cancelled) return;
        setDetail(loaded);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(messageOf(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [onLoad, name, reload]);

  /** One save, then whatever it changed is read back. */
  async function save(update: SpeakerUpdate): Promise<boolean> {
    if (detail === null || busy) return false;
    setBusy(true);
    setError(null);
    try {
      const stored = await onSave(detail.name, update);
      if (stored.name !== name) onRenamed(stored.name);
      setReload((count) => count + 1);
      return true;
    } catch (caught) {
      setError(messageOf(caught));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function submitRename(event: React.FormEvent) {
    event.preventDefault();
    const newName = (renameDraft ?? "").trim();
    if (detail === null || newName === "") return;
    if (newName === detail.name) {
      setRenameDraft(null);
      return;
    }
    if (await save({ newName })) setRenameDraft(null);
  }

  async function submitAlias(event: React.FormEvent) {
    event.preventDefault();
    const alias = (aliasDraft ?? "").trim();
    if (detail === null || alias === "") return;
    if (await save({ aliases: [...detail.aliases, alias] })) setAliasDraft(null);
  }

  async function removeAlias(alias: string) {
    if (detail === null) return;
    await save({ aliases: detail.aliases.filter((listed) => listed !== alias) });
  }

  async function saveBio() {
    if (bioDraft === null) return;
    if (await save({ bio: bioDraft })) setBioDraft(null);
  }

  async function remove() {
    if (detail === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onDelete(detail.name);
    } catch (caught) {
      setError(messageOf(caught));
      setBusy(false);
    }
  }

  const bio = bioDraft ?? detail?.bio ?? "";
  const bioDirty = detail !== null && bioDraft !== null && bioDraft !== detail.bio;
  const speechSec = detail?.meetings.reduce((sum, meeting) => sum + meeting.speech_sec, 0) ?? 0;

  return (
    <section className={styles.page} aria-label="Speaker">
      <div className={styles.head}>
        <div className={styles.breadcrumb}>
          <button type="button" className={`btn btn-ghost ${styles.target}`} onClick={onBack}>
            ← Speakers
          </button>
          <span className={styles.crumbSeparator}>/</span>
          {detail && (
            <span className="pill">
              {detail.registered ? "in the database" : "not in the database yet"}
            </span>
          )}
        </div>

        <div className={styles.headRow}>
          <div className={styles.identity}>
            {renameDraft === null ? (
              <div className={styles.nameLine}>
                <h2 className={styles.name}>{detail?.name ?? name}</h2>
                <button
                  type="button"
                  className={`btn btn-ghost ${styles.pencil}`}
                  aria-label="Rename speaker"
                  disabled={detail === null}
                  onClick={() => setRenameDraft(detail?.name ?? name)}
                >
                  <PencilIcon />
                </button>
              </div>
            ) : (
              <form
                className={styles.inlineForm}
                onSubmit={submitRename}
                aria-label="Rename speaker"
              >
                <input
                  className={styles.input}
                  aria-label="Name"
                  value={renameDraft}
                  autoFocus
                  disabled={busy}
                  onChange={(event) => setRenameDraft(event.target.value)}
                />
                <button
                  type="submit"
                  className={`btn ${styles.target}`}
                  disabled={busy || renameDraft.trim() === ""}
                >
                  Rename
                </button>
                <button
                  type="button"
                  className={`btn btn-secondary ${styles.target}`}
                  disabled={busy}
                  onClick={() => setRenameDraft(null)}
                >
                  Cancel
                </button>
              </form>
            )}

            {detail && (
              <>
                <div className={styles.aliases}>
                  <span className={styles.kicker}>Also known as</span>
                  {detail.aliases.map((alias) => (
                    <span key={alias} className={styles.chip}>
                      {alias}
                      <button
                        type="button"
                        className={styles.chipRemove}
                        aria-label={`Remove alias ${alias}`}
                        disabled={busy}
                        onClick={() => void removeAlias(alias)}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {aliasDraft === null ? (
                    <button
                      type="button"
                      className={styles.chipAdd}
                      onClick={() => setAliasDraft("")}
                    >
                      + Add a name
                    </button>
                  ) : (
                    <form
                      className={styles.inlineForm}
                      onSubmit={submitAlias}
                      aria-label="Add a name"
                    >
                      <input
                        className={`${styles.input} ${styles.aliasInput}`}
                        aria-label="Another name for this speaker"
                        value={aliasDraft}
                        autoFocus
                        disabled={busy}
                        onChange={(event) => setAliasDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Escape") setAliasDraft(null);
                        }}
                      />
                      <button
                        type="submit"
                        className="btn"
                        disabled={busy || aliasDraft.trim() === ""}
                      >
                        Add
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busy}
                        onClick={() => setAliasDraft(null)}
                      >
                        Cancel
                      </button>
                    </form>
                  )}
                </div>
                <p className={styles.muted}>
                  Labels written under any of these names count as this person. Adding another
                  speaker&apos;s name merges the two.
                </p>
              </>
            )}
          </div>

          {detail && (
            <div className={styles.stats} aria-label="Totals">
              <div className={styles.stat}>
                <span className={styles.statValue}>{detail.meetings.length}</span>
                <span className={styles.kicker}>meetings</span>
              </div>
              <div className={styles.stat}>
                <span className={styles.statValue}>{formatDuration(speechSec)}</span>
                <span className={styles.kicker}>labelled speech</span>
              </div>
              <div className={styles.stat}>
                <span className={styles.statValue}>{detail.voice.samples}</span>
                <span className={styles.kicker}>voice samples</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {detail === null && !error && (
        <p role="status" className={styles.muted}>
          Reading the speakers database…
        </p>
      )}

      {detail && (
        <div className={styles.body}>
          <div className={styles.main}>
            <div className={styles.sectionHead}>
              <h3 className={styles.sectionTitle}>Speech you labelled</h3>
              <span className={styles.muted}>newest first · only segments named by hand</span>
            </div>
            {detail.meetings.length === 0 ? (
              <p className={styles.muted}>
                No meeting carries a label under any of this speaker&apos;s names yet.
              </p>
            ) : (
              <ul className={styles.meetings} aria-label="Meetings">
                {detail.meetings.map((meeting) => (
                  <MeetingBlock
                    key={`${meeting.project}/${meeting.meeting}`}
                    meeting={meeting}
                    onOpenRecording={onOpenRecording}
                  />
                ))}
              </ul>
            )}
          </div>

          <aside className={styles.aside}>
            <div className={styles.section}>
              <label htmlFor="speaker-bio" className={styles.sectionTitle}>
                About
              </label>
              <textarea
                id="speaker-bio"
                className={styles.bio}
                rows={5}
                placeholder="Who this is: role, company, what they usually talk about."
                value={bio}
                disabled={busy}
                onChange={(event) => setBioDraft(event.target.value)}
              />
              <div className={styles.saveRow}>
                <button
                  type="button"
                  className={`btn ${styles.target}`}
                  disabled={busy || !bioDirty}
                  onClick={() => void saveBio()}
                >
                  Save
                </button>
                {bioDirty && <span className={styles.muted}>Unsaved changes</span>}
              </div>
            </div>

            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Projects</h3>
              {detail.projects.length === 0 ? (
                <p className={styles.muted}>Not labelled in any project yet.</p>
              ) : (
                <table className={styles.projects}>
                  <thead>
                    <tr>
                      <th scope="col">Project</th>
                      <th scope="col" className={styles.right}>
                        Meetings
                      </th>
                      <th scope="col" className={styles.right}>
                        Speech
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.projects.map((project) => (
                      <tr key={project.project}>
                        <td>
                          {project.project}
                          {project.in_roster && (
                            <span className={styles.mutedInline}> · on the roster</span>
                          )}
                        </td>
                        <td className={styles.right}>{project.meetings}</td>
                        <td className={styles.right}>{formatDuration(project.speech_sec)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Voice memory</h3>
              <dl className={styles.voice}>
                <div className={styles.voiceRow}>
                  <dt>Samples recognition uses</dt>
                  <dd>
                    {detail.voice.samples} · {formatDuration(detail.voice.speech_sec)}
                  </dd>
                </div>
                <div className={styles.voiceRow}>
                  <dt>Set aside</dt>
                  <dd>{detail.voice.set_aside}</dd>
                </div>
              </dl>
              <p className={styles.muted}>
                Each meeting on the left says whether its sample is in use, and why not when it is
                set aside.
              </p>
            </div>

            {detail.registered && (
              <div className={`${styles.section} ${styles.removal}`}>
                {confirmingRemove ? (
                  <>
                    <p className={styles.confirmText}>
                      Remove <strong>{detail.name}</strong> from the database? Their name, aliases
                      and bio are deleted. Labels in meetings are not touched.
                    </p>
                    <div className={styles.saveRow}>
                      <button
                        type="button"
                        className={`btn ${styles.target} ${styles.danger}`}
                        disabled={busy}
                        onClick={() => void remove()}
                      >
                        {busy ? "Removing…" : "Remove"}
                      </button>
                      <button
                        type="button"
                        className={`btn btn-secondary ${styles.target}`}
                        disabled={busy}
                        onClick={() => setConfirmingRemove(false)}
                      >
                        Cancel
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className={`btn ${styles.target} ${styles.danger}`}
                      onClick={() => setConfirmingRemove(true)}
                    >
                      Remove from database
                    </button>
                    <p className={styles.muted}>
                      Removes the name, aliases and bio. Labels in meetings stay as they are.
                    </p>
                  </>
                )}
              </div>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
