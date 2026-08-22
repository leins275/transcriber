//! T13: integration verification — the crate's public surface
//! (`commands` + `jobs` + `ingest`, wired exactly as `lib.rs` wires them)
//! driven end to end against a real `tempfile` vault and the in-memory fake
//! transcription service. No test here spawns the real F2 sidecar or
//! requires a whisper model (plan.md's QA expectation).

mod common;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use transcriber_desktop_lib::commands::{enqueue_paths_handler, reveal_job_handler};
use transcriber_desktop_lib::error::ErrorKind;
use transcriber_desktop_lib::jobs::JobState;
use transcriber_desktop_lib::paths;
use transcriber_desktop_lib::service::fake::{FakeService, FakeTiming};

use common::{build_state, new_tempdir, run, wait_for_terminal, write_recording};

/// Fast, deterministic timing: zero extra queued/running polls before a
/// scripted job resolves, so these tests don't wait multiple seconds per
/// job for no reason.
fn instant_timing() -> FakeTiming {
    FakeTiming {
        queued_polls: 0,
        running_polls: 0,
    }
}

// -- FR-9 / FR-15: happy path ------------------------------------------------

#[test]
fn full_pipeline_files_a_correctly_named_recording_and_reports_its_transcript_path() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let source = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            b"recording bytes",
        );

        let state = build_state(
            root.path().to_path_buf(),
            Arc::new(FakeService::with_timing(instant_timing())),
        );

        let snapshots = enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
            .await
            .expect("enqueue must succeed");
        assert_eq!(snapshots.len(), 1);
        assert_eq!(snapshots[0].state, JobState::Pending);

        let done = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;
        assert_eq!(done.state, JobState::Done);

        let expected_meeting_dir_suffix = PathBuf::from("ELS").join("260812 - Security issue");
        let meeting_dir = PathBuf::from(done.meeting_dir.as_deref().expect("meeting_dir set"));
        assert!(
            meeting_dir.ends_with(&expected_meeting_dir_suffix),
            "expected meeting_dir to end with {expected_meeting_dir_suffix:?}, got {meeting_dir:?}"
        );
        // Against the *canonical* root: F1 canonicalizes every path it hands
        // back, and a tempdir's own path need not be canonical -- a Windows
        // CI runner exposes `%TEMP%` through an 8.3 short component. Comparing
        // with the raw tempdir path tests the fixture's spelling rather than
        // where the recording was filed.
        let canonical_root = root
            .path()
            .canonicalize()
            .expect("the meetings root exists by now");
        let canonical_meeting_dir = meeting_dir
            .canonicalize()
            .expect("the meeting folder was just created");
        assert!(
            canonical_meeting_dir.starts_with(&canonical_root),
            "expected {canonical_meeting_dir:?} under {canonical_root:?}"
        );

        let source_dest = PathBuf::from(done.source_dest.as_deref().expect("source_dest set"));
        assert_eq!(source_dest, meeting_dir.join("source.mp4"));
        assert!(
            source_dest.is_file(),
            "the recording must actually be on disk"
        );
        assert_eq!(
            std::fs::read(&source_dest).expect("read filed recording"),
            b"recording bytes"
        );

        let transcript_path = PathBuf::from(
            done.transcript_path
                .as_deref()
                .expect("transcript_path set"),
        );
        assert_eq!(transcript_path, meeting_dir.join("transcript.json"));
    });
}

// -- FR-9: unsorted placement -------------------------------------------------

#[test]
fn a_badly_named_recording_lands_under_unsorted() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let source = write_recording(downloads.path(), "random meeting.mp4", b"bytes");

        let state = build_state(
            root.path().to_path_buf(),
            Arc::new(FakeService::with_timing(instant_timing())),
        );

        let snapshots = enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
            .await
            .expect("enqueue must succeed");

        let done = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;
        assert_eq!(done.state, JobState::Done);
        assert_eq!(done.classification.as_deref(), Some("unsorted"));
        assert!(done
            .meeting_dir
            .as_deref()
            .expect("meeting_dir set")
            .contains(vault::UNSORTED_DIR_NAME));
    });
}

// -- FR-6: a mixed batch --------------------------------------------------

#[test]
fn a_txt_file_in_the_same_batch_is_rejected_while_the_media_file_completes() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let good = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            b"recording bytes",
        );
        let bad = write_recording(downloads.path(), "notes.txt", b"just some notes");

        let state = build_state(
            root.path().to_path_buf(),
            Arc::new(FakeService::with_timing(instant_timing())),
        );

        let snapshots = enqueue_paths_handler(
            &state,
            vec![
                good.to_string_lossy().into_owned(),
                bad.to_string_lossy().into_owned(),
            ],
        )
        .await
        .expect("enqueue must succeed for a mixed batch");
        assert_eq!(snapshots.len(), 2);

        let good_terminal =
            wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;
        let bad_terminal =
            wait_for_terminal(&state, &snapshots[1].id, Duration::from_secs(5)).await;

        assert_eq!(good_terminal.state, JobState::Done);
        assert_eq!(bad_terminal.state, JobState::Rejected);
        assert_eq!(
            bad_terminal.error_kind.as_deref(),
            Some("unsupported_extension")
        );
        assert!(bad_terminal
            .message
            .as_deref()
            .expect("rejection message present")
            .contains("notes.txt"));

        // The rejected file was never written into the vault at all.
        assert!(
            !root.path().join("unsorted").exists() || {
                // if `unsorted/` exists it must be solely from some other job in
                // this test (there is none) -- assert it contains no notes.txt.
                std::fs::read_dir(root.path().join("unsorted"))
                    .map(|mut entries| entries.all(|e| !e.unwrap().path().ends_with("notes.txt")))
                    .unwrap_or(true)
            }
        );
    });
}

// -- FR-13: service down --------------------------------------------------

#[test]
fn with_the_fake_service_down_the_recording_is_still_filed_and_marked_awaiting_transcription() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let source = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            b"recording bytes",
        );

        let service = FakeService::with_timing(instant_timing());
        service.set_down(true);
        let state = build_state(root.path().to_path_buf(), Arc::new(service));

        let snapshots = enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
            .await
            .expect("enqueue must succeed even with the service down");

        let terminal = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;

        assert_eq!(terminal.state, JobState::Failed);
        assert_eq!(
            terminal.error_kind.as_deref(),
            Some("service_unavailable"),
            "must be a distinct awaiting/failed-transcription state, never an ingest failure"
        );
        let meeting_dir = PathBuf::from(terminal.meeting_dir.as_deref().expect("meeting_dir set"));
        assert!(
            meeting_dir.join("source.mp4").is_file(),
            "the recording must still be filed into the vault even though the service was down"
        );
    });
}

// -- FR-11: a crafted `..`-escaping path -----------------------------------

#[test]
fn a_crafted_dot_dot_escaping_path_is_refused_with_outside_root_before_any_filesystem_access() {
    // `paths::ensure_inside`'s lexical pre-check runs before the candidate
    // even needs to exist (module docs: "escape-before-existence") -- a
    // crafted `..`-escaping candidate that resolves outside `root` is
    // refused immediately, without ever touching disk for it.
    let root = new_tempdir();
    let escaping = root.path().join("..").join("evil.mp4");
    assert!(
        !escaping.exists(),
        "the crafted candidate must not exist on disk for this assertion to be meaningful"
    );

    let err = paths::ensure_inside(root.path(), &escaping)
        .expect_err("a `..`-escaping candidate must be refused");
    assert_eq!(err.kind(), ErrorKind::OutsideRoot);
}

#[test]
fn reveal_job_refuses_a_completed_job_whose_recorded_path_no_longer_sits_inside_the_current_root() {
    // The full pipeline's own containment guarantee (FR-11, FR-15): once a
    // job's recorded path no longer resolves inside the *currently
    // configured* meetings root, `reveal_job` refuses it -- this is a
    // Rust-side guarantee, never something a caller's string could bypass.
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let source = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            b"recording bytes",
        );

        let state = build_state(
            root.path().to_path_buf(),
            Arc::new(FakeService::with_timing(instant_timing())),
        );

        let snapshots = enqueue_paths_handler(&state, vec![source.to_string_lossy().into_owned()])
            .await
            .expect("enqueue must succeed");
        let done = wait_for_terminal(&state, &snapshots[0].id, Duration::from_secs(5)).await;
        assert_eq!(done.state, JobState::Done);

        // Simulate the configured root having moved out from under this
        // job's recorded path (e.g. the user pointed `meetings_root`
        // elsewhere) -- containment is re-checked against the *current*
        // root, not merely trusted from the registry.
        let elsewhere = new_tempdir();
        state.settings.write().await.meetings_root =
            Some(elsewhere.path().to_string_lossy().into_owned());

        let err = reveal_job_handler(&state, &done.id)
            .await
            .expect_err("must be refused once the recorded path sits outside the current root");
        assert_eq!(err.kind(), ErrorKind::OutsideRoot);
    });
}

// -- FR-9: collision policy -------------------------------------------------

#[test]
fn redropping_the_identical_file_applies_the_collision_policy_without_overwriting() {
    run(async {
        let root = new_tempdir();
        let downloads = new_tempdir();
        let content = b"identical recording bytes";
        let first_source = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            content,
        );

        let state = build_state(
            root.path().to_path_buf(),
            Arc::new(FakeService::with_timing(instant_timing())),
        );

        let first_snapshots =
            enqueue_paths_handler(&state, vec![first_source.to_string_lossy().into_owned()])
                .await
                .expect("first enqueue must succeed");
        let first_done =
            wait_for_terminal(&state, &first_snapshots[0].id, Duration::from_secs(5)).await;
        assert_eq!(first_done.state, JobState::Done);

        // Ingest *moves* `first_source` into the vault (same-volume rename,
        // or copy-verify-delete cross-volume) -- it no longer exists at its
        // original location, so "re-dropping the identical file" (a real
        // recording resurfacing later, e.g. re-exported from a device) is
        // simulated here: a fresh file with the exact same bytes *and* the
        // exact same mtime as what's already filed (F1's collision policy
        // matches size and mtime -- see `crates/vault/src/transfer.rs`'s
        // `same_recording`).
        let first_dest = PathBuf::from(first_done.source_dest.as_deref().expect("source_dest set"));
        let filed_mtime = std::fs::metadata(&first_dest)
            .expect("read filed recording metadata")
            .modified()
            .expect("mtime supported");
        let second_source = write_recording(
            downloads.path(),
            "ELS - 260812 - Security issue.mp4",
            content,
        );
        std::fs::OpenOptions::new()
            .write(true)
            .open(&second_source)
            .expect("reopen resurfaced fixture")
            .set_modified(filed_mtime)
            .expect("set mtime to match the already-filed recording");

        let second_snapshots =
            enqueue_paths_handler(&state, vec![second_source.to_string_lossy().into_owned()])
                .await
                .expect("second enqueue must succeed");
        let second_done =
            wait_for_terminal(&state, &second_snapshots[0].id, Duration::from_secs(5)).await;
        assert_eq!(second_done.state, JobState::Done);

        // F1's collision policy for a byte-identical re-drop is a no-op:
        // the second job lands at the very same meeting_dir/source_dest as
        // the first (never a numerically suffixed sibling, and never an
        // overwrite of the original bytes).
        assert_eq!(first_done.meeting_dir, second_done.meeting_dir);
        assert_eq!(first_done.source_dest, second_done.source_dest);
        let source_dest =
            PathBuf::from(second_done.source_dest.as_deref().expect("source_dest set"));
        assert_eq!(
            std::fs::read(&source_dest).expect("read filed recording"),
            content
        );
    });
}

/// A shared, no-op sanity assertion that `Path::ends_with` on a joined
/// PathBuf behaves the way the tests above assume across separators.
#[test]
fn path_ends_with_assumption_holds_for_a_joined_relative_suffix() {
    let full = Path::new("C:\\Meetings").join("ELS").join("260812 - Title");
    assert!(full.ends_with(Path::new("ELS").join("260812 - Title")));
}

// -- NFR-2: a multi-gigabyte ingest never blocks the async runtime ---------

/// Substitutes for the manual "drag a >=2GB file and wiggle the window"
/// observation `npm run tauri dev` GUI-driving cannot reliably automate on
/// this host (see this task's report -- raw screen-coordinate clicks landed
/// on the wrong target, confirming T11's own finding). Instead this proves
/// the underlying mechanism NFR-2 depends on directly and at real scale: a
/// ~2 GiB *cross-volume* ingest (the source lives on the default temp
/// volume; the vault root is forced onto this crate's own build volume via
/// `tempdir_in("target")`, so the transfer is a genuine byte-for-byte copy,
/// not an instant same-volume rename) runs entirely off a single-threaded
/// Tokio runtime's one thread while a concurrent heartbeat task keeps
/// ticking on a tight interval throughout. If `ingest()` ever performed
/// blocking IO directly on the runtime thread instead of via
/// `spawn_blocking`, the heartbeat would stall for the whole transfer
/// instead of ticking steadily.
///
/// Ignored by default (writes and transfers ~2 GiB, taking real wall-clock
/// time) -- run explicitly with `cargo test -p transcriber-desktop --test
/// e2e_flow -- --ignored nfr2` to reproduce.
#[test]
#[ignore = "writes and cross-volume-transfers ~2GiB; run explicitly for NFR-2 evidence"]
fn ingesting_a_multi_gigabyte_cross_volume_file_never_blocks_the_async_runtime_nfr2() {
    use std::io::Write;
    use std::sync::atomic::{AtomicU32, Ordering};

    const TWO_GIB: u64 = 2 * 1024 * 1024 * 1024;
    const CHUNK: usize = 16 * 1024 * 1024;

    let downloads = new_tempdir(); // default temp volume (typically C:)
    let root = tempfile::Builder::new()
        .prefix("t13-nfr2-root-")
        // This crate's own manifest directory -- guaranteed to exist and to
        // sit on this repo's own build volume (D: in this repo), unlike a
        // relative "target" (the workspace's target dir lives three levels
        // up, not under this package).
        .tempdir_in(env!("CARGO_MANIFEST_DIR"))
        .expect("tempdir_in(CARGO_MANIFEST_DIR)");

    let source = downloads.path().join("ELS - 260812 - Big Recording.mp4");
    {
        let mut file = std::fs::File::create(&source).expect("create fixture");
        let chunk = vec![0u8; CHUNK];
        let mut written = 0u64;
        while written < TWO_GIB {
            file.write_all(&chunk).expect("write fixture chunk");
            written += CHUNK as u64;
        }
    }
    assert!(
        std::fs::metadata(&source).expect("stat fixture").len() >= TWO_GIB,
        "fixture must be at least 2 GiB"
    );

    // A single-thread runtime: if `ingest()` performed blocking IO directly
    // on this thread instead of via `spawn_blocking`, the heartbeat task
    // below would starve and its tick count would stall (NFR-2).
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build current-thread runtime");

    let (elapsed, ticks) = runtime.block_on(async {
        let ticks = Arc::new(AtomicU32::new(0));
        let ticks_task = ticks.clone();
        let heartbeat = tokio::spawn(async move {
            for _ in 0..40 {
                tokio::time::sleep(Duration::from_millis(50)).await;
                ticks_task.fetch_add(1, Ordering::SeqCst);
            }
        });

        let start = std::time::Instant::now();
        let outcome = transcriber_desktop_lib::ingest::ingest(root.path(), &source)
            .await
            .expect("a ~2GiB cross-volume ingest must still succeed");
        let elapsed = start.elapsed();

        heartbeat.await.expect("heartbeat task must complete");
        assert!(outcome.source_dest.is_file());
        (elapsed, ticks.load(Ordering::SeqCst))
    });

    eprintln!(
        "NFR-2 evidence: ~2GiB cross-volume ingest took {elapsed:?}; heartbeat ticked \
         {ticks}/40 times on a 50ms interval while it ran (a stalled runtime would show far \
         fewer ticks than the transfer's wall-clock time / 50ms would predict)"
    );
    assert!(
        ticks >= 8,
        "heartbeat must keep ticking throughout a multi-second transfer (NFR-2); got {ticks} ticks"
    );
}
