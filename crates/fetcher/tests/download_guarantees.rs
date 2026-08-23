//! The guarantees the Python's own tests pinned down, against the public API.
//!
//! Ported case for case from `services/transcription/tests/test_model_download.py`
//! and `test_cuda_runtime.py`, which ran with no network, no GPU and no real
//! weights: a fake transport writes deterministic bytes to disk so resume
//! offsets, digest handling and cancellation can be asserted directly.

use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use fetcher::fakes::{sha256_of, zip_bytes, FakeTransport};
use fetcher::{Download, DownloadState, FetchError, Install, Payload, Progress};

const REPO_URL: &str = "https://huggingface.co/org/repo/resolve/rev";

fn model_payload(content: &[u8]) -> Payload {
    Payload::file(
        "whisper",
        "model.bin",
        &format!("{REPO_URL}/model.bin"),
        content.len() as u64,
        &sha256_of(content),
    )
}

fn zip_payload(name: &str, file_name: &str, archive: &[u8], dest_subdir: &str) -> Payload {
    Payload {
        name: name.to_string(),
        file_name: file_name.to_string(),
        url: format!("{REPO_URL}/{file_name}"),
        size: archive.len() as u64,
        sha256: sha256_of(archive),
        install: Install::ZipTree {
            prefix: "nvidia/".to_string(),
            dest_subdir: dest_subdir.to_string(),
        },
    }
}

/// A download of one model file, with the fake kept so a test can steer it.
fn single_file(dir: &Path, content: &[u8]) -> (Download, Arc<FakeTransport>) {
    let transport = Arc::new(FakeTransport::new(&[("model.bin", content)]));
    let download = Download::new(
        dir,
        vec![model_payload(content)],
        Box::new(Arc::clone(&transport)),
    )
    .expect("build download");
    (download, transport)
}

fn run(download: &mut Download) -> (Vec<Progress>, Result<(), FetchError>) {
    let mut events = Vec::new();
    let outcome = download.start(&mut |p| events.push(p.clone()), Duration::ZERO);
    (events, outcome)
}

/// Run a download that the test needs to have succeeded before it can assert
/// anything interesting.
fn run_ok(download: &mut Download) -> Vec<Progress> {
    let (events, outcome) = run(download);
    outcome.expect("download should have succeeded");
    events
}

#[test]
fn a_completed_download_lands_the_file_and_writes_its_ready_marker() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);

    let (events, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(download.state(), DownloadState::Complete);
    assert_eq!(fs::read(dir.path().join("model.bin")).unwrap(), content);
    assert!(fetcher::is_installed(&dir.path().join("model.bin")));
    assert_eq!(events.last().unwrap().state, DownloadState::Complete);
}

#[test]
fn the_marker_records_which_pin_was_verified() {
    // Not decoration: a support session has to be able to tell which artifact
    // a machine actually has without re-hashing several gigabytes.
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 64];
    let (mut download, _transport) = single_file(dir.path(), &content);
    run_ok(&mut download);

    let marker = fs::read_to_string(dir.path().join("model.bin.ready")).unwrap();
    assert!(marker.contains(&sha256_of(&content)));
    assert!(marker.contains("whisper"));
}

#[test]
fn the_file_is_written_flat_into_the_directory_it_was_given() {
    // `docs/verification-installer.md` "Blocker 2": the caller passes the
    // exact directory the loader will read, so nesting anything underneath it
    // is the layout mismatch that broke the Python loader.
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 500];
    let (mut download, _transport) = single_file(dir.path(), &content);
    run_ok(&mut download);

    assert!(dir.path().join("model.bin").is_file());
    let subdirectories: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .flatten()
        .filter(|e| e.path().is_dir())
        .collect();
    assert!(
        subdirectories.is_empty(),
        "nothing may be nested underneath"
    );
}

#[test]
fn progress_reports_carry_the_whole_shape_and_fire_more_than_once() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);

    let (events, _) = run(&mut download);

    assert!(events.len() >= 2);
    let last = events.last().unwrap();
    assert_eq!(last.state, DownloadState::Complete);
    assert_eq!(last.downloaded_bytes, 10_000);
    assert_eq!(last.total_bytes, 10_000);
    assert!((last.percent - 100.0).abs() < f64::EPSILON);
    assert!(events.iter().any(|e| e.state == DownloadState::Downloading));
    assert!(events.iter().any(|e| e.state == DownloadState::Verifying));
}

#[test]
fn a_throttled_sink_still_learns_how_the_transfer_started_and_ended() {
    // The interval is a rate limit, not a requirement to wait a full second
    // before reporting anything: a transfer that takes a millisecond must
    // still report that it began and that it finished.
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);

    let mut events = Vec::new();
    download
        .start(&mut |p| events.push(p.clone()), Duration::from_secs(1))
        .unwrap();

    assert_eq!(events.first().unwrap().state, DownloadState::Downloading);
    assert_eq!(events.first().unwrap().total_bytes, 10_000);
    assert_eq!(events.last().unwrap().state, DownloadState::Complete);
}

#[test]
fn an_interrupted_transfer_resumes_from_the_incomplete_sibling() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let transport =
        Arc::new(FakeTransport::new(&[("model.bin", &content)]).interrupt_at("model.bin", 5_000));
    let mut download = Download::new(
        dir.path(),
        vec![model_payload(&content)],
        Box::new(Arc::clone(&transport)),
    )
    .unwrap();

    let (_, outcome) = run(&mut download);

    assert!(matches!(outcome, Err(FetchError::Interrupted { .. })));
    assert_eq!(download.state(), DownloadState::Error);
    let incomplete = dir.path().join("model.bin.incomplete");
    let first_size = fs::metadata(&incomplete).unwrap().len();
    assert!(
        0 < first_size && first_size < 10_000,
        "the partial blob is what makes the retry cheap"
    );
    assert!(!dir.path().join("model.bin").exists());

    transport.heal();
    let (_, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(
        *transport.resume_offsets().last().unwrap(),
        first_size,
        "the retry must pick up where the drop left off, not at byte zero"
    );
    assert_eq!(download.state(), DownloadState::Complete);
    assert_eq!(fs::read(dir.path().join("model.bin")).unwrap(), content);
}

#[test]
fn a_partial_blob_longer_than_the_real_file_is_discarded_rather_than_resumed() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 200];
    let (mut download, transport) = single_file(dir.path(), &content);
    fs::write(dir.path().join("model.bin.incomplete"), vec![b'x'; 500]).unwrap();

    let (_, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(transport.resume_offsets(), vec![0]);
    assert_eq!(fs::read(dir.path().join("model.bin")).unwrap(), content);
}

#[test]
fn a_cancel_stops_within_one_chunk_and_leaves_a_resumable_partial_file() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);
    let token = download.cancel_token();

    let seen = AtomicUsize::new(0);
    let outcome = download.start(
        &mut |_p| {
            if seen.fetch_add(1, Ordering::SeqCst) == 1 {
                token.cancel();
            }
        },
        Duration::ZERO,
    );

    assert!(outcome.is_ok(), "a cancel is not a failure");
    assert_eq!(download.state(), DownloadState::Cancelled);
    let incomplete = dir.path().join("model.bin.incomplete");
    let size = fs::metadata(&incomplete).unwrap().len();
    assert!(0 < size && size < 10_000);
    assert!(!dir.path().join("model.bin").exists());
    assert!(!fetcher::is_installed(&dir.path().join("model.bin")));
}

#[test]
fn a_digest_mismatch_is_an_error_and_never_marks_anything_usable() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 500];
    let payload = Payload::file(
        "whisper",
        "model.bin",
        &format!("{REPO_URL}/model.bin"),
        content.len() as u64,
        &"0".repeat(64),
    );
    let transport = FakeTransport::new(&[("model.bin", &content)]);
    let mut download = Download::new(dir.path(), vec![payload], Box::new(transport)).unwrap();

    let (_, outcome) = run(&mut download);

    assert!(matches!(outcome, Err(FetchError::DigestMismatch { .. })));
    assert_eq!(download.state(), DownloadState::Error);
    assert!(!dir.path().join("model.bin.ready").exists());
    assert!(
        !dir.path().join("model.bin.incomplete").exists(),
        "wrong bytes are not a resume point; keeping them would poison the retry"
    );
}

#[test]
fn verification_fails_on_a_truncated_file_and_takes_the_marker_away() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);
    run_ok(&mut download);
    assert!(download.verify());

    fs::write(dir.path().join("model.bin"), &content[..content.len() - 1]).unwrap();

    assert!(!download.verify());
    assert!(!dir.path().join("model.bin.ready").exists());
    assert!(!fetcher::is_installed(&dir.path().join("model.bin")));
}

#[test]
fn verification_fails_on_a_corrupted_file_of_the_right_length() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10_000];
    let (mut download, _transport) = single_file(dir.path(), &content);
    run_ok(&mut download);

    let corrupted: Vec<u8> = content.iter().map(|b| b.wrapping_add(1)).collect();
    fs::write(dir.path().join("model.bin"), &corrupted).unwrap();

    assert!(!download.verify());
    assert!(!dir.path().join("model.bin.ready").exists());
}

#[test]
fn an_already_installed_payload_is_not_fetched_again() {
    // FR-16-style idempotency: a restarted first-run wizard must never re-fetch
    // gigabytes a previous run already verified.
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 500];
    let (mut download, transport) = single_file(dir.path(), &content);
    run_ok(&mut download);
    assert_eq!(transport.calls(), 1);

    let (mut second_download, second_transport) = single_file(dir.path(), &content);
    assert!(second_download.already_installed());
    let (events, outcome) = run(&mut second_download);

    assert!(outcome.is_ok());
    assert_eq!(second_download.state(), DownloadState::Complete);
    assert_eq!(second_transport.calls(), 0);
    assert_eq!(events.last().unwrap().state, DownloadState::Complete);
}

#[test]
fn a_same_sized_but_corrupt_leftover_file_is_refetched_rather_than_trusted() {
    // The app folder is user-writable by design and a crash mid-write can leave
    // a file of exactly the right length. Length is not integrity.
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 500];
    let corrupt: Vec<u8> = content.iter().map(|b| b.wrapping_add(1)).collect();
    fs::write(dir.path().join("model.bin"), &corrupt).unwrap();

    let (mut download, transport) = single_file(dir.path(), &content);
    let (_, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(
        transport.calls(),
        1,
        "the corrupt leftover must trigger a real fetch"
    );
    assert_eq!(fs::read(dir.path().join("model.bin")).unwrap(), content);
}

#[test]
fn several_payloads_share_one_byte_total() {
    let dir = tempfile::tempdir().unwrap();
    let first = vec![b'1'; 300];
    let second = vec![b'2'; 700];
    let transport = FakeTransport::new(&[("a.bin", &first), ("b.bin", &second)]);
    let payloads = vec![
        Payload::file(
            "a",
            "a.bin",
            &format!("{REPO_URL}/a.bin"),
            300,
            &sha256_of(&first),
        ),
        Payload::file(
            "b",
            "b.bin",
            &format!("{REPO_URL}/b.bin"),
            700,
            &sha256_of(&second),
        ),
    ];
    let mut download = Download::new(dir.path(), payloads, Box::new(transport)).unwrap();

    let (events, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(download.total_bytes(), 1000);
    assert_eq!(events.last().unwrap().downloaded_bytes, 1000);
    assert!(fetcher::is_installed(&dir.path().join("a.bin")));
    assert!(fetcher::is_installed(&dir.path().join("b.bin")));
}

#[test]
fn a_zip_payload_extracts_only_its_prefix_and_then_the_archive_is_gone() {
    let dir = tempfile::tempdir().unwrap();
    let archive = zip_bytes(&[
        ("nvidia/cublas/bin/cublas64_12.dll", b"binary-dll-content"),
        ("fake_pkg-1.0.dist-info/METADATA", b"not nvidia"),
    ]);
    let transport = FakeTransport::new(&[("cublas.whl", &archive)]).with_chunk_size(64);
    let payload = zip_payload("cublas", "cublas.whl", &archive, "");
    let mut download = Download::new(dir.path(), vec![payload], Box::new(transport)).unwrap();

    let (_, outcome) = run(&mut download);

    assert!(outcome.is_ok());
    assert_eq!(
        fs::read(dir.path().join("nvidia/cublas/bin/cublas64_12.dll")).unwrap(),
        b"binary-dll-content"
    );
    assert!(!dir.path().join("fake_pkg-1.0.dist-info").exists());
    assert!(
        !dir.path().join("_archives").exists(),
        "nothing multi-hundred-megabyte stays duplicated on disk"
    );
    assert!(fetcher::is_extracted(dir.path()));
}

#[test]
fn two_zip_payloads_merge_into_one_tree_and_a_restart_skips_them_both() {
    let dir = tempfile::tempdir().unwrap();
    let a = zip_bytes(&[("nvidia/cublas/bin/a.dll", b"a")]);
    let b = zip_bytes(&[("nvidia/cudnn/bin/b.dll", b"b")]);
    let transport =
        Arc::new(FakeTransport::new(&[("a.whl", &a), ("b.whl", &b)]).with_chunk_size(64));
    let payloads = vec![
        zip_payload("a", "a.whl", &a, ""),
        zip_payload("b", "b.whl", &b, ""),
    ];
    let mut download = Download::new(
        dir.path(),
        payloads.clone(),
        Box::new(Arc::clone(&transport)),
    )
    .unwrap();

    run_ok(&mut download);

    assert_eq!(
        fs::read(dir.path().join("nvidia/cublas/bin/a.dll")).unwrap(),
        b"a"
    );
    assert_eq!(
        fs::read(dir.path().join("nvidia/cudnn/bin/b.dll")).unwrap(),
        b"b"
    );

    // A fresh session against the same directory has nothing left to do.
    let second_transport = Arc::new(FakeTransport::new(&[]));
    let mut again = Download::new(
        dir.path(),
        payloads,
        Box::new(Arc::clone(&second_transport)),
    )
    .unwrap();
    assert!(again.already_installed());
    run_ok(&mut again);
    assert_eq!(again.state(), DownloadState::Complete);
    assert_eq!(second_transport.calls(), 0);
}

#[test]
fn a_zip_payload_unpacks_into_the_subdirectory_it_was_given() {
    // The GPU build of the LLM runtime keeps its own tree so that it and the
    // shared runtime directory stay independently replaceable.
    let dir = tempfile::tempdir().unwrap();
    let archive = zip_bytes(&[("nvidia/bin/x.dll", b"x")]);
    let transport = FakeTransport::new(&[("llama.whl", &archive)]).with_chunk_size(64);
    let payload = zip_payload("llama-cuda", "llama.whl", &archive, "llama-cuda");
    let mut download = Download::new(dir.path(), vec![payload], Box::new(transport)).unwrap();

    run_ok(&mut download);

    assert!(dir.path().join("llama-cuda/nvidia/bin/x.dll").is_file());
    assert!(fetcher::is_extracted(&dir.path().join("llama-cuda")));
}

#[test]
fn an_interrupted_group_leaves_no_marker_claiming_it_is_ready() {
    // Markers are written after the last payload verifies, so a run that dies
    // partway through cannot leave a half-installed tree looking complete.
    let dir = tempfile::tempdir().unwrap();
    let first = vec![b'1'; 300];
    let second = vec![b'2'; 700];
    let transport = Arc::new(
        FakeTransport::new(&[("a.bin", &first), ("b.bin", &second)]).interrupt_at("b.bin", 100),
    );
    let payloads = vec![
        Payload::file(
            "a",
            "a.bin",
            &format!("{REPO_URL}/a.bin"),
            300,
            &sha256_of(&first),
        ),
        Payload::file(
            "b",
            "b.bin",
            &format!("{REPO_URL}/b.bin"),
            700,
            &sha256_of(&second),
        ),
    ];
    let mut download =
        Download::new(dir.path(), payloads, Box::new(Arc::clone(&transport))).unwrap();

    let (_, outcome) = run(&mut download);

    assert!(outcome.is_err());
    assert!(
        dir.path().join("a.bin").is_file(),
        "the verified bytes are kept"
    );
    assert!(
        !dir.path().join("a.bin.ready").exists(),
        "but nothing in an unfinished group counts as installed"
    );
    assert!(!fetcher::is_installed(&dir.path().join("a.bin")));
}

#[test]
fn a_payload_pointing_off_the_allowlist_never_reaches_a_transport() {
    let dir = tempfile::tempdir().unwrap();
    let content = vec![b'b'; 10];
    let payload = Payload::file(
        "sneaky",
        "model.bin",
        "https://models.example.com/model.bin",
        10,
        &sha256_of(&content),
    );
    let transport = Arc::new(FakeTransport::new(&[("model.bin", &content)]));

    let err =
        Download::new(dir.path(), vec![payload], Box::new(Arc::clone(&transport))).unwrap_err();

    assert!(matches!(err, FetchError::HostNotAllowed { .. }));
    assert_eq!(transport.calls(), 0);
}

#[test]
fn a_download_with_nothing_selected_is_refused_rather_than_succeeding_emptily() {
    let dir = tempfile::tempdir().unwrap();
    let transport = FakeTransport::new(&[]);
    assert!(matches!(
        Download::new(dir.path(), Vec::new(), Box::new(transport)),
        Err(FetchError::NothingToDownload)
    ));
}
