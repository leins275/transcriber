//! File transfer — rename/copy, verify, delete, roll back (FR-12, NFR-2).
//!
//! Owned by T6. Same-volume transfers use an atomic rename; cross-volume
//! transfers copy, verify the destination size, then delete the original.
//! The original is never lost, even when verification fails.
//!
//! [`crate::transfer::transfer_into_place`] is the entry point the ingest
//! orchestration calls: it tries [`std::fs::rename`] first (same volume,
//! atomic, mtime-preserving per R7), and falls back to the
//! copy-verify-delete path on any rename failure (cross-volume or
//! otherwise). [`crate::transfer::copy_verify_delete`] is separately
//! callable so the cross-volume path can be exercised without a second
//! volume, and [`crate::transfer::copy_verify_delete_expecting`] takes the
//! expected size explicitly so that a verification failure can be tested
//! honestly, against a real file, without racing the filesystem.

use std::fs;
use std::io;
use std::path::Path;

use crate::error::{IoFailure, VaultError};

/// Moves `src` into `dest`, preferring an atomic same-volume rename and
/// falling back to a verified copy-then-delete when the rename fails
/// (FR-12, NFR-2, R5).
///
/// `src` must exist and be a regular file, or this returns
/// [`VaultError::SourceMissing`] / [`VaultError::SourceNotAFile`] without
/// touching anything. On any failure after this point, `dest` is left
/// exactly as it was found and `src` is left untouched.
pub fn transfer_into_place(src: &Path, dest: &Path) -> Result<(), VaultError> {
    let expected_len = stat_source(src)?.len();

    match fs::rename(src, dest) {
        Ok(()) => verify_renamed(src, dest, expected_len),
        Err(_) => copy_verify_delete_expecting(src, dest, expected_len),
    }
}

/// After a successful `rename`, verifies the destination's size matches
/// what was expected, renaming back to `src` on a mismatch. In ordinary
/// operation a rename cannot change a file's size, so this is defense in
/// depth rather than an expected path.
fn verify_renamed(src: &Path, dest: &Path, expected_len: u64) -> Result<(), VaultError> {
    match fs::metadata(dest) {
        Ok(meta) if meta.len() == expected_len => Ok(()),
        Ok(meta) => {
            let actual_len = meta.len();
            let _ = fs::rename(dest, src);
            Err(verification_mismatch(dest, expected_len, actual_len))
        }
        Err(e) => {
            let _ = fs::rename(dest, src);
            Err(io_err(dest, &e))
        }
    }
}

/// Copies `src` to `dest`, verifies the destination's size matches `src`'s
/// own size, then deletes `src` (FR-12's copy-verify-delete semantics).
///
/// If verification fails, `dest` is removed and `src` is left intact — the
/// original is deleted only after a successful verification.
pub fn copy_verify_delete(src: &Path, dest: &Path) -> Result<(), VaultError> {
    let expected_len = stat_source(src)?.len();
    copy_verify_delete_expecting(src, dest, expected_len)
}

/// The same copy-verify-delete sequence as [`copy_verify_delete`], but
/// against a caller-supplied expected length rather than one freshly read
/// from `src`.
///
/// This exists so the verification-failure branch can be exercised
/// honestly against a real file (by supplying a deliberately wrong
/// expected length) instead of racing the filesystem to force a genuine
/// mismatch.
pub fn copy_verify_delete_expecting(
    src: &Path,
    dest: &Path,
    expected_len: u64,
) -> Result<(), VaultError> {
    stat_source(src)?;

    fs::copy(src, dest).map_err(|e| io_err(dest, &e))?;

    let actual_len = match fs::metadata(dest) {
        Ok(meta) => meta.len(),
        Err(e) => return Err(io_err(dest, &e)),
    };

    if actual_len != expected_len {
        let _ = fs::remove_file(dest);
        return Err(verification_mismatch(dest, expected_len, actual_len));
    }

    fs::remove_file(src).map_err(|e| io_err(src, &e))
}

/// Compares two recordings by size and modification time, the FR-11
/// dedupe rule that a byte-identical re-drop is a no-op. Depends on mtime
/// surviving a prior transfer (R7): both [`std::fs::rename`] and
/// [`std::fs::copy`] preserve it on Windows.
pub fn same_recording(a: &Path, b: &Path) -> io::Result<bool> {
    let meta_a = fs::metadata(a)?;
    let meta_b = fs::metadata(b)?;
    Ok(meta_a.len() == meta_b.len() && meta_a.modified()? == meta_b.modified()?)
}

/// Validates that `src` exists and is a regular file, returning its
/// metadata. Never touches `dest` or performs any mutation.
fn stat_source(src: &Path) -> Result<fs::Metadata, VaultError> {
    match fs::metadata(src) {
        Ok(meta) if meta.is_file() => Ok(meta),
        Ok(_) => Err(VaultError::SourceNotAFile),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(VaultError::SourceMissing),
        Err(e) => Err(io_err(src, &e)),
    }
}

/// Wraps a `std::io::Error` with the path that produced it.
fn io_err(path: &Path, err: &io::Error) -> VaultError {
    VaultError::Io {
        path: path.to_path_buf(),
        source: IoFailure::from(err),
    }
}

/// Builds the typed error for a post-copy size mismatch: the destination's
/// size does not match what was expected, so the transfer is rolled back
/// rather than risking a truncated `source.*`.
fn verification_mismatch(dest: &Path, expected_len: u64, actual_len: u64) -> VaultError {
    let io_error = io::Error::new(
        io::ErrorKind::InvalidData,
        format!("destination is {actual_len} bytes, expected {expected_len} bytes after transfer"),
    );
    io_err(dest, &io_error)
}
