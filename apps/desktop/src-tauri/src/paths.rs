//! Path canonicalization and meetings-root containment checks (FR-11,
//! FR-15).
//!
//! A drag-drop payload is untrusted input and a file dialog result is not
//! validation (`desktop` profile inline rule) — every path this app acts on
//! is checked here before any write or shell invocation.
//!
//! ## Escape-before-existence (FR-11)
//!
//! [`ensure_inside`] runs a **lexical** escape check first — resolving
//! `.`/`..` components and comparing drive/UNC identity without ever
//! touching the filesystem for `candidate` — so a crafted `..`-escaping
//! path, a path on another drive, a UNC path or a `\\?\` device path is
//! refused before any filesystem access to it at all, let alone a write.
//! Only once that passes are both sides canonicalized (which requires
//! `candidate` to exist), which also normalizes away a `\\?\` mismatch
//! between the two sides and catches a symlink escape the lexical check
//! cannot see.
//!
//! ## Single allowlist (FR-6, mitigation for the F1/spec divergence)
//!
//! The vault crate (F1) validates media extensions against a ten-item list
//! in `vault::media`, but that list is a private `const` — `crates/vault`'s
//! curated public API (`crates/vault/src/lib.rs`) does not re-export it.
//! [`supported_extensions`] is therefore this feature's own copy, kept to
//! exactly one place and documented as mirroring F1's real list rather than
//! this spec's FR-6 text, which lists `.aac` (F1 does not accept it) and
//! omits `.avi` (F1 does) — the merged vault code is the truth, per the
//! plan's stated mitigation.

use std::ffi::OsString;
use std::path::{Component, Path, PathBuf, Prefix};

use crate::error::AppError;

/// The ten recording media extensions this feature accepts, lowercase and
/// without a leading dot. Mirrors `vault::media`'s (private) allowlist —
/// see the module docs for why this is a deliberate, documented copy
/// rather than a re-derivation from the spec text.
const SUPPORTED_EXTENSIONS: [&str; 10] = [
    "mp4", "mkv", "mov", "webm", "avi", "m4a", "mp3", "wav", "flac", "ogg",
];

/// The single source of truth for accepted recording extensions (FR-6).
/// The frontend receives this list via `SettingsView::supported_extensions`
/// rather than hard-coding its own copy.
pub fn supported_extensions() -> &'static [&'static str] {
    &SUPPORTED_EXTENSIONS
}

/// Canonicalizes an existing path.
///
/// A missing path is reported as `AppError::not_a_file` rather than the
/// underlying `NotFound` error, since every caller in this module only
/// ever wants to know "does this resolve to something real" (NFR-6: never
/// panics). Any other filesystem failure (permission, etc.) is reported as
/// `AppError::io`.
pub fn canonicalize_existing(path: &Path) -> Result<PathBuf, AppError> {
    std::fs::canonicalize(path).map_err(|err| {
        if err.kind() == std::io::ErrorKind::NotFound {
            AppError::not_a_file(format!("{} does not exist", display(path)))
        } else {
            AppError::io(format!("failed to resolve {}: {err}", display(path)))
        }
    })
}

/// Strips a Windows extended-length `\\?\` prefix for display purposes,
/// mirroring vault's `paths::simplify_extended_prefix`: a plain drive path
/// (`\\?\C:\...`) is simplified to `C:\...`, a device-`\\?\UNC\...` path is
/// simplified to `\\...`, and anything else (or a path without the prefix
/// at all) is returned unchanged. Never touches the filesystem.
pub fn strip_verbatim(path: &Path) -> PathBuf {
    const VERBATIM_UNC_PREFIX: &str = r"\\?\UNC\";
    const VERBATIM_PREFIX: &str = r"\\?\";

    let Some(s) = path.to_str() else {
        return path.to_path_buf();
    };

    if let Some(rest) = s.strip_prefix(VERBATIM_UNC_PREFIX) {
        return PathBuf::from(format!(r"\\{rest}"));
    }

    if let Some(rest) = s.strip_prefix(VERBATIM_PREFIX) {
        if is_plain_drive_path(rest) {
            return PathBuf::from(rest);
        }
    }

    path.to_path_buf()
}

/// Whether `s` starts with a plain drive-letter path component (`<letter>:\`),
/// as opposed to a UNC share or some other device form.
fn is_plain_drive_path(s: &str) -> bool {
    let bytes = s.as_bytes();
    bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\'
}

fn display(path: &Path) -> String {
    strip_verbatim(path).display().to_string()
}

/// A path's drive/host identity plus its resolved (`.`/`..`-free) component
/// list, computed without touching the filesystem — the vocabulary
/// `ensure_inside`'s lexical pre-check compares on.
#[derive(Debug, PartialEq, Eq)]
struct LexicalPath {
    key: String,
    components: Vec<OsString>,
}

/// Resolves `path` into a [`LexicalPath`] purely lexically. Returns `None`
/// for a path with no drive/UNC/device prefix (i.e. not the kind of
/// absolute path a canonicalized root or a real dropped file ever is), so
/// such a path is never treated as contained by anything.
fn lexical_normalize(path: &Path) -> Option<LexicalPath> {
    let mut key = String::new();
    let mut components = Vec::new();

    for component in path.components() {
        match component {
            Component::Prefix(prefix) => key = prefix_key(prefix.kind()),
            Component::RootDir | Component::CurDir => {}
            Component::ParentDir => {
                components.pop();
            }
            Component::Normal(part) => components.push(part.to_os_string()),
        }
    }

    if key.is_empty() {
        None
    } else {
        Some(LexicalPath { key, components })
    }
}

/// A stable, comparable key for a path prefix's identity. `Disk` and
/// `VerbatimDisk` for the same letter (and the plain/verbatim UNC pair)
/// collapse to the same key, so a `\\?\`-prefixed path and its plain
/// equivalent compare equal — the "both sides normalized" half of FR-11's
/// verbatim-prefix handling.
fn prefix_key(kind: Prefix<'_>) -> String {
    match kind {
        Prefix::Disk(letter) | Prefix::VerbatimDisk(letter) => {
            format!("disk:{}", (letter as char).to_ascii_uppercase())
        }
        Prefix::UNC(server, share) | Prefix::VerbatimUNC(server, share) => format!(
            "unc:{}:{}",
            server.to_string_lossy().to_ascii_lowercase(),
            share.to_string_lossy().to_ascii_lowercase()
        ),
        Prefix::Verbatim(component) => format!(
            "verbatim:{}",
            component.to_string_lossy().to_ascii_lowercase()
        ),
        Prefix::DeviceNS(component) => {
            format!(
                "device:{}",
                component.to_string_lossy().to_ascii_lowercase()
            )
        }
    }
}

/// Whether every component of `root` is a prefix of `candidate`'s
/// components under the same drive/UNC identity — component-wise, never a
/// string `starts_with`, so `C:\Meetings-old\x.mp4` is never contained by
/// `C:\Meetings`.
fn is_component_prefix(root: &LexicalPath, candidate: &LexicalPath) -> bool {
    root.key == candidate.key
        && candidate.components.len() >= root.components.len()
        && root
            .components
            .iter()
            .zip(candidate.components.iter())
            .all(|(r, c)| r == c)
}

fn outside_root_error(candidate: &Path) -> AppError {
    AppError::outside_root(format!(
        "{} is outside the meetings root",
        display(candidate)
    ))
}

/// Ensures `candidate` resolves to a path inside `root` (FR-11, FR-15).
/// Returns the canonical form of `candidate` on success.
///
/// See the module docs for why the lexical check runs before any
/// filesystem access to `candidate`.
pub fn ensure_inside(root: &Path, candidate: &Path) -> Result<PathBuf, AppError> {
    let normalized_root = lexical_normalize(root).ok_or_else(|| {
        AppError::internal(format!("meetings root {} is not absolute", display(root)))
    })?;
    let normalized_candidate = match lexical_normalize(candidate) {
        Some(normalized) => normalized,
        None => return Err(outside_root_error(candidate)),
    };

    // The root is trusted configuration, not hostile input, so resolving it
    // costs nothing this function was protecting against — the
    // escape-before-existence rule (module docs) is about never touching the
    // filesystem for `candidate`, and that still holds below.
    let canonical_root = canonicalize_existing(root)?;

    // A candidate may legitimately arrive spelled either way, and the two
    // spellings are not textually comparable:
    //
    //   * `list_vault` builds its candidates by reading the *configured*
    //     root's directory entries, so they carry the root's own spelling;
    //   * ingest and reveal take theirs from F1, which canonicalizes.
    //
    // Those agree only while the configured root is already in canonical
    // form. It often is not — an 8.3 short component (`C:\Users\RUNNER~1`,
    // which is exactly how `%TEMP%` is exposed on a Windows CI runner) or a
    // junction anywhere in the path both produce a root whose spelling
    // differs from what `canonicalize` returns, and then a component-wise
    // comparison against one spelling rejects every path built from the
    // other. Accepting either is not a weakening: the authoritative check is
    // the canonical `starts_with` below, and this only decides whether the
    // candidate has earned the filesystem access needed to reach it.
    let lexically_inside = is_component_prefix(&normalized_root, &normalized_candidate)
        || lexical_normalize(&canonical_root)
            .is_some_and(|canonical| is_component_prefix(&canonical, &normalized_candidate));
    if !lexically_inside {
        return Err(outside_root_error(candidate));
    }

    // Only now — after the candidate has already been proven, lexically, to
    // resolve under root — do we touch the filesystem for it.
    let canonical_candidate = canonicalize_existing(candidate)?;

    if !canonical_candidate.starts_with(&canonical_root) {
        return Err(outside_root_error(candidate));
    }

    Ok(canonical_candidate)
}

/// Classifies a dropped path against FR-6: rejects directories (via a
/// metadata check only — never traversing their contents) and validates the
/// extension case-insensitively against `allowlist`.
///
/// Containment is not this function's concern — callers run
/// [`ensure_inside`] first.
pub fn classify_drop(path: &Path, allowlist: &[&str]) -> Result<(), AppError> {
    let metadata = std::fs::metadata(path)
        .map_err(|_| AppError::not_a_file(format!("{} does not exist", display(path))))?;

    if metadata.is_dir() {
        return Err(AppError::not_a_file(format!(
            "{} is a directory, not a recording file",
            display(path)
        )));
    }

    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| display(path));

    match path
        .extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase())
    {
        Some(ext)
            if allowlist
                .iter()
                .any(|allowed| allowed.eq_ignore_ascii_case(&ext)) =>
        {
            Ok(())
        }
        Some(ext) => Err(AppError::unsupported_extension(format!(
            "{name} has an unsupported extension \".{ext}\""
        ))),
        None => Err(AppError::unsupported_extension(format!(
            "{name} has no file extension"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn path_inside_root_is_accepted_and_returned_canonical() {
        let root = tempdir().expect("tempdir");
        let file = root.path().join("good.mp4");
        fs::write(&file, b"data").expect("write fixture");

        let resolved = ensure_inside(root.path(), &file).expect("should be inside root");

        let canonical_root = fs::canonicalize(root.path()).expect("canonicalize root");
        assert!(resolved.starts_with(&canonical_root));
        assert!(resolved.ends_with("good.mp4"));
    }

    #[test]
    fn parent_traversal_is_refused_before_any_write() {
        let root = tempdir().expect("tempdir");
        // Neither `evil.mp4` nor its parent directory exists — proving the
        // rejection happened lexically, without ever needing the candidate
        // to exist on disk.
        let escaping = root.path().join("..").join("evil.mp4");

        let err = ensure_inside(root.path(), &escaping).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
        assert!(!escaping.exists(), "candidate must never be materialized");
    }

    #[test]
    fn nonexistent_sibling_path_is_refused_as_outside_root_not_as_a_missing_file() {
        let root = tempdir().expect("tempdir");
        // A path that lexically resolves to a sibling directory that does
        // not exist at all. If the escape check required canonicalizing
        // the candidate first, this would fail with a generic "missing"
        // error instead of `outside_root` — proving the lexical check runs
        // first.
        let escaping = root
            .path()
            .join("..")
            .join("definitely-not-a-real-sibling-dir")
            .join("ghost.mp4");

        let err = ensure_inside(root.path(), &escaping).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }

    #[test]
    fn path_on_another_drive_is_refused() {
        let root = tempdir().expect("tempdir");
        let other_drive = PathBuf::from(r"D:\some\other\place\evil.mp4");

        let err = ensure_inside(root.path(), &other_drive).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }

    #[test]
    fn unc_path_is_refused() {
        let root = tempdir().expect("tempdir");
        let unc = PathBuf::from(r"\\server\share\evil.mp4");

        let err = ensure_inside(root.path(), &unc).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }

    #[test]
    fn verbatim_device_path_on_a_different_volume_is_refused() {
        let root = tempdir().expect("tempdir");
        let device_path =
            PathBuf::from(r"\\?\Volume{12345678-abcd-1234-abcd-1234567890ab}\evil.mp4");

        let err = ensure_inside(root.path(), &device_path).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }

    #[test]
    fn string_prefix_match_is_not_containment() {
        let root = tempdir().expect("tempdir");
        // A sibling directory whose name merely starts with the root's own
        // directory name plus a suffix (`<root>-old`) — a string
        // `starts_with` would wrongly accept this; component comparison
        // must not.
        let mut sibling_name = root
            .path()
            .file_name()
            .expect("tempdir has a name")
            .to_os_string();
        sibling_name.push("-old");
        let sibling_root = root.path().with_file_name(sibling_name);
        fs::create_dir_all(&sibling_root).expect("create sibling dir");
        let sibling_file = sibling_root.join("x.mp4");
        fs::write(&sibling_file, b"data").expect("write fixture");

        let err = ensure_inside(root.path(), &sibling_file).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);

        fs::remove_dir_all(&sibling_root).ok();
    }

    #[test]
    fn verbatim_prefix_is_stripped_for_display_and_normalized_for_comparison() {
        let root = tempdir().expect("tempdir");
        let file = root.path().join("good.mp4");
        fs::write(&file, b"data").expect("write fixture");

        // The file's true canonical form is verbatim-prefixed on Windows.
        let canonical = fs::canonicalize(&file).expect("canonicalize fixture");
        assert!(
            canonical.to_string_lossy().starts_with(r"\\?\"),
            "test assumption: canonicalize yields a verbatim prefix on Windows"
        );

        // Passing the verbatim-prefixed form back in as the candidate must
        // still be accepted against the plain (non-canonical) root.
        let resolved = ensure_inside(root.path(), &canonical).expect("must be accepted");
        assert!(!resolved.to_string_lossy().is_empty());

        // `strip_verbatim` simplifies a plain-drive verbatim path for
        // display.
        let stripped = strip_verbatim(&canonical);
        assert!(!stripped.to_string_lossy().starts_with(r"\\?\"));

        // An outside-root error message never leaks the verbatim prefix.
        let escaping = PathBuf::from(r"\\?\D:\other\evil.mp4");
        let err = ensure_inside(root.path(), &escaping).expect_err("must be refused");
        assert!(!err.message().contains(r"\\?\"));
    }

    #[test]
    fn strip_verbatim_simplifies_a_plain_drive_path_only() {
        assert_eq!(
            strip_verbatim(Path::new(r"\\?\C:\Meetings\x.mp4")),
            PathBuf::from(r"C:\Meetings\x.mp4")
        );
        // A UNC device path is left as its simplified `\\` form, not a bare
        // strip of the `\\?\` marker alone.
        assert_eq!(
            strip_verbatim(Path::new(r"\\?\UNC\server\share\x.mp4")),
            PathBuf::from(r"\\server\share\x.mp4")
        );
        // No prefix at all: unchanged.
        assert_eq!(
            strip_verbatim(Path::new(r"C:\Meetings\x.mp4")),
            PathBuf::from(r"C:\Meetings\x.mp4")
        );
    }

    #[test]
    fn nonexistent_file_under_root_reports_not_a_file() {
        let root = tempdir().expect("tempdir");
        let missing = root.path().join("ghost.mp4");

        let err = ensure_inside(root.path(), &missing).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::NotAFile);
    }

    #[test]
    fn directory_input_is_rejected_as_not_a_file_without_traversal() {
        let root = tempdir().expect("tempdir");
        let sub_dir = root.path().join("a-directory");
        fs::create_dir(&sub_dir).expect("create dir");
        // A file inside the directory that a traversing implementation
        // might notice; classify_drop must reject on metadata alone.
        fs::write(sub_dir.join("inner.mp4"), b"data").expect("write fixture");

        let err = classify_drop(&sub_dir, supported_extensions()).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::NotAFile);
    }

    #[test]
    fn nonexistent_path_passed_to_classify_drop_reports_not_a_file() {
        let root = tempdir().expect("tempdir");
        let missing = root.path().join("ghost.mp4");

        let err = classify_drop(&missing, supported_extensions()).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::NotAFile);
    }

    #[test]
    fn extension_check_is_case_insensitive_against_the_allowlist() {
        let root = tempdir().expect("tempdir");
        let upper = root.path().join("GOOD.MP4");
        fs::write(&upper, b"data").expect("write fixture");

        classify_drop(&upper, supported_extensions()).expect("uppercase extension is accepted");
    }

    #[test]
    fn unsupported_extension_names_the_file_and_extension() {
        let root = tempdir().expect("tempdir");
        let text_file = root.path().join("notes.txt");
        fs::write(&text_file, b"data").expect("write fixture");

        let err = classify_drop(&text_file, supported_extensions()).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::UnsupportedExtension);
        assert!(err.message().contains("notes.txt"));
        assert!(err.message().contains("txt"));
    }

    #[test]
    fn reveal_target_outside_root_is_refused() {
        let root = tempdir().expect("tempdir");
        let other = tempdir().expect("other tempdir");
        let meeting_dir = other.path().join("ELS").join("260812 - Security issue");
        fs::create_dir_all(&meeting_dir).expect("create fixture dir");

        let err = ensure_inside(root.path(), &meeting_dir).expect_err("must be refused");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }

    #[test]
    fn supported_extensions_mirrors_vaults_real_allowlist() {
        // Documents the FR-6/vault divergence the plan flags: `.avi` is
        // accepted (vault accepts it), `.aac` is not (vault does not),
        // even though this spec's FR-6 text says the opposite.
        let list = supported_extensions();
        assert!(list.contains(&"avi"));
        assert!(!list.contains(&"aac"));
        assert_eq!(list.len(), 10);
    }

    /// Creates a directory junction at `link` pointing at `target`, or
    /// returns `false` if this environment will not make one.
    ///
    /// `mklink /J` rather than `std::os::windows::fs::symlink_dir`: a
    /// junction needs no privilege, while a directory *symlink* needs either
    /// admin or Developer Mode, which a CI runner may not have.
    fn make_junction(link: &std::path::Path, target: &std::path::Path) -> bool {
        std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(link)
            .arg(target)
            .output()
            .map(|out| out.status.success())
            .unwrap_or(false)
    }

    #[test]
    fn a_root_whose_spelling_is_not_canonical_still_contains_its_own_children() {
        // The bug this pins: a configured root can be spelled differently
        // from what `canonicalize` returns -- through a junction here, and
        // through an 8.3 short component (`C:\Users\RUNNER~1\...`, how
        // `%TEMP%` is exposed on a Windows CI runner) in the wild. Candidates
        // reach `ensure_inside` in *both* spellings: `list_vault` reads the
        // configured root's own directory entries, while ingest and reveal
        // take theirs from F1, which canonicalizes. Comparing against only
        // one spelling rejected every path built from the other, and the
        // whole ingest pipeline failed with `outside_root`.
        let dir = tempdir().expect("tempdir");
        let real_root = dir.path().join("real-meetings-root");
        fs::create_dir_all(&real_root).expect("create real root");
        let meeting_dir = real_root.join("ELS").join("260812 - Security issue");
        fs::create_dir_all(&meeting_dir).expect("create meeting dir");

        let junction_root = dir.path().join("via-junction");
        if !make_junction(&junction_root, &real_root) {
            eprintln!("skipping: this environment will not create a junction");
            return;
        }

        // The candidate spelled the way F1 hands it back (canonical).
        let canonical_meeting = canonicalize_existing(&meeting_dir).expect("canonicalize meeting");
        ensure_inside(&junction_root, &canonical_meeting)
            .expect("a canonical child of a junction-spelled root is inside it");

        // And spelled the way `list_vault` builds it (root's own spelling).
        let via_junction = junction_root.join("ELS").join("260812 - Security issue");
        ensure_inside(&junction_root, &via_junction)
            .expect("a child built from the root's own spelling is inside it");
    }

    #[test]
    fn a_non_canonical_root_still_refuses_a_sibling_outside_it() {
        // The other half of the same change: accepting either spelling must
        // not accept a path that is genuinely outside.
        let dir = tempdir().expect("tempdir");
        let real_root = dir.path().join("real-meetings-root");
        fs::create_dir_all(&real_root).expect("create real root");
        let outsider = dir.path().join("somewhere-else");
        fs::create_dir_all(&outsider).expect("create outsider");

        let junction_root = dir.path().join("via-junction");
        if !make_junction(&junction_root, &real_root) {
            eprintln!("skipping: this environment will not create a junction");
            return;
        }

        let err = ensure_inside(&junction_root, &outsider)
            .expect_err("a sibling of the root's target is not inside the root");
        assert_eq!(err.kind(), crate::error::ErrorKind::OutsideRoot);
    }
}
