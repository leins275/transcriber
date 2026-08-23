//! Vault artifact documents: front matter, slugs, and item folders.
//!
//! Port of `services/transcription/src/transcription/artifacts.py` -- the
//! *writing* half. The reading half that only enumerates folders already
//! lives in [`vault::artifacts`]; what is here is everything that touches a
//! document's content or decides its name.
//!
//! Front matter is written as `key: <json value>` lines. JSON is a YAML
//! subset, so the block reads as ordinary YAML front matter to humans and
//! external tools while staying trivially parseable here without a YAML
//! dependency.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::atomic;

/// The path budget and the reserved directory names come from the vault
/// crate, which owns them for every other folder in the tree.
pub use vault::paths::{
    ACTION_ITEMS_DIR_NAME, EXPORTS_DIR_NAME, FACTS_DIR_NAME, MAX_PATH_LEN, REPORTS_DIR_NAME,
};

/// The longest sibling filename that must also fit in the budget:
/// `screenshot-hmmss.png` is 20 characters.
const LONGEST_SIBLING: usize = 20;

const MAX_SLUG_CHARS: usize = 60;

/// Characters trimmed from a slug's ends: Windows silently drops trailing
/// dots and spaces, and a leading or trailing dash is just ugly.
const TRIM_CHARS: [char; 3] = ['-', '.', ' '];

/// Why an artifact could not be written.
#[derive(Debug)]
pub enum ArtifactError {
    /// The vault root is so deep that even a one-character slug would exceed
    /// the Windows path budget.
    PathTooDeep {
        parent: PathBuf,
    },
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl std::fmt::Display for ArtifactError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ArtifactError::PathTooDeep { parent } => write!(
                f,
                "vault path is too deep to store artifacts under {}",
                parent
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_else(|| parent.display().to_string())
            ),
            ArtifactError::Io(e) => write!(f, "{e}"),
            ArtifactError::Json(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ArtifactError {}

impl From<std::io::Error> for ArtifactError {
    fn from(e: std::io::Error) -> Self {
        ArtifactError::Io(e)
    }
}

impl From<serde_json::Error> for ArtifactError {
    fn from(e: serde_json::Error) -> Self {
        ArtifactError::Json(e)
    }
}

/// A Windows-safe, human-readable folder slug from an item title.
///
/// Lowercased, illegal/control characters and whitespace to `-`, runs
/// collapsed, trimmed of dots/spaces/dashes, capped at 60 characters (on a
/// character boundary). Non-Latin text passes through: Windows folder names
/// are Unicode, and only the illegal set is replaced -- a Cyrillic title stays
/// a Cyrillic folder.
///
/// One deliberate divergence from the Python original: it lowercases with
/// `str.casefold()`, this uses `to_lowercase()`. They differ only for a
/// handful of characters (`ß` casefolds to `ss`), and lowercase is what the
/// rest of this workspace's case-insensitive comparisons already use.
pub fn slugify(title: &str, fallback: &str) -> String {
    let replaced: String = title
        .trim()
        .to_lowercase()
        .chars()
        .map(|c| {
            if vault::paths::is_illegal_char(c) || c.is_whitespace() {
                '-'
            } else {
                c
            }
        })
        .collect();

    // Collapse dash runs, which the two substitutions above create freely.
    let mut collapsed = String::with_capacity(replaced.len());
    let mut last_was_dash = false;
    for c in replaced.chars() {
        if c == '-' {
            if !last_was_dash {
                collapsed.push(c);
            }
            last_was_dash = true;
        } else {
            collapsed.push(c);
            last_was_dash = false;
        }
    }

    let trimmed = collapsed.trim_matches(|c| TRIM_CHARS.contains(&c));
    let capped: String = trimmed.chars().take(MAX_SLUG_CHARS).collect();
    let capped = capped.trim_end_matches(|c| TRIM_CHARS.contains(&c));

    if capped.is_empty() {
        fallback.to_string()
    } else {
        capped.to_string()
    }
}

/// Trim `slug` until `parent/slug/<slug>.md` -- and the longest screenshot
/// sibling next to it -- fits the Windows path budget.
///
/// Fails only when even a single character cannot fit, which means the vault
/// root itself is too deep for artifacts.
pub fn fit_slug(parent: &Path, slug: &str) -> Result<String, ArtifactError> {
    let base_len = parent.display().to_string().chars().count() + 1; // parent + separator
    let mut candidate: Vec<char> = slug.chars().collect();

    while !candidate.is_empty() {
        // parent/slug/<slug>.md and parent/slug/screenshot-....png, plus room
        // for a " (n)" collision suffix on the directory.
        let dir_len = base_len + candidate.len() + 4;
        let leaf = (candidate.len() + ".md".len()).max(LONGEST_SIBLING);
        if dir_len + 1 + leaf <= MAX_PATH_LEN {
            return Ok(candidate.into_iter().collect());
        }
        candidate.pop();
        while candidate.last().is_some_and(|c| TRIM_CHARS.contains(c)) {
            candidate.pop();
        }
    }

    Err(ArtifactError::PathTooDeep {
        parent: parent.to_path_buf(),
    })
}

/// Create and return `parent/slug`, suffixing ` (n)` on collision (the vault
/// crate's `suffixed` convention).
pub fn unique_item_dir(parent: &Path, slug: &str) -> Result<PathBuf, ArtifactError> {
    fs::create_dir_all(parent)?;
    let fitted = fit_slug(parent, slug)?;

    let mut candidate = parent.join(&fitted);
    let mut n = 1u32;
    while candidate.exists() {
        n += 1;
        candidate = parent.join(vault::paths::suffixed(&fitted, n));
    }
    fs::create_dir(&candidate)?;
    Ok(candidate)
}

/// A `---` block with one `key: <json>` line per entry (YAML-compatible).
pub fn render_front_matter(meta: &Map<String, Value>) -> Result<String, ArtifactError> {
    let mut lines = vec!["---".to_string()];
    for (key, value) in meta {
        lines.push(format!("{key}: {}", crate::python_json::to_string(value)?));
    }
    lines.push("---".to_string());
    Ok(lines.join("\n"))
}

/// Parse a leading `---` front-matter block; returns `(meta, body)`.
///
/// Best-effort: a missing or malformed block yields an empty map and the
/// original text, and a value line that is not JSON is kept as a raw string
/// rather than failing the whole read. A half-written artifact should still
/// show its body.
pub fn parse_front_matter(text: &str) -> (Map<String, Value>, String) {
    let empty = Map::new();
    if !text.starts_with("---") {
        return (empty, text.to_string());
    }
    let lines: Vec<&str> = text.lines().collect();
    let Some(end) = lines
        .iter()
        .skip(1)
        .position(|l| *l == "---")
        .map(|i| i + 1)
    else {
        return (empty, text.to_string());
    };

    let mut meta = Map::new();
    for line in &lines[1..end] {
        let Some((key, raw_value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let raw_value = raw_value.trim();
        let value = serde_json::from_str::<Value>(raw_value)
            .unwrap_or_else(|_| Value::String(raw_value.to_string()));
        meta.insert(key.to_string(), value);
    }

    let body = lines[end + 1..]
        .join("\n")
        .trim_start_matches('\n')
        .to_string();
    (meta, body)
}

/// One item read back from an artifact directory.
#[derive(Debug, Clone, PartialEq)]
pub struct StoredItem {
    pub dir: PathBuf,
    pub md_path: PathBuf,
    pub meta: Map<String, Value>,
    pub body: String,
    pub screenshot_names: Vec<String>,
}

/// Write one extraction item: `parent/<slug>/<slug>.md` plus its screenshots.
///
/// Images land first, then the markdown atomically -- the `.md` is the commit
/// point, so a crash never leaves an item referencing missing files. An
/// imageless directory left by such a crash is inert junk: the listing skips
/// it (no `.md`), and the next write's collision suffixing steps over it.
pub fn write_item(
    parent_dir: &Path,
    title: &str,
    meta: &Map<String, Value>,
    body_md: &str,
    images: &[(String, Vec<u8>)],
) -> Result<PathBuf, ArtifactError> {
    let item_dir = unique_item_dir(parent_dir, &slugify(title, "item"))?;
    for (name, data) in images {
        fs::write(item_dir.join(name), data)?;
    }

    let mut sections = vec![
        render_front_matter(meta)?,
        String::new(),
        format!("# {title}"),
        String::new(),
    ];
    if !body_md.trim().is_empty() {
        sections.push(body_md.trim().to_string());
        sections.push(String::new());
    }
    if !images.is_empty() {
        let refs: Vec<String> = images
            .iter()
            .map(|(name, _)| format!("![{name}]({name})"))
            .collect();
        sections.push(refs.join("\n"));
        sections.push(String::new());
    }

    let dir_name = item_dir
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let md_path = item_dir.join(format!("{dir_name}.md"));
    atomic::write_text(&md_path, &sections.join("\n"))?;
    Ok(md_path)
}

/// Read every item under an `action items/` or `facts/` directory.
///
/// Best-effort like [`vault::artifacts`]: a subdirectory without a readable
/// `.md` is skipped, never an error. Sorted by folder name for a stable order.
pub fn list_items(kind_dir: &Path) -> Vec<StoredItem> {
    let Ok(entries) = fs::read_dir(kind_dir) else {
        return Vec::new();
    };

    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .map(|e| e.path())
        .collect();
    dirs.sort_by_key(|p| {
        p.file_name()
            .map(|n| n.to_string_lossy().to_lowercase())
            .unwrap_or_default()
    });

    let mut items = Vec::new();
    for dir in dirs {
        let Some(name) = dir.file_name().map(|n| n.to_string_lossy().into_owned()) else {
            continue;
        };
        let md_path = dir.join(format!("{name}.md"));
        let Ok(text) = fs::read_to_string(&md_path) else {
            continue;
        };
        let (meta, body) = parse_front_matter(&text);

        let mut screenshot_names: Vec<String> = fs::read_dir(&dir)
            .map(|entries| {
                entries
                    .flatten()
                    .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .filter(|n| n.to_lowercase().ends_with(".png"))
                    .collect()
            })
            .unwrap_or_default();
        screenshot_names.sort();

        items.push(StoredItem {
            dir,
            md_path,
            meta,
            body,
            screenshot_names,
        });
    }
    items
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn meta_of(pairs: &[(&str, Value)]) -> Map<String, Value> {
        let mut m = Map::new();
        for (k, v) in pairs {
            m.insert((*k).to_string(), v.clone());
        }
        m
    }

    #[test]
    fn slugify_lowercases_and_dashes_whitespace() {
        assert_eq!(slugify("Security Issue", "item"), "security-issue");
    }

    #[test]
    fn slugify_replaces_illegal_characters_and_collapses_runs() {
        assert_eq!(slugify("a/b\\c:d", "item"), "a-b-c-d");
        assert_eq!(slugify("a   ///   b", "item"), "a-b");
    }

    #[test]
    fn slugify_trims_dots_spaces_and_dashes_from_the_ends() {
        // Windows silently drops trailing dots and spaces, so a slug must
        // never end in one -- the folder would not have the name we think.
        assert_eq!(slugify("  ...hello...  ", "item"), "hello");
    }

    #[test]
    fn slugify_keeps_cyrillic() {
        assert_eq!(slugify("Отчёт по проекту", "item"), "отчёт-по-проекту");
    }

    #[test]
    fn slugify_caps_at_sixty_characters_without_a_trailing_dash() {
        let slug = slugify(&"word ".repeat(40), "item");
        assert_eq!(slug.chars().count(), 59, "{slug}");
        assert!(!slug.ends_with('-'), "{slug}");
    }

    #[test]
    fn slugify_falls_back_when_nothing_survives() {
        assert_eq!(slugify("///", "item"), "item");
        assert_eq!(slugify("   ", "fact"), "fact");
    }

    #[test]
    fn fit_slug_keeps_a_short_slug_untouched() {
        let parent = Path::new("C:\\vault\\ELS\\action items");
        assert_eq!(
            fit_slug(parent, "security-issue").unwrap(),
            "security-issue"
        );
    }

    #[test]
    fn fit_slug_trims_until_the_path_budget_fits() {
        let deep = format!("C:\\{}", "d".repeat(150));
        let parent = PathBuf::from(deep);
        let fitted = fit_slug(&parent, &"s".repeat(80)).unwrap();
        assert!(fitted.len() < 80, "expected trimming, got {fitted}");

        let full = parent.join(&fitted).join(format!("{fitted}.md"));
        assert!(full.display().to_string().chars().count() <= MAX_PATH_LEN);
    }

    #[test]
    fn fit_slug_rejects_a_root_too_deep_for_any_slug() {
        let parent = PathBuf::from(format!("C:\\{}", "d".repeat(255)));
        assert!(matches!(
            fit_slug(&parent, "anything"),
            Err(ArtifactError::PathTooDeep { .. })
        ));
    }

    #[test]
    fn unique_item_dir_suffixes_on_collision() {
        let root = tempfile::tempdir().unwrap();
        let a = unique_item_dir(root.path(), "note").unwrap();
        let b = unique_item_dir(root.path(), "note").unwrap();
        assert_eq!(a.file_name().unwrap(), "note");
        assert_eq!(b.file_name().unwrap(), "note (2)");
    }

    #[test]
    fn front_matter_renders_json_values_in_order() {
        let meta = meta_of(&[
            ("kind", json!("action_item")),
            ("meeting", json!("260812 - Demo")),
            ("timestamps", json!([1.5, 2.0])),
        ]);
        assert_eq!(
            render_front_matter(&meta).unwrap(),
            "---\nkind: \"action_item\"\nmeeting: \"260812 - Demo\"\ntimestamps: [1.5, 2.0]\n---"
        );
    }

    #[test]
    fn front_matter_round_trips() {
        let meta = meta_of(&[("kind", json!("fact")), ("confidence", json!(0.8))]);
        let text = format!("{}\n\nbody text\n", render_front_matter(&meta).unwrap());
        let (parsed, body) = parse_front_matter(&text);
        assert_eq!(parsed, meta);
        // No trailing newline: the body is rejoined from split lines, so a
        // final line terminator is not part of it. Matches Python's
        // `"\n".join(lines[end + 1:]).lstrip("\n")`.
        assert_eq!(body, "body text");
    }

    #[test]
    fn front_matter_parsing_survives_a_malformed_value_line() {
        let text = "---\ngood: \"yes\"\nbad: not json\n---\nbody";
        let (meta, body) = parse_front_matter(text);
        assert_eq!(meta.get("good"), Some(&json!("yes")));
        // Kept raw rather than dropped: the reader still learns what it said.
        assert_eq!(meta.get("bad"), Some(&json!("not json")));
        assert_eq!(body, "body");
    }

    #[test]
    fn text_without_front_matter_is_all_body() {
        let (meta, body) = parse_front_matter("# Title\n\nbody");
        assert!(meta.is_empty());
        assert_eq!(body, "# Title\n\nbody");
    }

    #[test]
    fn write_item_lays_out_markdown_and_images() {
        let root = tempfile::tempdir().unwrap();
        let meta = meta_of(&[("kind", json!("action_item"))]);
        let images = vec![("screenshot-1.png".to_string(), b"\x89PNG".to_vec())];

        let md = write_item(root.path(), "Fix the Thing", &meta, "Do it soon.", &images).unwrap();

        assert_eq!(md.file_name().unwrap(), "fix-the-thing.md");
        // Compared with line endings normalised; that the file lands with
        // platform endings is `atomic`'s contract, tested there and pinned
        // against the Python writer's own bytes in tests/golden_compat.rs.
        let text = fs::read_to_string(&md).unwrap().replace("\r\n", "\n");
        assert_eq!(
            text,
            "---\nkind: \"action_item\"\n---\n\n# Fix the Thing\n\nDo it soon.\n\n![screenshot-1.png](screenshot-1.png)\n"
        );
        assert!(md.parent().unwrap().join("screenshot-1.png").is_file());
    }

    #[test]
    fn write_item_omits_the_body_and_image_sections_when_empty() {
        let root = tempfile::tempdir().unwrap();
        let md = write_item(root.path(), "Bare", &Map::new(), "   ", &[]).unwrap();
        let text = fs::read_to_string(&md).unwrap().replace("\r\n", "\n");
        assert_eq!(text, "---\n---\n\n# Bare\n");
    }

    #[test]
    fn list_items_reads_back_what_write_item_wrote() {
        let root = tempfile::tempdir().unwrap();
        let meta = meta_of(&[("kind", json!("fact"))]);
        write_item(root.path(), "Beta", &meta, "second", &[]).unwrap();
        write_item(
            root.path(),
            "Alpha",
            &meta,
            "first",
            &[("screenshot-0.png".to_string(), b"x".to_vec())],
        )
        .unwrap();

        let items = list_items(root.path());
        assert_eq!(items.len(), 2);
        // Sorted by folder name, not by write order.
        assert_eq!(items[0].dir.file_name().unwrap(), "alpha");
        assert_eq!(items[0].meta, meta);
        assert_eq!(
            items[0].body,
            "# Alpha\n\nfirst\n\n![screenshot-0.png](screenshot-0.png)"
        );
        assert_eq!(items[0].screenshot_names, vec!["screenshot-0.png"]);
        assert_eq!(items[1].dir.file_name().unwrap(), "beta");
    }

    #[test]
    fn list_items_skips_directories_without_their_markdown() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("orphan")).unwrap();
        assert!(list_items(root.path()).is_empty());
    }

    #[test]
    fn list_items_on_a_missing_directory_is_empty_not_an_error() {
        assert!(list_items(Path::new("Z:\\definitely\\not\\here")).is_empty());
    }
}
