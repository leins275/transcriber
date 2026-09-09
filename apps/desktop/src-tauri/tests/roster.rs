//! T1: the per-project speaker roster (`<vault root>/<PROJECT>/roster.json`)
//! driven through the two Tauri command handlers against a real `tempfile`
//! vault — the `tests/common` harness wires the same `AppState` `lib.rs`
//! wires in production, so nothing here is mocked: the roster handlers are
//! controllers over the filesystem and the filesystem is the thing under
//! test (blueprint: FR-1, FR-2, FR-3).

// The shared harness serves several test binaries; this one has no jobs to
// wait on and no recordings to write, so part of it is unused here.
#[allow(dead_code)]
mod common;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use transcriber_desktop_lib::commands::chats::{
    list_chats_handler, save_chat_handler, ChatConversationInput, ChatMessageInput,
};
use transcriber_desktop_lib::commands::list_vault_handler;
use transcriber_desktop_lib::commands::roster::{
    read_project_roster_handler, save_project_roster_handler, ProjectRosterInput, RosterMode,
};
use transcriber_desktop_lib::commands::AppState;
use transcriber_desktop_lib::error::ErrorKind;
use transcriber_desktop_lib::service::fake::FakeService;

use common::{build_state, new_tempdir, run};

/// The FR-2 normalization fixture, shared **verbatim** with
/// `apps/desktop/src/lib/roster.test.ts`: trimmed, blanks dropped,
/// case-insensitive duplicates collapsed onto the first spelling, original
/// order preserved. If these two literals ever diverge between the Rust and
/// the TypeScript file, the editor is showing a list the backend would
/// silently rewrite on save.
const MESSY_NAMES: [&str; 5] = ["  Anna ", "anna", "", "Maxim", "Maxim"];
const NORMALIZED_NAMES: [&str; 2] = ["Anna", "Maxim"];

/// Creates `root/<project>` — a project is simply a directory under the
/// meetings root, exactly as the vault listing sees one.
fn make_project(root: &Path, project: &str) -> PathBuf {
    let dir = root.join(project);
    std::fs::create_dir_all(&dir).expect("create the project directory");
    dir
}

/// Creates a real meeting folder so the project also lists as one.
fn make_meeting(root: &Path, project: &str, meeting: &str) {
    let dir = make_project(root, project).join(meeting);
    std::fs::create_dir_all(&dir).expect("create the meeting directory");
    std::fs::write(dir.join("source.mp4"), b"bytes").expect("write the fixture recording");
}

fn roster_input(mode: RosterMode, names: &[&str]) -> ProjectRosterInput {
    ProjectRosterInput {
        mode,
        names: names.iter().map(|name| (*name).to_string()).collect(),
    }
}

fn state_over(root: &Path) -> AppState {
    build_state(root.to_path_buf(), Arc::new(FakeService::new()))
}

fn roster_path(root: &Path, project: &str) -> PathBuf {
    root.join(project).join("roster.json")
}

// -- FR-1: the file contract -------------------------------------------------

#[test]
fn a_project_that_has_never_had_a_roster_reads_as_the_open_roster() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        let view = read_project_roster_handler(&state, "ACME")
            .await
            .expect("a project with no roster file is a normal state, not an error");

        assert_eq!(view.mode, RosterMode::Open);
        assert!(
            view.names.is_empty(),
            "expected no names, got {:?}",
            view.names
        );
    });
}

#[test]
fn a_saved_roster_round_trips_and_lands_in_the_project_folder() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        save_project_roster_handler(
            &state,
            "ACME",
            roster_input(RosterMode::Roster, &["Anna", "Maxim"]),
        )
        .await
        .expect("saving a roster for a real project must succeed");

        let view = read_project_roster_handler(&state, "ACME")
            .await
            .expect("read the roster back");
        assert_eq!(view.mode, RosterMode::Roster);
        assert_eq!(view.names, ["Anna", "Maxim"]);

        let stored: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(roster_path(root.path(), "ACME"))
                .expect("roster.json must sit at project level"),
        )
        .expect("roster.json must be readable JSON");
        assert_eq!(stored["schema_version"], 1);
        assert_eq!(stored["mode"], "roster");
        assert_eq!(stored["names"], serde_json::json!(["Anna", "Maxim"]));
    });
}

#[test]
fn an_unparseable_roster_file_reads_as_the_open_roster() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        std::fs::write(roster_path(root.path(), "ACME"), b"{ this is not json")
            .expect("write the corrupt roster file");
        let state = state_over(root.path());

        let view = read_project_roster_handler(&state, "ACME")
            .await
            .expect("a corrupt roster degrades to open mode rather than failing the call");

        assert_eq!(view.mode, RosterMode::Open);
        assert!(
            view.names.is_empty(),
            "expected no names, got {:?}",
            view.names
        );
    });
}

#[test]
fn a_roster_file_beyond_the_read_cap_reads_as_the_open_roster() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        // Valid JSON, 65 KiB of it: what is refused here is the size, not
        // the syntax.
        let oversized = format!(
            "{{\"schema_version\":1,\"mode\":\"roster\",\"names\":[\"{}\"]}}",
            "A".repeat(65 * 1024)
        );
        std::fs::write(roster_path(root.path(), "ACME"), oversized.as_bytes())
            .expect("write the oversized roster file");
        let state = state_over(root.path());

        let view = read_project_roster_handler(&state, "ACME")
            .await
            .expect("an oversized roster degrades to open mode rather than failing the call");

        assert_eq!(view.mode, RosterMode::Open);
        assert!(
            view.names.is_empty(),
            "expected no names, got {:?}",
            view.names
        );
    });
}

#[test]
fn a_saved_roster_never_surfaces_as_a_meeting_in_the_vault_listing() {
    run(async {
        let root = new_tempdir();
        make_meeting(root.path(), "ACME", "260812 - Security issue");
        let state = state_over(root.path());
        save_project_roster_handler(
            &state,
            "ACME",
            roster_input(RosterMode::Roster, &["Anna", "Maxim"]),
        )
        .await
        .expect("save the roster");

        let entries = list_vault_handler(&state).await.expect("list the vault");

        assert_eq!(
            entries
                .iter()
                .map(|entry| entry.meeting_name.clone())
                .collect::<Vec<String>>(),
            ["260812 - Security issue"]
        );
    });
}

// -- FR-2: normalization and limits ------------------------------------------

#[test]
fn saving_trims_blank_and_case_insensitively_duplicate_names() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        let saved = save_project_roster_handler(
            &state,
            "ACME",
            roster_input(RosterMode::Roster, &MESSY_NAMES),
        )
        .await
        .expect("save the messy roster");

        assert_eq!(saved.names, NORMALIZED_NAMES);
        let stored: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(roster_path(root.path(), "ACME")).expect("read roster.json"),
        )
        .expect("roster.json must be readable JSON");
        assert_eq!(stored["names"], serde_json::json!(["Anna", "Maxim"]));
    });
}

#[test]
fn a_name_longer_than_two_hundred_characters_is_refused_and_nothing_is_written() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        let err = save_project_roster_handler(
            &state,
            "ACME",
            roster_input(RosterMode::Roster, &["Anna", &"N".repeat(201)]),
        )
        .await
        .expect_err("a 201-character name must be refused");

        assert_eq!(err.kind(), ErrorKind::InvalidArgument);
        assert!(
            !roster_path(root.path(), "ACME").exists(),
            "a refused save must leave no roster file behind"
        );
    });
}

#[test]
fn more_than_five_hundred_names_are_refused_and_nothing_is_written() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());
        let crowd: Vec<String> = (0..501).map(|index| format!("Speaker {index}")).collect();

        let err = save_project_roster_handler(
            &state,
            "ACME",
            ProjectRosterInput {
                mode: RosterMode::Roster,
                names: crowd,
            },
        )
        .await
        .expect_err("a 501-name roster must be refused");

        assert_eq!(err.kind(), ErrorKind::InvalidArgument);
        assert!(
            !roster_path(root.path(), "ACME").exists(),
            "a refused save must leave no roster file behind"
        );
    });
}

// -- FR-3: the project argument is untrusted IPC input ------------------------

#[test]
fn reading_a_roster_for_anything_that_is_not_a_project_is_refused() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        for bogus in [
            "unsorted", "chats", "reports", "", ".", "..", "A/B", "A\\B", "A:B", "NOPE",
        ] {
            let Err(err) = read_project_roster_handler(&state, bogus).await else {
                panic!("project {bogus:?} must be refused");
            };
            assert_eq!(
                err.kind(),
                ErrorKind::InvalidArgument,
                "project {bogus:?} must be refused as an invalid argument"
            );
        }
    });
}

#[test]
fn saving_a_roster_never_creates_a_project() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());

        let err = save_project_roster_handler(
            &state,
            "NOPE",
            roster_input(RosterMode::Roster, &["Anna"]),
        )
        .await
        .expect_err("saving into a project that does not exist must be refused");

        assert_eq!(err.kind(), ErrorKind::InvalidArgument);
        assert!(
            !root.path().join("NOPE").exists(),
            "a refused save must not create the project directory"
        );
    });
}

#[test]
fn with_no_meetings_root_configured_reading_a_roster_is_not_configured() {
    run(async {
        let root = new_tempdir();
        make_project(root.path(), "ACME");
        let state = state_over(root.path());
        state.settings.write().await.meetings_root = None;

        let err = read_project_roster_handler(&state, "ACME")
            .await
            .expect_err("without a vault there is nowhere to read a roster from");

        assert_eq!(err.kind(), ErrorKind::NotConfigured);
    });
}

#[test]
fn saved_chats_still_list_after_the_project_lookup_is_shared_with_the_roster() {
    run(async {
        let root = new_tempdir();
        make_meeting(root.path(), "ACME", "260812 - Security issue");
        let state = state_over(root.path());
        save_chat_handler(
            &state,
            "ACME",
            ChatConversationInput {
                id: None,
                title: "Дедлайн и решения".to_string(),
                messages: vec![ChatMessageInput {
                    role: "user".to_string(),
                    content: "когда дедлайн?".to_string(),
                    sources: vec![],
                }],
            },
        )
        .await
        .expect("save a chat");

        let listed = list_chats_handler(&state, "ACME")
            .await
            .expect("the project's chats must still list");

        assert_eq!(
            listed
                .iter()
                .map(|summary| summary.title.clone())
                .collect::<Vec<String>>(),
            ["Дедлайн и решения"]
        );
    });
}
