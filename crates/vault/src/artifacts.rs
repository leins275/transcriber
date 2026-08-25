//! Reading the action items that F2's LLM extraction job writes into
//! `<meeting>/action items/<slug>/`.
//!
//! Two other trees carry the same file shape but are *legacy* and never
//! touched by the app: the project-level `<PROJECT>/action items/` and
//! `<PROJECT>/facts/` trees an older build wrote, and the per-meeting
//! `<meeting>/facts/` trees from before the facts job was retired (the
//! summary carries the notable facts now). None of them are migrated or
//! deleted — they stay on disk for the operator's external tools.
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
//! hand; if the two ever disagree, Python wins. [`read_item`] here mirrors
//! the Python reader's semantics and must keep doing so.
//!
//! Every key below is written on every extraction item by
//! `jobs._extract_sync`:
//!
//! - `type` — string, non-null. The action-item type (legacy facts items
//!   carry `kind` instead).
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
//!   trips through both readers; a non-JSON value degrades to its raw
//!   string and parsing never fails.
//! - **No code path rewrites an existing artifact `.md`.** After its atomic
//!   creation an item file is read-only everywhere — the reading here never
//!   touches bytes, and the on-demand screenshot capture only *adds* PNG
//!   files beside the `.md`. A future mutation feature must round-trip
//!   unknown keys and the body byte-exactly outside the keys it changes.
//!
//! Neither side acts on `archived`: listings and exports include archived
//! items exactly like unarchived ones. It exists for the operator's
//! external tools.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::paths::ACTION_ITEMS_DIR_NAME;

/// One extraction item read back from an item directory.
#[derive(Debug, Clone, PartialEq)]
pub struct StoredItem {
    /// The item's directory (`<meeting>/action items/<slug>`).
    pub dir: PathBuf,
    /// Front matter, each value parsed as JSON where possible; a value that
    /// is not valid JSON degrades to a `Value::String` of the raw text (the
    /// Python reader's rule).
    pub meta: BTreeMap<String, Value>,
    /// The markdown body below the front matter (starts with the item's own
    /// `# title` heading, exactly as written).
    pub body_md: String,
    /// The `.png` files sitting in the item directory, sorted by name —
    /// extraction-time captures and on-demand captures alike.
    pub screenshot_names: Vec<String>,
}

/// Parses a leading `---` front-matter block; returns `(meta, body)`.
///
/// Best-effort, mirroring the Python `parse_front_matter`: a missing or
/// unterminated block yields an empty map with the whole text as body, and
/// a malformed value line is skipped rather than failing the read. Handles
/// CRLF files (the Python writer emits platform line endings).
fn parse_front_matter(text: &str) -> (BTreeMap<String, Value>, String) {
    let lines: Vec<&str> = text.lines().collect();
    if lines.first().map(|line| line.trim_end()) != Some("---") {
        return (BTreeMap::new(), text.to_string());
    }
    let Some(end) = lines
        .iter()
        .enumerate()
        .skip(1)
        .find(|(_, line)| line.trim_end() == "---")
        .map(|(index, _)| index)
    else {
        return (BTreeMap::new(), text.to_string());
    };

    let mut meta = BTreeMap::new();
    for line in &lines[1..end] {
        let Some((key, raw_value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let raw_value = raw_value.trim();
        let value = serde_json::from_str(raw_value)
            .unwrap_or_else(|_| Value::String(raw_value.to_string()));
        meta.insert(key.to_string(), value);
    }
    let body = lines[end + 1..].join("\n");
    (meta, body.trim_start_matches('\n').to_string())
}

/// Reads one `<kind>/<slug>/` item directory; `None` when it holds no
/// readable `<slug>.md` (best-effort, like the listing).
pub fn read_item(item_dir: &Path) -> Option<StoredItem> {
    let name = item_dir.file_name()?.to_str()?;
    let md_path = item_dir.join(format!("{name}.md"));
    let text = std::fs::read_to_string(&md_path).ok()?;
    let (meta, body_md) = parse_front_matter(&text);

    let mut screenshot_names: Vec<String> = std::fs::read_dir(item_dir)
        .ok()?
        .flatten()
        .filter(|entry| entry.path().is_file())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|file_name| {
            Path::new(file_name)
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("png"))
        })
        .collect();
    screenshot_names.sort();

    Some(StoredItem {
        dir: item_dir.to_path_buf(),
        meta,
        body_md,
        screenshot_names,
    })
}

/// Reads every item under `<meeting>/action items/`, sorted by folder name
/// (case-insensitively, matching the Python listing). Best-effort: a
/// subdirectory without a readable `.md` is skipped, a missing kind
/// directory is an empty list, never an error.
pub fn list_action_items(meeting_dir: &Path) -> Vec<StoredItem> {
    let kind_dir = meeting_dir.join(ACTION_ITEMS_DIR_NAME);
    let Ok(entries) = std::fs::read_dir(&kind_dir) else {
        return Vec::new();
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    dirs.sort_by_key(|path| {
        path.file_name()
            .map(|name| name.to_string_lossy().to_lowercase())
            .unwrap_or_default()
    });
    dirs.iter().filter_map(|dir| read_item(dir)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn write_fixture_item(kind_dir: &Path, slug: &str, front: &str, body: &str) -> PathBuf {
        let dir = kind_dir.join(slug);
        fs::create_dir_all(&dir).expect("create item dir");
        fs::write(dir.join(format!("{slug}.md")), format!("{front}\n\n{body}")).expect("write md");
        dir
    }

    const FRONT: &str = "---\ntype: \"task\"\ntitle: \"Fix login\"\narchived: false\nsource_project: null\ntimestamps: [10.0, 20.0]\n---";

    #[test]
    fn reads_front_matter_body_and_screenshots() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let kind_dir = tmp.path().join(ACTION_ITEMS_DIR_NAME);
        let dir = write_fixture_item(&kind_dir, "fix-login", FRONT, "# Fix login\n\nBody text.");
        fs::write(dir.join("screenshot-0010.png"), b"fake").expect("png");
        fs::write(dir.join("notes.txt"), b"not a png").expect("txt");

        let items = list_action_items(tmp.path());
        assert_eq!(items.len(), 1);
        let item = &items[0];
        assert_eq!(item.meta["type"], Value::String("task".into()));
        assert_eq!(item.meta["title"], Value::String("Fix login".into()));
        assert_eq!(item.meta["archived"], Value::Bool(false));
        assert_eq!(item.meta["source_project"], Value::Null);
        assert_eq!(item.meta["timestamps"], serde_json::json!([10.0, 20.0]));
        assert!(item.body_md.starts_with("# Fix login"));
        assert!(item.body_md.contains("Body text."));
        assert_eq!(item.screenshot_names, vec!["screenshot-0010.png"]);
    }

    #[test]
    fn crlf_files_and_non_json_values_degrade_gracefully() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let kind_dir = tmp.path().join(ACTION_ITEMS_DIR_NAME);
        // CRLF everywhere (the Python writer's platform line endings on
        // Windows), a YAML-ish unquoted value, and an unknown key.
        let text = "---\r\ntype: \"task\"\r\ntitle: plain unquoted title\r\ncustom_key: 7\r\n---\r\n\r\n# T\r\nbody\r\n";
        let dir = kind_dir.join("t");
        fs::create_dir_all(&dir).expect("dir");
        fs::write(dir.join("t.md"), text).expect("md");

        let item = read_item(&dir).expect("readable item");
        assert_eq!(
            item.meta["title"],
            Value::String("plain unquoted title".into()),
            "a non-JSON value degrades to its raw string"
        );
        assert_eq!(
            item.meta["custom_key"],
            serde_json::json!(7),
            "unknown keys survive"
        );
        assert_eq!(item.body_md, "# T\nbody");
    }

    #[test]
    fn listing_is_best_effort_and_sorted() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let kind_dir = tmp.path().join(ACTION_ITEMS_DIR_NAME);
        write_fixture_item(&kind_dir, "b-second", FRONT, "# b");
        write_fixture_item(&kind_dir, "A-first", FRONT, "# a");
        // A junk directory without its md is skipped, never an error.
        fs::create_dir_all(kind_dir.join("junk")).expect("junk dir");
        // A stray file at the kind level is ignored.
        fs::write(kind_dir.join("stray.txt"), b"x").expect("stray");

        let names: Vec<String> = list_action_items(tmp.path())
            .iter()
            .map(|item| {
                item.dir
                    .file_name()
                    .expect("dir name")
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        assert_eq!(names, vec!["A-first", "b-second"]);
    }

    #[test]
    fn a_meeting_without_the_directory_lists_empty() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(list_action_items(tmp.path()).is_empty());
    }

    #[test]
    fn missing_or_unterminated_front_matter_is_all_body() {
        let (meta, body) = parse_front_matter("plain text");
        assert!(meta.is_empty());
        assert_eq!(body, "plain text");

        let (meta, body) = parse_front_matter("---\ntype: \"task\"\nno closing fence");
        assert!(meta.is_empty());
        assert!(body.starts_with("---"));
    }
}
