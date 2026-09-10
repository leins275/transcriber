//! T5: the app carries the optional meeting **type** from the UI down to
//! the vault (blueprint 260910-meeting-type-in-filename, FR-7).
//!
//! Driven through the crate's public surface exactly the way
//! `tests/e2e_flow.rs` and `tests/roster_bounds.rs` drive it: a real
//! `AppState` (real `JobRegistry`, real ingest, real
//! `vault::rename_meeting`) over a `tempfile` vault root, with
//! `service::fake::FakeService` standing in for the out-of-process
//! transcription service — the only test double here, and only because it
//! is the one dependency this app does not own. Nothing in-process is
//! mocked: the folder on disk and the view the handler returns are the
//! observable end results these tests assert on.

// The shared harness serves several test binaries; not every piece of it is
// used here.
#[allow(dead_code)]
mod common;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use transcriber_desktop_lib::commands::meetings::update_vault_entry_handler;
use transcriber_desktop_lib::commands::{enqueue_paths_handler, list_vault_handler, AppState};
use transcriber_desktop_lib::jobs::{JobSnapshot, JobState};
use transcriber_desktop_lib::service::fake::FakeService;

use common::{build_state, new_tempdir, run, wait_for_terminal, write_recording};

// -- harness ---------------------------------------------------------------

/// The registry polls the service on the production interval and the fake
/// spends `FakeTiming::default`'s few polls per job, so a drop costs a
/// handful of seconds of wall clock; generous enough that a slow CI host
/// never turns a correct implementation red.
const JOB_TIMEOUT: Duration = Duration::from_secs(20);

/// An `AppState` over `root`, wired the way `lib.rs` wires it.
///
/// The fake reports **no LLM model installed** on purpose: with one, a
/// finished transcription chains a `summarize` over the same meeting folder
/// (`jobs::queue_follow_up`), and `update_vault_entry` rightly refuses to
/// rename a folder a queued job is about to read. These tests are about
/// naming, not the chain, so they use the LLM-less install whose chain ends
/// at transcription -- leaving the meeting genuinely idle.
fn state_over(root: &Path) -> AppState {
    build_state(
        root.to_path_buf(),
        Arc::new(FakeService::with_llm_model_absent()),
    )
}

/// Drops `file_name` into the app and waits for its transcription to
/// finish. Because `state_over`'s service reports no LLM model, that
/// terminal state is the whole chain: the meeting is filed and has no
/// active job by the time a rename is attempted (`update_vault_entry`
/// refuses a meeting with one).
async fn drop_and_wait(state: &AppState, downloads: &Path, file_name: &str) -> JobSnapshot {
    let source = write_recording(downloads, file_name, b"recording bytes");
    let snapshots = enqueue_paths_handler(state, vec![source.to_string_lossy().into_owned()])
        .await
        .expect("enqueue must succeed");
    wait_for_terminal(state, &snapshots[0].id, JOB_TIMEOUT).await
}

/// The single entry `list_vault` reports.
async fn only_entry(state: &AppState) -> transcriber_desktop_lib::commands::VaultMeetingView {
    let mut entries = list_vault_handler(state).await.expect("list the vault");
    assert_eq!(
        entries.len(),
        1,
        "expected exactly one filed meeting, got {entries:?}"
    );
    entries.remove(0)
}

/// `<root>/ELS/<meeting_name>`.
fn els_meeting(root: &Path, meeting_name: &str) -> PathBuf {
    root.join("ELS").join(meeting_name)
}

// -- FR-7 c1: a typed drop is filed under a typed folder --------------------

#[test]
fn a_drop_whose_name_carries_a_type_is_filed_in_a_typed_meeting_folder() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());

        let done = drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue - Standup.mp4",
        )
        .await;

        assert_eq!(done.state, JobState::Done);
        assert_eq!(done.classification.as_deref(), Some("sorted"));
        let entry = only_entry(&state).await;
        assert_eq!(entry.project.as_deref(), Some("ELS"));
        assert_eq!(entry.meeting_name, "260812 - Security issue - Standup");
    });
}

// -- FR-7 c2: five sections route to unsorted -------------------------------

#[test]
fn a_drop_with_a_hyphen_inside_a_section_is_filed_under_unsorted() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());

        let done = drop_and_wait(&state, downloads.path(), "ELS - 260812 - a - b - c.mp4").await;

        assert_eq!(done.classification.as_deref(), Some("unsorted"));
        let entry = only_entry(&state).await;
        assert_eq!(entry.project, None);
        assert!(
            entry.meeting_name.ends_with("ELS - 260812 - a - b - c"),
            "the unsorted folder keeps the dropped stem verbatim, got {:?}",
            entry.meeting_name
        );
    });
}

#[test]
fn an_unsorted_drop_reports_why_its_name_did_not_conform() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());

        let done = drop_and_wait(&state, downloads.path(), "ELS - 260812 - a - b - c.mp4").await;

        let message = done
            .message
            .as_deref()
            .expect("an unsorted job must say why the name did not conform");
        assert!(
            message.contains("separator"),
            "expected the vault's rejection reason, got {message:?}"
        );
    });
}

// -- FR-7 c3: the rename form gives, changes and strips the type ------------

#[test]
fn giving_an_untyped_meeting_a_type_renames_its_folder() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());
        drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
        )
        .await;
        let entry = only_entry(&state).await;

        let view = update_vault_entry_handler(
            &state,
            &entry.id,
            Some("ELS".to_string()),
            "260812",
            "Security issue",
            Some("Retro".to_string()),
        )
        .await
        .expect("the rename must succeed");

        assert_eq!(view.meeting_name, "260812 - Security issue - Retro");
        assert!(
            els_meeting(root.path(), "260812 - Security issue - Retro")
                .join("source.mp4")
                .is_file(),
            "the recording must travel to the typed folder"
        );
        assert!(
            !els_meeting(root.path(), "260812 - Security issue").exists(),
            "the untyped folder must be gone"
        );
    });
}

#[test]
fn omitting_the_type_strips_it_from_the_folder_name() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());
        drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue - Standup.mp4",
        )
        .await;
        let entry = only_entry(&state).await;

        let view = update_vault_entry_handler(
            &state,
            &entry.id,
            Some("ELS".to_string()),
            "260812",
            "Security issue",
            None,
        )
        .await
        .expect("the rename must succeed");

        assert_eq!(view.meeting_name, "260812 - Security issue");
        assert!(
            els_meeting(root.path(), "260812 - Security issue")
                .join("source.mp4")
                .is_file(),
            "the recording must travel to the untyped folder"
        );
        assert!(
            !els_meeting(root.path(), "260812 - Security issue - Standup").exists(),
            "the typed folder must be gone"
        );
    });
}

#[test]
fn a_hyphen_in_the_type_is_refused_and_the_folder_stays_where_it_was() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());
        drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
        )
        .await;
        let entry = only_entry(&state).await;

        let err = update_vault_entry_handler(
            &state,
            &entry.id,
            Some("ELS".to_string()),
            "260812",
            "Security issue",
            Some("Re-tro".to_string()),
        )
        .await
        .expect_err("a hyphen in the type must be refused");

        assert!(
            err.message().contains("reserved"),
            "expected the reserved-separator refusal, got {:?}",
            err.message()
        );
        assert!(
            els_meeting(root.path(), "260812 - Security issue")
                .join("source.mp4")
                .is_file(),
            "nothing may move when the type is refused"
        );
    });
}

#[test]
fn a_hyphen_in_the_title_is_refused_and_the_folder_stays_where_it_was() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());
        drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
        )
        .await;
        let entry = only_entry(&state).await;

        let err = update_vault_entry_handler(
            &state,
            &entry.id,
            Some("ELS".to_string()),
            "260812",
            "Sync - part 2",
            None,
        )
        .await
        .expect_err("a hyphen in the title must be refused");

        assert!(
            err.message().contains("reserved"),
            "expected the reserved-separator refusal, got {:?}",
            err.message()
        );
        assert!(
            els_meeting(root.path(), "260812 - Security issue")
                .join("source.mp4")
                .is_file(),
            "nothing may move when the title is refused"
        );
    });
}

#[test]
fn renaming_a_typed_meeting_by_its_entry_id_keeps_the_type() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let state = state_over(root.path());
        drop_and_wait(
            &state,
            downloads.path(),
            "ELS - 260812 - Security issue - Retro.mp4",
        )
        .await;
        let entry = only_entry(&state).await;

        let view = update_vault_entry_handler(
            &state,
            &entry.id,
            Some("ELS".to_string()),
            "260812",
            "Weekly sync",
            Some("Retro".to_string()),
        )
        .await
        .expect("renaming only the title must succeed");

        assert_eq!(view.id, entry.id);
        assert_eq!(view.meeting_name, "260812 - Weekly sync - Retro");
        assert!(
            els_meeting(root.path(), "260812 - Weekly sync - Retro")
                .join("source.mp4")
                .is_file(),
            "the recording must travel to the renamed typed folder"
        );
    });
}
