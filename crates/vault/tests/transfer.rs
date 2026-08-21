//! Tests for the file transfer module (FR-12, NFR-2, R7). Owned by T6.
//!
//! All cases use real directories via `tempfile` — no mocked filesystem.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant};

use tempfile::tempdir;
use vault::error::VaultError;
use vault::transfer::{copy_verify_delete, copy_verify_delete_expecting, transfer_into_place};

fn write_file(path: &Path, bytes: &[u8]) {
    let mut f = fs::File::create(path).expect("create file");
    f.write_all(bytes).expect("write file");
}

#[test]
fn successful_transfer_moves_bytes_and_removes_original() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();
    write_file(&src, b"hello world");

    transfer_into_place(&src, &dest).expect("transfer should succeed");

    assert!(
        !src.exists(),
        "original must be absent after a successful transfer"
    );
    let dest_meta = fs::metadata(&dest).expect("dest must exist");
    assert_eq!(dest_meta.len(), 11);
}

#[test]
fn successful_transfer_preserves_mtime() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();
    write_file(&src, b"hello world");
    let original_mtime = fs::metadata(&src).unwrap().modified().unwrap();

    transfer_into_place(&src, &dest).expect("transfer should succeed");

    let dest_mtime = fs::metadata(&dest).unwrap().modified().unwrap();
    assert_eq!(
        dest_mtime, original_mtime,
        "R7: the size+mtime dedupe depends on mtime surviving the transfer"
    );
}

#[test]
fn forced_failure_leaves_original_intact_and_creates_no_partial_file() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    write_file(&src, b"hello world");
    // Sabotage the destination: pre-create it as a directory.
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(&dest).unwrap();

    let result = transfer_into_place(&src, &dest);

    assert!(result.is_err(), "transfer onto a directory must fail");
    assert!(src.exists(), "original must survive a failed transfer");
    assert_eq!(fs::read(&src).unwrap(), b"hello world");
    assert!(
        dest.is_dir(),
        "the pre-existing directory must be untouched"
    );
}

#[test]
fn zero_byte_source_transfers_cleanly() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    write_file(&src, b"");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    transfer_into_place(&src, &dest).expect("zero-byte transfer should succeed");

    assert!(!src.exists());
    assert_eq!(fs::metadata(&dest).unwrap().len(), 0);
}

#[test]
fn missing_source_is_a_typed_error() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("does-not-exist.mp4");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    let result = transfer_into_place(&src, &dest);

    assert_eq!(result, Err(VaultError::SourceMissing));
}

#[test]
fn directory_as_source_is_a_typed_error() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("a-directory");
    fs::create_dir_all(&src).unwrap();
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    let result = transfer_into_place(&src, &dest);

    assert_eq!(result, Err(VaultError::SourceNotAFile));
}

#[test]
fn same_volume_transfer_of_8mib_completes_under_500ms() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    let payload = vec![7u8; 8 * 1024 * 1024];
    write_file(&src, &payload);
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    let start = Instant::now();
    transfer_into_place(&src, &dest).expect("same-volume transfer should succeed");
    let elapsed = start.elapsed();

    assert!(
        elapsed < Duration::from_millis(500),
        "NFR-2: same-volume transfer of 8 MiB took {elapsed:?}, expected under 500ms"
    );
    assert_eq!(fs::metadata(&dest).unwrap().len(), payload.len() as u64);
}

#[test]
fn copy_path_verifies_size_before_deleting_the_original() {
    // Simulates the cross-volume path directly, without a second volume.
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    write_file(&src, b"hello world");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    copy_verify_delete(&src, &dest).expect("copy path should succeed");

    assert!(
        !src.exists(),
        "original must be deleted only after verification succeeds"
    );
    assert_eq!(fs::read(&dest).unwrap(), b"hello world");
}

#[test]
fn copy_path_rolls_back_when_verification_fails() {
    let dir = tempdir().expect("tempdir");
    let src = dir.path().join("source.mp4");
    write_file(&src, b"hello world");
    let dest = dir.path().join("dest").join("source.mp4");
    fs::create_dir_all(dest.parent().unwrap()).unwrap();

    // Lie about the expected length so verification is forced to fail.
    let result = copy_verify_delete_expecting(&src, &dest, 999);

    assert!(result.is_err());
    assert!(
        !dest.exists(),
        "destination must be removed when verification fails"
    );
    assert!(src.exists(), "original must survive a failed verification");
    assert_eq!(fs::read(&src).unwrap(), b"hello world");
}
