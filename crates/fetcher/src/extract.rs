//! Unpacking the part of a zip payload that is wanted.
//!
//! A runtime payload is a zip (a Python wheel was one too) carrying far more
//! than the files that are needed: metadata directories, headers, import
//! libraries. Extracting only a configured prefix is what the Python's
//! parameterised extraction did, and it is what keeps a 500 MB archive from
//! becoming 500 MB of installed junk.
//!
//! Member paths keep their prefix on the way out, so `nvidia/cublas/bin/x.dll`
//! lands at `<dest>/nvidia/cublas/bin/x.dll`. That is not an accident of the
//! Python implementation but the layout the loaders expect.

use std::fs;
use std::io;
use std::path::Path;

use zip::ZipArchive;

use crate::error::FetchError;

/// Extract every file member of `archive` whose path starts with `prefix` into
/// `dest`, returning how many were written.
///
/// An empty `prefix` extracts everything. Directory entries are skipped and
/// their directories created on demand, because a zip is not required to carry
/// them and half of them do not.
pub fn extract_tree(archive: &Path, prefix: &str, dest: &Path) -> Result<usize, FetchError> {
    let file = fs::File::open(archive).map_err(|source| FetchError::io(archive, source))?;
    let mut zip = ZipArchive::new(file).map_err(|source| FetchError::Archive {
        path: archive.to_path_buf(),
        source,
    })?;

    let mut written = 0;
    for index in 0..zip.len() {
        let mut entry = zip.by_index(index).map_err(|source| FetchError::Archive {
            path: archive.to_path_buf(),
            source,
        })?;
        // Zip stores forward slashes, but archives built on Windows by careless
        // tools do not always agree.
        let name = entry.name().replace('\\', "/");
        if !name.starts_with(prefix) || name.ends_with('/') || entry.is_dir() {
            continue;
        }

        // `enclosed_name` refuses absolute paths, drive letters and `..`
        // components. A payload that contains one is not the payload that was
        // pinned, so the whole archive is refused rather than that one member
        // skipped.
        let relative = entry
            .enclosed_name()
            .ok_or_else(|| FetchError::UnsafeArchiveMember {
                path: archive.to_path_buf(),
                member: name.clone(),
            })?;

        let out_path = dest.join(relative);
        if let Some(parent) = out_path.parent() {
            fs::create_dir_all(parent).map_err(|source| FetchError::io(parent, source))?;
        }
        let mut out =
            fs::File::create(&out_path).map_err(|source| FetchError::io(&out_path, source))?;
        io::copy(&mut entry, &mut out).map_err(|source| FetchError::io(&out_path, source))?;
        written += 1;
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fakes::zip_bytes;

    fn archive_of(dir: &Path, entries: &[(&str, &[u8])]) -> std::path::PathBuf {
        let path = dir.join("payload.zip");
        fs::write(&path, zip_bytes(entries)).unwrap();
        path
    }

    #[test]
    fn only_the_configured_prefix_is_extracted() {
        let dir = tempfile::tempdir().unwrap();
        let archive = archive_of(
            dir.path(),
            &[
                ("nvidia/cublas/bin/cublas64_12.dll", b"binary-dll-content"),
                ("fake_pkg-1.0.dist-info/METADATA", b"not nvidia"),
            ],
        );
        let dest = dir.path().join("runtime");

        let written = extract_tree(&archive, "nvidia/", &dest).unwrap();

        assert_eq!(written, 1);
        assert_eq!(
            fs::read(dest.join("nvidia/cublas/bin/cublas64_12.dll")).unwrap(),
            b"binary-dll-content"
        );
        assert!(!dest.join("fake_pkg-1.0.dist-info").exists());
    }

    #[test]
    fn two_archives_merge_into_one_tree() {
        // The CUDA runtime case: several wheels each carrying part of one
        // `nvidia/` tree, unpacked into the same destination.
        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("runtime");
        let a = dir.path().join("a.zip");
        let b = dir.path().join("b.zip");
        fs::write(&a, zip_bytes(&[("nvidia/cublas/bin/a.dll", b"a")])).unwrap();
        fs::write(&b, zip_bytes(&[("nvidia/cudnn/bin/b.dll", b"b")])).unwrap();

        extract_tree(&a, "nvidia/", &dest).unwrap();
        extract_tree(&b, "nvidia/", &dest).unwrap();

        assert_eq!(
            fs::read(dest.join("nvidia/cublas/bin/a.dll")).unwrap(),
            b"a"
        );
        assert_eq!(fs::read(dest.join("nvidia/cudnn/bin/b.dll")).unwrap(), b"b");
    }

    #[test]
    fn an_empty_prefix_extracts_the_whole_archive() {
        let dir = tempfile::tempdir().unwrap();
        let archive = archive_of(dir.path(), &[("a.txt", b"a"), ("nested/b.txt", b"b")]);
        let dest = dir.path().join("out");

        assert_eq!(extract_tree(&archive, "", &dest).unwrap(), 2);
        assert!(dest.join("nested/b.txt").is_file());
    }

    #[test]
    fn a_member_that_escapes_the_destination_fails_the_whole_archive() {
        // Zip-slip: the archive is not the artifact that was pinned, so
        // nothing in it is trustworthy, not just the offending member.
        let dir = tempfile::tempdir().unwrap();
        let archive = archive_of(dir.path(), &[("nvidia/../../escaped.dll", b"x")]);
        let dest = dir.path().join("runtime");

        let err = extract_tree(&archive, "nvidia/", &dest).unwrap_err();
        assert!(matches!(err, FetchError::UnsafeArchiveMember { .. }));
        assert!(!dir.path().join("escaped.dll").exists());
    }

    #[test]
    fn a_file_that_is_not_a_zip_is_an_archive_error_not_a_panic() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("truncated.zip");
        fs::write(&archive, b"PK\x03\x04 and then nothing").unwrap();

        assert!(matches!(
            extract_tree(&archive, "", &dir.path().join("out")),
            Err(FetchError::Archive { .. })
        ));
    }
}
