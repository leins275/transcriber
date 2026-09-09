//! T4: the job registry fills `max_speakers` and the strict matching
//! threshold from the meeting's **project roster**, at the moment the job is
//! submitted to the service (blueprint 260909-roster-bounds-diarization,
//! FR-2 and FR-7 c3/c4).
//!
//! Everything here is driven through the crate's public surface exactly the
//! way `tests/e2e_flow.rs` and `tests/roster.rs` drive it: a real
//! `AppState` (real `JobRegistry`, real ingest, real `commands::roster`
//! reader) over a `tempfile` vault, with `service::fake::FakeService`
//! standing in for the out-of-process transcription service. The fake is
//! the only test double — it *is* the boundary this task's contract is
//! about, and the request it received is the observable end result these
//! tests assert on. `roster.json` is written by hand as bytes on disk,
//! because "what the operator's file says" is the input under test, not
//! what the save handler would have normalized it into.

// The shared harness serves several test binaries; not every piece of it is
// used here.
#[allow(dead_code)]
mod common;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use transcriber_desktop_lib::commands::meetings::transcribe_vault_entry_handler;
use transcriber_desktop_lib::commands::speakers::{
    diarize_labelled_meetings_handler, diarize_vault_entry_handler,
};
use transcriber_desktop_lib::commands::{enqueue_paths_handler, list_vault_handler, AppState};
use transcriber_desktop_lib::config::Settings;
use transcriber_desktop_lib::jobs::JobState;
use transcriber_desktop_lib::service::fake::{FakeService, FakeTiming};
use transcriber_desktop_lib::service::{LlmJobKind, LlmSubmitRequest, SubmitRequest};

use common::{build_state, new_tempdir, run, wait_for_terminal, write_recording};

// -- harness ---------------------------------------------------------------

/// The registry polls the service on the production interval, so every job
/// here costs one poll of wall clock; the timeout is generous enough that a
/// slow CI host never turns a correct implementation red.
const JOB_TIMEOUT: Duration = Duration::from_secs(20);

/// A drop whose name files it under project `ELS`.
const SORTED_DROP: &str = "ELS - 260812 - Security issue.mp4";

/// A drop whose name carries no project code, so it lands under `unsorted`.
const UNSORTED_DROP: &str = "random meeting.mp4";

/// Zero extra queued/running polls before a scripted job resolves.
fn instant_timing() -> FakeTiming {
    FakeTiming {
        queued_polls: 0,
        running_polls: 0,
    }
}

/// Writes `<root>/<project>/roster.json` verbatim — the shape the shipped
/// save handler writes (`{schema_version, mode, names}`), spelled out here
/// so a test can also write shapes the handler never would (an empty strict
/// roster, corrupt JSON).
fn write_roster(root: &Path, project: &str, mode: &str, names: &[&str]) {
    let dir = root.join(project);
    std::fs::create_dir_all(&dir).expect("create the project directory");
    let body = serde_json::json!({
        "schema_version": 1,
        "mode": mode,
        "names": names,
    });
    std::fs::write(
        dir.join(vault::ROSTER_FILE_NAME),
        serde_json::to_vec_pretty(&body).expect("serialize the roster"),
    )
    .expect("write roster.json");
}

/// Creates a filed meeting that is transcribed and still has its recording
/// — the state both "Identify speakers" and re-transcribe require.
fn make_transcribed_meeting(root: &Path, project: &str, meeting: &str) -> PathBuf {
    let dir = root.join(project).join(meeting);
    std::fs::create_dir_all(&dir).expect("create the meeting directory");
    std::fs::write(dir.join("source.mp4"), b"recording bytes").expect("write the recording");
    std::fs::write(
        dir.join(vault::TRANSCRIPT_FILE_NAME),
        br#"{"schema_version":1,"segments":[]}"#,
    )
    .expect("write transcript.json");
    dir
}

/// Adds the one hand-made speaker label that makes a meeting a candidate
/// for the vault-wide "Identify speakers in labelled meetings" backfill.
fn add_hand_made_label(meeting_dir: &Path) {
    std::fs::write(
        meeting_dir.join("speakers.json"),
        br#"{"schema_version":1,"assignments":{"0":"Anna"}}"#,
    )
    .expect("write speakers.json");
}

/// Drops `file_name` into the app and waits for its transcription to
/// finish, so the submission it produced is on the fake by the time the
/// assertions run.
async fn drop_and_wait(state: &AppState, downloads: &Path, file_name: &str) {
    let source = write_recording(downloads, file_name, b"recording bytes");
    let snapshots = enqueue_paths_handler(state, vec![source.to_string_lossy().into_owned()])
        .await
        .expect("enqueue must succeed");
    let terminal = wait_for_terminal(state, &snapshots[0].id, JOB_TIMEOUT).await;
    assert_eq!(
        terminal.state,
        JobState::Done,
        "the fixture drop must transcribe cleanly before its submission is inspected"
    );
}

/// The entry id `list_vault` reports for the meeting named `meeting_name`.
async fn entry_id_of(state: &AppState, meeting_name: &str) -> String {
    list_vault_handler(state)
        .await
        .expect("list the vault")
        .into_iter()
        .find(|entry| entry.meeting_name == meeting_name)
        .unwrap_or_else(|| panic!("no vault entry named {meeting_name:?}"))
        .id
}

/// The single transcribe submission the fake received.
fn only_submission(fake: &FakeService) -> SubmitRequest {
    let mut submissions = fake.submissions();
    assert_eq!(
        submissions.len(),
        1,
        "expected exactly one transcribe submission, got {submissions:?}"
    );
    submissions.remove(0)
}

/// Waits until the fake has received `count` derived-job submissions.
async fn llm_submissions(
    fake: &FakeService,
    count: usize,
    timeout: Duration,
) -> Vec<LlmSubmitRequest> {
    let deadline = Instant::now() + timeout;
    loop {
        let submissions = fake.llm_submissions();
        if submissions.len() >= count {
            return submissions;
        }
        assert!(
            Instant::now() < deadline,
            "the fake received {} derived submissions, expected {count}",
            submissions.len()
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Compares a threshold that reached the wire against a hardcoded expected
/// value. The derived strict values are the result of binary floating-point
/// subtraction (`0.35 - 0.1`), which lands one ulp away from the decimal
/// number a person would write down, so an exact `==` would be asserting
/// IEEE-754 rounding rather than the behaviour.
#[track_caller]
fn assert_threshold(actual: Option<f64>, expected: f64) {
    let value = actual.expect("the submission must carry a strict matching threshold");
    assert!(
        (value - expected).abs() < 1e-9,
        "expected a strict matching threshold of ~{expected}, got {value}"
    );
}

/// An `AppState` wired like [`common::build_state`] but over caller-chosen
/// settings — the only way to observe how a hand-edited `config.json` key
/// reaches the wire, since the strict threshold is read once at startup.
fn build_state_with_settings(
    root: &Path,
    service: Arc<FakeService>,
    settings: Settings,
) -> AppState {
    AppState::new_with(
        root.to_path_buf(),
        root.to_path_buf(),
        settings,
        root.to_path_buf(),
        service,
        None,
        false,
        Arc::new(common::RecordingSink::default()),
        Arc::new(common::RecordingStatusSink::default()),
        Arc::new(common::NeverSpawnSidecarController),
        Arc::new(common::RecordingRevealer::default()),
    )
}

// -- FR-2 / FR-7: a strict roster bounds the transcribe submission ---------

#[test]
fn a_drop_into_a_strict_roster_project_is_capped_at_the_roster_size_and_matched_more_leniently() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim", "Pavel"]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, Some(3));
        assert_threshold(submission.speaker_match_threshold, 0.4);
    });
}

#[test]
fn a_drop_into_an_open_roster_project_carries_no_cap_and_no_threshold() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "open", &["Anna", "Maxim", "Pavel"]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, None);
        assert_eq!(submission.speaker_match_threshold, None);
    });
}

#[test]
fn the_cap_counts_the_roster_names_the_way_the_roster_editor_normalizes_them() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        // Trimmed, blanks dropped, case-insensitive duplicates collapsed:
        // one person, so one voice may speak — not three.
        write_roster(root.path(), "ELS", "roster", &["  Anna ", "anna", ""]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        assert_eq!(only_submission(&fake).max_speakers, Some(1));
    });
}

#[test]
fn a_strict_roster_holding_no_names_carries_no_cap_and_no_threshold() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &[]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, None);
        assert_eq!(submission.speaker_match_threshold, None);
    });
}

#[test]
fn an_unparseable_roster_file_carries_no_cap_and_no_threshold() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        std::fs::create_dir_all(root.path().join("ELS")).expect("create the project directory");
        std::fs::write(
            root.path().join("ELS").join(vault::ROSTER_FILE_NAME),
            b"{ this is not json",
        )
        .expect("write the corrupt roster file");
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, None);
        assert_eq!(submission.speaker_match_threshold, None);
    });
}

#[test]
fn a_recording_filed_under_unsorted_carries_no_cap_even_though_unsorted_holds_a_strict_roster() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        // `unsorted/` is not a project: a roster file someone dropped there
        // by hand must never bound a recording that lands in it.
        write_roster(
            root.path(),
            vault::UNSORTED_DIR_NAME,
            "roster",
            &["Anna", "Maxim"],
        );
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), UNSORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, None);
        assert_eq!(submission.speaker_match_threshold, None);
    });
}

// -- FR-2 c3 / c5: the other two paths into the diarization pass -----------

#[test]
fn identify_speakers_on_a_filed_meeting_carries_the_strict_rosters_cap_and_threshold() {
    run(async {
        let root = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim"]);
        make_transcribed_meeting(root.path(), "ELS", "260812 - Security issue");
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());
        let entry_id = entry_id_of(&state, "260812 - Security issue").await;

        let snapshot = diarize_vault_entry_handler(&state, &entry_id)
            .await
            .expect("identify speakers must enqueue a diarize job");

        wait_for_terminal(&state, &snapshot.id, JOB_TIMEOUT).await;
        let submissions = fake.llm_submissions();
        assert_eq!(
            submissions.len(),
            1,
            "expected exactly one derived submission, got {submissions:?}"
        );
        assert_eq!(submissions[0].kind, LlmJobKind::Diarize);
        assert_eq!(submissions[0].max_speakers, Some(2));
        assert_threshold(submissions[0].speaker_match_threshold, 0.4);
    });
}

#[test]
fn re_transcribing_a_filed_meeting_resolves_the_roster_the_same_way_a_fresh_drop_does() {
    run(async {
        let root = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim"]);
        make_transcribed_meeting(root.path(), "ELS", "260812 - Security issue");
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());
        let entry_id = entry_id_of(&state, "260812 - Security issue").await;

        let snapshot = transcribe_vault_entry_handler(&state, &entry_id, None)
            .await
            .expect("re-transcribe must enqueue a transcribe job");

        wait_for_terminal(&state, &snapshot.id, JOB_TIMEOUT).await;
        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, Some(2));
        assert_threshold(submission.speaker_match_threshold, 0.4);
    });
}

// -- FR-2 c4: the derived stages never carry bounds ------------------------

#[test]
fn the_drop_chains_summarize_and_export_stages_carry_no_cap_and_no_threshold() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim", "Pavel"]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state(root.path().to_path_buf(), fake.clone());

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let chained = llm_submissions(&fake, 2, JOB_TIMEOUT).await;
        let kinds: Vec<LlmJobKind> = chained.iter().map(|request| request.kind).collect();
        assert_eq!(kinds, [LlmJobKind::Summarize, LlmJobKind::Export]);
        assert!(
            chained.iter().all(|request| request.max_speakers.is_none()
                && request.speaker_match_threshold.is_none()),
            "a derived stage runs no diarization pass, so it must carry neither field: {chained:?}"
        );
    });
}

// -- FR-2 c6: the roster is read at submit time, not at enqueue time -------
//
// The ordering this needs cannot come from timing: the registry's serial
// worker hands job two to `process_one` as soon as job one's `submit_llm`
// returns. `FakeService::hold_llm_submissions` supplies it instead --
// job one blocks in the fake, the test rewrites the roster, the release is
// what lets job two run at all.

#[test]
fn a_backfill_job_still_queued_when_the_roster_grows_is_submitted_against_the_new_roster() {
    run(async {
        let root = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim"]);
        for meeting in ["260101 - Kickoff", "260102 - Review"] {
            let dir = make_transcribed_meeting(root.path(), "ELS", meeting);
            add_hand_made_label(&dir);
        }
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        // Without this the two backfill jobs reach the service microseconds
        // apart (`submit_llm_and_poll` spawns the poll loop and returns), so
        // a roster rewritten from the test thread would be racing the
        // worker. The hold blocks the *first* submission inside the fake,
        // which pins the second job as un-submitted -- and therefore its
        // roster as unread -- until the test releases it.
        fake.hold_llm_submissions();
        let state = build_state(root.path().to_path_buf(), fake.clone());

        let queued = diarize_labelled_meetings_handler(&state)
            .await
            .expect("the backfill must queue both labelled meetings");
        assert_eq!(queued, 2);

        // The first meeting is now parked *inside* `submit_llm`, which is
        // what keeps the serial worker from ever reaching the second one:
        // `process_one` does not return until this call does. Its cap is the
        // roster as it stood at ITS submit time -- two names.
        let held = llm_submissions(&fake, 1, JOB_TIMEOUT).await;
        assert_eq!(
            held.len(),
            1,
            "the hold must keep the second submission from happening: {held:?}"
        );
        assert_eq!(held[0].max_speakers, Some(2));

        // Grow the roster while the second job is provably still queued,
        // then let the worker go. Everything the second job reads happens
        // after this write, so an enqueue-time read would hand it the old
        // cap of 2 and a submit-time read the new cap of 3 -- no timing.
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim", "Pavel"]);
        fake.release_llm_submissions();

        let submissions = llm_submissions(&fake, 2, JOB_TIMEOUT).await;
        assert_eq!(
            submissions[1].max_speakers,
            Some(3),
            "the second job must carry the roster as it stood when it was submitted"
        );
    });
}

// -- FR-7 c2 / c4: where the strict threshold's value comes from -----------

#[test]
fn a_configured_strict_threshold_is_what_reaches_the_wire() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim", "Pavel"]);
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state_with_settings(
            root.path(),
            fake.clone(),
            Settings {
                meetings_root: Some(root.path().to_string_lossy().into_owned()),
                speaker_match_threshold_strict: Some(0.3),
                ..Settings::default()
            },
        );

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        let submission = only_submission(&fake);
        assert_eq!(submission.max_speakers, Some(3));
        assert_threshold(submission.speaker_match_threshold, 0.3);
    });
}

#[test]
fn without_a_strict_key_the_threshold_is_derived_from_the_services_own_matching_threshold() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        write_roster(root.path(), "ELS", "roster", &["Anna", "Maxim", "Pavel"]);
        let mut extra = serde_json::Map::new();
        extra.insert(
            "speaker_match_threshold".to_string(),
            serde_json::json!(0.35),
        );
        let fake = Arc::new(FakeService::with_timing(instant_timing()));
        let state = build_state_with_settings(
            root.path(),
            fake.clone(),
            Settings {
                meetings_root: Some(root.path().to_string_lossy().into_owned()),
                extra,
                ..Settings::default()
            },
        );

        drop_and_wait(&state, downloads.path(), SORTED_DROP).await;

        assert_threshold(only_submission(&fake).speaker_match_threshold, 0.25);
    });
}
