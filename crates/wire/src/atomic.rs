//! Atomic file writes (NFR-5).
//!
//! Port of the `transcript.write_atomic` pattern: write to an unpredictable
//! temp file *inside the destination directory* -- so the rename is a
//! same-filesystem operation and therefore atomic -- fsync it, then rename
//! over the target. A reader never observes a partial file, and a crash
//! mid-write leaves the previous version intact rather than a truncated one.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::Path;

/// Write `bytes` to `target` atomically, creating parent directories.
pub fn write_bytes(target: &Path, bytes: &[u8], prefix: &str) -> io::Result<()> {
    let dir = target.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{} has no parent directory", target.display()),
        )
    })?;
    fs::create_dir_all(dir)?;

    let tmp = tempfile::Builder::new()
        .prefix(prefix)
        .suffix(".tmp")
        .tempfile_in(dir)?;

    // Scope the handle so the file is closed before the rename: Windows will
    // not rename over a target while a handle to the source is open.
    {
        let mut handle: &File = tmp.as_file();
        handle.write_all(bytes)?;
        handle.flush()?;
        handle.sync_all()?;
    }

    // `persist` is rename-with-replace; on failure the temp file is removed
    // by its own Drop, so nothing is left behind.
    tmp.persist(target).map_err(|e| e.error)?;
    Ok(())
}

/// Write `text` to `target` atomically, as UTF-8 with platform line endings.
///
/// The translation is not a style choice: the Python writer this replaces
/// opened its temp file in text mode (`open(..., "w")`), so every `.md` it
/// ever wrote on Windows landed with CRLF. Those files are in users' vaults
/// now. Writing LF here would rewrite every line of every artifact the first
/// time a job re-runs -- a whole-file diff that says nothing changed.
///
/// Like Python's text mode, this translates every `\n`, including one that is
/// already part of a `\r\n`. Callers build their documents with `\n` only, so
/// that case does not arise; it is matched rather than special-cased because
/// diverging deliberately is how the two writers drift apart.
pub fn write_text(target: &Path, text: &str) -> io::Result<()> {
    write_bytes(target, to_platform_newlines(text).as_bytes(), ".artifact-")
}

#[cfg(windows)]
fn to_platform_newlines(text: &str) -> std::borrow::Cow<'_, str> {
    if text.contains('\n') {
        std::borrow::Cow::Owned(text.replace('\n', "\r\n"))
    } else {
        std::borrow::Cow::Borrowed(text)
    }
}

#[cfg(not(windows))]
fn to_platform_newlines(text: &str) -> std::borrow::Cow<'_, str> {
    std::borrow::Cow::Borrowed(text)
}

/// Write a `transcript.json` body atomically (its own temp prefix, matching
/// the Python writer's `.transcript-*.tmp`).
///
/// No newline translation, unlike [`write_text`]: JSON escapes any newline
/// inside a string as `\n` (two characters), so a compact document contains no
/// literal line terminator to translate, and the bytes should be the bytes.
pub fn write_transcript(target: &Path, text: &str) -> io::Result<()> {
    write_bytes(target, text.as_bytes(), ".transcript-")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_through_missing_parent_directories() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("a").join("b").join("note.md");
        write_text(&target, "hello").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "hello");
    }

    #[test]
    fn replaces_an_existing_file() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("note.md");
        write_text(&target, "first").unwrap();
        write_text(&target, "second").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "second");
    }

    #[test]
    #[cfg(windows)]
    fn text_lands_with_crlf_like_the_python_writer() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("note.md");
        write_text(&target, "a\nb\n").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"a\r\nb\r\n");
    }

    #[test]
    fn transcript_bytes_are_never_translated() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("transcript.json");
        write_transcript(&target, "{\"a\": 1}").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"{\"a\": 1}");
    }

    #[test]
    fn leaves_no_temp_files_behind() {
        let root = tempfile::tempdir().unwrap();
        write_text(&root.path().join("note.md"), "hello").unwrap();
        let names: Vec<String> = fs::read_dir(root.path())
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, vec!["note.md".to_string()]);
    }
}
