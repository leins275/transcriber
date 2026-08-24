//! Read-only enumeration of project-level artifacts (action items, facts,
//! reports) written by F2's LLM jobs.
//!
//! Same contract as [`crate::list`]: best-effort against a directory tree
//! never fully under this crate's control — junk entries are skipped, no
//! `Result` anywhere, an absent directory is an empty listing. Nothing here
//! creates, writes, renames or deletes anything, and nothing reads more
//! than one `.md` file's presence per item — content stays with the caller
//! (F3 reads it behind size caps).
//!
//! # Front-matter field contract (mirror)
//!
//! Item `.md` files open with a `---` fenced block of `key: <json value>`
//! lines — JSON is a YAML subset, so external property editors (Obsidian
//! and friends) read it as ordinary YAML front matter. The field set is a
//! cross-language contract, exactly like the directory names in
//! [`crate::paths`].
//!
//! **The Python side owns it.** The source of truth is the module docstring
//! of `services/transcription/src/transcription/artifacts.py` together with
//! the key-set test in `services/transcription/tests/test_llm_jobs.py`,
//! which fails CI on any drift. What follows is a mirror kept in sync by
//! hand; if the two ever disagree, Python wins. Nothing in this crate parses
//! front matter today, but any Rust code that later reads or writes it must
//! use these names verbatim.
//!
//! Every key below is written on every extraction item (action items and
//! facts share one writer, `jobs._extract_sync`):
//!
//! - `type` / `kind` — string, non-null. `type` for action items, `kind`
//!   for facts; the only difference between the two key sets.
//! - `title` — string, non-null.
//! - `archived` — boolean, non-null. Always written `false`; flipped only
//!   by an external editor. An absent key reads as false.
//! - `source_project` — string, nullable. The vault project folder holding
//!   the meeting; `null` when the meeting lives under the reserved
//!   [`crate::paths::UNSORTED_DIR_NAME`] root — never the literal string
//!   `"unsorted"` posing as a project.
//! - `source_meeting` — string, non-null. The meeting folder's name.
//! - `source_recording` — string, nullable. The stored `source.<ext>`
//!   filename; `null` when the meeting folder has none.
//! - `source_date` — string `YYYY-MM-DD`, nullable. Parsed from the
//!   meeting folder's leading `YYMMDD` (the naming contract in
//!   [`crate::paths`]), century fixed at 20xx; `null` when unparseable.
//! - `timestamps` — number array, non-null. Transcript offsets in seconds.
//! - `created` — string, non-null. ISO datetime, UTC.
//! - `model` — string, non-null.
//! - `job_id` — string, non-null.
//! - `screenshots` — string, non-null. The screenshot-capture status value.
//!
//! Two clauses are behaviour rather than fields:
//!
//! - **Unknown keys survive.** Hand-edited front matter — reordered keys,
//!   YAML-quoted strings, extra keys, `archived` flipped to `true` — round-
//!   trips through the Python reader; a non-JSON value degrades to its raw
//!   string and parsing never fails.
//! - **No code path rewrites an existing artifact `.md`.** After its atomic
//!   creation an item file is read-only to this app — this module included,
//!   which reads only a file's *presence*. A future mutation feature must
//!   round-trip unknown keys and the body byte-exactly outside the keys it
//!   changes.
//!
//! Neither side acts on `archived`: listings and exports include archived
//! items exactly like unarchived ones. It exists for the operator's
//! external tools.
//!
//! Sibling features may relocate or delete this module (artifacts moving
//! into the per-meeting folder; the project-view artifact UI going away).
//! If it is deleted, the contract's single home is the Python side named
//! above — this mirror is a convenience, never the authority.

use std::fs;
use std::path::{Path, PathBuf};

use crate::paths::{ACTION_ITEMS_DIR_NAME, FACTS_DIR_NAME, REPORTS_DIR_NAME};

/// Which project-level artifact directory to enumerate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactKind {
    /// `<PROJECT>/action items/`
    ActionItems,
    /// `<PROJECT>/facts/`
    Facts,
}

impl ArtifactKind {
    /// The reserved on-disk directory name for this kind.
    pub fn dir_name(self) -> &'static str {
        match self {
            ArtifactKind::ActionItems => ACTION_ITEMS_DIR_NAME,
            ArtifactKind::Facts => FACTS_DIR_NAME,
        }
    }
}

/// One artifact item folder: `<PROJECT>/<kind>/<slug>/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactEntry {
    /// The item folder's name.
    pub slug: String,
    /// Absolute path to the item folder.
    pub dir: PathBuf,
    /// Absolute path to the item's `<slug>.md` (exists at listing time).
    pub md_path: PathBuf,
    /// The `.png` screenshot filenames directly inside the folder, sorted.
    pub screenshot_names: Vec<String>,
}

/// One dated report folder: `<PROJECT>/reports/<name>/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReportEntry {
    /// The report folder's name (a `YYMMDD` date, possibly suffixed).
    pub name: String,
    /// Absolute path to the report folder.
    pub dir: PathBuf,
    /// `report.md`, when present.
    pub md_path: Option<PathBuf>,
    /// `report.pdf`, when present.
    pub pdf_path: Option<PathBuf>,
}

/// Finds the project directory under `root` whose name matches `project`
/// case-insensitively (project-folder reuse is case-insensitive throughout
/// this crate).
fn project_dir(root: &Path, project: &str) -> Option<PathBuf> {
    let entries = fs::read_dir(root).ok()?;
    for entry in entries.flatten() {
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if name.eq_ignore_ascii_case(project) {
            return Some(entry.path());
        }
    }
    None
}

/// Lists every item under `<root>/<project>/<kind dir>/`, sorted by slug.
///
/// An item is a subdirectory containing `<its own name>.md`; anything else
/// at that level is skipped. Returns an empty `Vec` (never an error) for a
/// missing project or kind directory.
pub fn list_project_artifacts(
    root: &Path,
    project: &str,
    kind: ArtifactKind,
) -> Vec<ArtifactEntry> {
    let Some(project_path) = project_dir(root, project) else {
        return Vec::new();
    };
    let kind_dir = project_path.join(kind.dir_name());
    let Ok(children) = fs::read_dir(&kind_dir) else {
        return Vec::new();
    };

    let mut items = Vec::new();
    for child in children.flatten() {
        let Ok(file_type) = child.file_type() else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let Some(slug) = child.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let dir = child.path();
        let md_path = dir.join(format!("{slug}.md"));
        if !md_path.is_file() {
            continue;
        }
        let mut screenshot_names: Vec<String> = fs::read_dir(&dir)
            .map(|entries| {
                entries
                    .flatten()
                    .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
                    .filter_map(|e| e.file_name().to_str().map(str::to_owned))
                    .filter(|name| name.to_ascii_lowercase().ends_with(".png"))
                    .collect()
            })
            .unwrap_or_default();
        screenshot_names.sort();
        items.push(ArtifactEntry {
            slug,
            dir,
            md_path,
            screenshot_names,
        });
    }
    items.sort_by_key(|item| item.slug.to_lowercase());
    items
}

/// Lists every dated report folder under `<root>/<project>/reports/`,
/// newest name first (names are `YYMMDD`-prefixed, so lexical order is
/// date order).
pub fn list_reports(root: &Path, project: &str) -> Vec<ReportEntry> {
    let Some(project_path) = project_dir(root, project) else {
        return Vec::new();
    };
    let reports_dir = project_path.join(REPORTS_DIR_NAME);
    let Ok(children) = fs::read_dir(&reports_dir) else {
        return Vec::new();
    };

    let mut reports = Vec::new();
    for child in children.flatten() {
        let Ok(file_type) = child.file_type() else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let Some(name) = child.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let dir = child.path();
        let md = dir.join("report.md");
        let pdf = dir.join("report.pdf");
        let md_path = md.is_file().then_some(md);
        let pdf_path = pdf.is_file().then_some(pdf);
        if md_path.is_none() && pdf_path.is_none() {
            continue;
        }
        reports.push(ReportEntry {
            name,
            dir,
            md_path,
            pdf_path,
        });
    }
    reports.sort_by(|a, b| b.name.cmp(&a.name));
    reports
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn write(path: &Path, content: &str) {
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        fs::write(path, content).expect("write");
    }

    #[test]
    fn missing_project_or_kind_dir_yields_an_empty_list() {
        let dir = tempdir().expect("tempdir");
        assert!(list_project_artifacts(dir.path(), "ELS", ArtifactKind::ActionItems).is_empty());
        fs::create_dir_all(dir.path().join("ELS")).expect("mkdir");
        assert!(list_project_artifacts(dir.path(), "ELS", ArtifactKind::Facts).is_empty());
        assert!(list_reports(dir.path(), "ELS").is_empty());
    }

    #[test]
    fn lists_items_with_their_screenshots_and_skips_junk() {
        let dir = tempdir().expect("tempdir");
        let items_dir = dir.path().join("ELS").join("action items");
        write(
            &items_dir.join("fix-login").join("fix-login.md"),
            "---\n---\n# Fix login\n",
        );
        fs::write(
            items_dir.join("fix-login").join("screenshot-0102.png"),
            b"png",
        )
        .expect("write png");
        // Junk: a folder without a matching md, and a stray file.
        fs::create_dir_all(items_dir.join("empty-folder")).expect("mkdir");
        fs::write(items_dir.join("stray.txt"), b"junk").expect("write junk");

        let items = list_project_artifacts(dir.path(), "ELS", ArtifactKind::ActionItems);

        assert_eq!(items.len(), 1);
        assert_eq!(items[0].slug, "fix-login");
        assert_eq!(items[0].screenshot_names, vec!["screenshot-0102.png"]);
        assert!(items[0].md_path.ends_with("fix-login.md"));
    }

    #[test]
    fn project_lookup_is_case_insensitive() {
        let dir = tempdir().expect("tempdir");
        write(
            &dir.path()
                .join("els")
                .join("facts")
                .join("a-fact")
                .join("a-fact.md"),
            "# A fact\n",
        );

        let items = list_project_artifacts(dir.path(), "ELS", ArtifactKind::Facts);

        assert_eq!(items.len(), 1);
    }

    #[test]
    fn reports_list_newest_first_and_require_an_artifact() {
        let dir = tempdir().expect("tempdir");
        let reports = dir.path().join("ELS").join("reports");
        write(&reports.join("260101").join("report.md"), "old");
        write(&reports.join("260812").join("report.md"), "new");
        fs::write(reports.join("260812").join("report.pdf"), b"%PDF-").expect("write pdf");
        fs::create_dir_all(reports.join("260901")).expect("empty report dir");

        let listed = list_reports(dir.path(), "ELS");

        assert_eq!(
            listed.iter().map(|r| r.name.as_str()).collect::<Vec<_>>(),
            vec!["260812", "260101"]
        );
        assert!(listed[0].pdf_path.is_some());
        assert!(listed[1].pdf_path.is_none());
    }
}
