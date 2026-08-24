//! The per-recording export: one document out of what a meeting already has.
//!
//! Deterministic -- no model runs here. The order is the one the operator
//! asked for and is part of the contract: summary, then this recording's
//! action items, then its facts, then the full speaker-labelled transcript.
//!
//! Port of `exporting.py`, plus the export half of `jobs.py`. Two rules from
//! there are load-bearing:
//!
//! - **A missing piece is a warning, not a failure.** A meeting with no
//!   summary yet still exports; the section says so. The alternative -- refusing
//!   to export until every part exists -- makes the feature useless exactly
//!   when someone wants a quick handout.
//! - **The markdown is the deliverable and the PDF is the convenience.** A
//!   render that fails leaves the `.md` in place and adds a warning.

use std::path::Path;

use wire::artifacts::StoredItem;
use wire::transcript::TranscriptDoc;
use wire::ErrorKind;

use crate::jobs::{JobContext, JobFailure, JobOutcome};
use crate::llm::prompts;

/// Assemble the document and write `export.md` and `export.pdf`.
pub fn export_meeting(
    meeting_dir: &Path,
    output_dir: &Path,
    ctx: &JobContext,
) -> Result<JobOutcome, JobFailure> {
    ctx.check_cancelled()?;

    let meeting_name = name_of(meeting_dir);
    // Items live at the project level, one directory above the meeting.
    let project_dir = meeting_dir.parent().map(Path::to_path_buf);

    let (markdown, mut warnings) = build_markdown(BuildInputs {
        meeting_dir,
        meeting_name: &meeting_name,
        project_dir: project_dir.as_deref(),
        export_dir: output_dir,
    });

    let md_path = output_dir.join("export.md");
    wire::atomic::write_text(&md_path, &markdown).map_err(|err| {
        JobFailure::new(
            ErrorKind::Internal,
            format!("could not write {}: {err}", md_path.display()),
        )
    })?;
    ctx.set_progress(0.6);

    let pdf_path = output_dir.join("export.pdf");
    let pdf_written = match crate::pdf::render_to_file(&markdown, &pdf_path, output_dir) {
        Ok(()) => true,
        Err(err) => {
            // The document is already on disk; a failed render costs the
            // convenience copy, not the export.
            warnings.push(format!("the PDF could not be rendered: {err}"));
            false
        }
    };

    ctx.set_progress(1.0);
    Ok(JobOutcome {
        result_json: Some(
            serde_json::json!({
                "markdown": "export.md",
                "pdf": if pdf_written { Some("export.pdf") } else { None },
            })
            .to_string(),
        ),
        warnings,
        ..Default::default()
    })
}

struct BuildInputs<'a> {
    meeting_dir: &'a Path,
    meeting_name: &'a str,
    project_dir: Option<&'a Path>,
    export_dir: &'a Path,
}

/// Build the export document, returning it alongside what was missing.
fn build_markdown(inputs: BuildInputs<'_>) -> (String, Vec<String>) {
    let mut warnings = Vec::new();
    let mut out = String::new();

    out.push_str(&format!("# {}\n\n", inputs.meeting_name));

    out.push_str("## Summary\n\n");
    match load_summary(inputs.meeting_dir) {
        Some(summary) => {
            out.push_str(&summary);
            out.push_str("\n\n");
        }
        None => {
            warnings.push("no summary.md; the export's summary section is empty".to_string());
            out.push_str("_No summary has been generated for this recording yet._\n\n");
        }
    }

    for (heading, dir_name) in [
        ("Action items", vault::paths::ACTION_ITEMS_DIR_NAME),
        ("Facts", vault::paths::FACTS_DIR_NAME),
    ] {
        out.push_str(&format!("## {heading}\n\n"));
        let items = inputs
            .project_dir
            .map(|project| items_for_meeting(project, dir_name, inputs.meeting_name))
            .unwrap_or_default();

        if items.is_empty() {
            out.push_str(&format!(
                "_No {} recorded for this recording._\n\n",
                heading.to_lowercase()
            ));
        } else {
            for item in &items {
                out.push_str(&item_section(item, inputs.export_dir));
                out.push('\n');
            }
        }
    }

    out.push_str("## Transcript\n\n");
    match load_transcript(inputs.meeting_dir) {
        Some(doc) => {
            let lines = prompts::render_transcript_lines(
                &doc.segments,
                &speaker_overrides(inputs.meeting_dir),
            );
            if lines.is_empty() {
                out.push_str("_The transcript is empty._\n");
            } else {
                out.push_str(&lines.join("\n\n"));
                out.push('\n');
            }
        }
        None => {
            warnings.push("transcript.json is missing or unreadable".to_string());
            out.push_str("_The transcript could not be read._\n");
        }
    }

    (out, warnings)
}

/// Caps mirroring the desktop app's defensive reads: a vault is a directory
/// the app does not fully control, and a file that grew to gigabytes should
/// not be pulled into memory to be embedded in a handout.
const MAX_TRANSCRIPT_BYTES: u64 = 64 * 1024 * 1024;
const MAX_SIDECAR_BYTES: u64 = 1024 * 1024;
const MAX_SUMMARY_BYTES: u64 = 4 * 1024 * 1024;

fn read_capped(path: &Path, cap: u64) -> Option<String> {
    let size = std::fs::metadata(path).ok()?.len();
    if size > cap {
        return None;
    }
    std::fs::read_to_string(path).ok()
}

fn load_summary(meeting_dir: &Path) -> Option<String> {
    let text = read_capped(
        &meeting_dir.join(vault::paths::SUMMARY_FILE_NAME),
        MAX_SUMMARY_BYTES,
    )?;
    let trimmed = text.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_string())
}

fn load_transcript(meeting_dir: &Path) -> Option<TranscriptDoc> {
    let text = read_capped(
        &meeting_dir.join(wire::TRANSCRIPT_FILE_NAME),
        MAX_TRANSCRIPT_BYTES,
    )?;
    TranscriptDoc::from_json(&text).ok()
}

/// The manual speaker names a user assigned, keyed by segment id.
fn speaker_overrides(meeting_dir: &Path) -> std::collections::HashMap<String, String> {
    let Some(text) = read_capped(&meeting_dir.join("speakers.json"), MAX_SIDECAR_BYTES) else {
        return Default::default();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Default::default();
    };
    // The sidecar nests them under `assignments`; anything else is a file
    // written by something other than this app, and is ignored rather than
    // guessed at.
    value
        .get("assignments")
        .and_then(serde_json::Value::as_object)
        .map(|map| {
            map.iter()
                .filter_map(|(key, value)| {
                    value.as_str().map(|name| (key.clone(), name.to_string()))
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The project-level items whose front matter cites this meeting.
fn items_for_meeting(project_dir: &Path, kind_dir: &str, meeting_name: &str) -> Vec<StoredItem> {
    wire::artifacts::list_items(&project_dir.join(kind_dir))
        .into_iter()
        .filter(|item| {
            item.meta
                .get("source_meeting")
                .and_then(serde_json::Value::as_str)
                == Some(meeting_name)
        })
        .collect()
}

/// One item as a section of the export.
fn item_section(item: &StoredItem, export_dir: &Path) -> String {
    let title = item
        .meta
        .get("title")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| name_of(&item.dir));

    let kind = item
        .meta
        .get("type")
        .or_else(|| item.meta.get("kind"))
        .and_then(serde_json::Value::as_str);

    let heading = match kind {
        Some(kind) => format!("### {title} (`{kind}`)"),
        None => format!("### {title}"),
    };

    // The stored body opens with its own `# title`; drop it so the export
    // keeps one coherent outline under our own heading.
    let body = strip_leading_heading(&item.body);
    let body = relocate_screenshots(&body, &item.dir, export_dir);

    if body.trim().is_empty() {
        format!("{heading}\n")
    } else {
        format!("{heading}\n\n{}\n", body.trim())
    }
}

fn strip_leading_heading(body: &str) -> String {
    let mut lines = body.lines();
    match lines.next() {
        Some(first) if first.starts_with("# ") => lines
            .collect::<Vec<_>>()
            .join("\n")
            .trim_start()
            .to_string(),
        _ => body.to_string(),
    }
}

/// Rewrite an item's relative `screenshot-*.png` links so they resolve from
/// the export document's own directory.
///
/// Without this the export shows broken images: the links were written
/// relative to the item folder, and the export lives somewhere else entirely.
fn relocate_screenshots(body: &str, item_dir: &Path, export_dir: &Path) -> String {
    let mut out = String::with_capacity(body.len());
    let mut rest = body;

    while let Some(open) = rest.find("](") {
        let (before, after) = rest.split_at(open + 2);
        out.push_str(before);
        match after.find(')') {
            Some(close) => {
                let target = &after[..close];
                if is_screenshot(target) {
                    out.push_str(&relative_link(item_dir, target, export_dir));
                } else {
                    out.push_str(target);
                }
                rest = &after[close..];
            }
            None => {
                out.push_str(after);
                return out;
            }
        }
    }
    out.push_str(rest);
    out
}

fn is_screenshot(target: &str) -> bool {
    !target.contains('/')
        && !target.contains('\\')
        && target.starts_with("screenshot-")
        && target.to_lowercase().ends_with(".png")
}

/// A link from `export_dir` to `item_dir/name`, in forward slashes.
///
/// Computed by walking up from the export directory rather than with a path
/// library, because both sides are inside the same vault and the answer has to
/// be a *relative* link a markdown reader can follow.
fn relative_link(item_dir: &Path, name: &str, export_dir: &Path) -> String {
    let from: Vec<_> = export_dir.components().collect();
    let to: Vec<_> = item_dir.components().collect();
    let shared = from.iter().zip(&to).take_while(|(a, b)| a == b).count();

    let mut parts: Vec<String> =
        std::iter::repeat_n("..".to_string(), from.len() - shared).collect();
    parts.extend(
        to[shared..]
            .iter()
            .map(|c| c.as_os_str().to_string_lossy().into_owned()),
    );
    parts.push(name.to_string());
    parts.join("/")
}

fn name_of(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::CancelToken;

    fn transcript_with(segments: Vec<wire::transcript::Segment>) -> TranscriptDoc {
        TranscriptDoc {
            schema_version: wire::SCHEMA_VERSION,
            created_at: "2026-08-23T18:00:00+00:00".to_string(),
            source: wire::transcript::Source {
                path: "source.mp4".to_string(),
                filename: "source.mp4".to_string(),
                duration_sec: 60.0,
            },
            provider: wire::transcript::ProviderInfo {
                name: "local".to_string(),
                model: "large-v3".to_string(),
                device: "cpu".to_string(),
                compute_type: "ggml".to_string(),
            },
            language: Some("ru".to_string()),
            language_probability: None,
            text: String::new(),
            segments,
            stats: wire::transcript::Stats {
                elapsed_sec: 1.0,
                realtime_factor: 1.0,
                cost_usd: None,
                currency: None,
            },
            diarization: None,
        }
    }

    fn segment(
        id: i64,
        start: f64,
        text: &str,
        speaker: Option<&str>,
    ) -> wire::transcript::Segment {
        wire::transcript::Segment {
            id,
            start,
            end: start + 2.0,
            text: text.to_string(),
            avg_logprob: Some(-0.2),
            no_speech_prob: Some(0.01),
            compression_ratio: Some(1.4),
            words: None,
            speaker: speaker.map(str::to_string),
        }
    }

    /// A project with one meeting, and whatever the test asks to put in it.
    struct Vault {
        _root: tempfile::TempDir,
        project: std::path::PathBuf,
        meeting: std::path::PathBuf,
        export: std::path::PathBuf,
    }

    fn vault() -> Vault {
        let root = tempfile::tempdir().unwrap();
        let project = root.path().join("GIS");
        let meeting = project.join("260812 - Demo");
        let export = meeting.join("exports").join("260823");
        std::fs::create_dir_all(&export).unwrap();
        Vault {
            _root: root,
            project,
            meeting,
            export,
        }
    }

    fn build(v: &Vault) -> (String, Vec<String>) {
        build_markdown(BuildInputs {
            meeting_dir: &v.meeting,
            meeting_name: &name_of(&v.meeting),
            project_dir: Some(&v.project),
            export_dir: &v.export,
        })
    }

    #[test]
    fn the_sections_appear_in_the_agreed_order() {
        let v = vault();
        let (md, _) = build(&v);
        let order = ["## Summary", "## Action items", "## Facts", "## Transcript"];
        let mut cursor = 0;
        for heading in order {
            let at = md[cursor..]
                .find(heading)
                .unwrap_or_else(|| panic!("{heading} missing or out of order in:\n{md}"));
            cursor += at + heading.len();
        }
    }

    #[test]
    fn an_empty_meeting_still_exports_and_says_what_is_missing() {
        // Refusing to export until every part exists would make the feature
        // useless exactly when someone wants a quick handout.
        let v = vault();
        let (md, warnings) = build(&v);

        assert!(md.contains("_No summary has been generated"), "{md}");
        assert!(md.contains("_The transcript could not be read._"), "{md}");
        assert_eq!(warnings.len(), 2, "{warnings:?}");
    }

    #[test]
    fn the_summary_is_included_verbatim() {
        let v = vault();
        std::fs::write(
            v.meeting.join(vault::paths::SUMMARY_FILE_NAME),
            "Обсудили сроки.\n",
        )
        .unwrap();

        let (md, warnings) = build(&v);
        assert!(md.contains("Обсудили сроки."), "{md}");
        // The transcript is absent in this fixture and is warned about; what
        // matters here is that the summary is not.
        assert!(
            !warnings.iter().any(|w| w.contains("summary")),
            "{warnings:?}"
        );
    }

    #[test]
    fn a_transcript_is_rendered_with_its_speakers() {
        let v = vault();
        let doc = transcript_with(vec![
            segment(0, 0.0, "Привет.", Some("Speaker 1")),
            segment(1, 5.0, "Здравствуйте.", Some("Speaker 2")),
        ]);
        std::fs::write(
            v.meeting.join(wire::TRANSCRIPT_FILE_NAME),
            doc.to_json().unwrap(),
        )
        .unwrap();

        let (md, warnings) = build(&v);
        assert!(md.contains("Speaker 1"), "{md}");
        assert!(md.contains("Здравствуйте."), "{md}");
        assert!(
            !warnings.iter().any(|w| w.contains("transcript")),
            "{warnings:?}"
        );
    }

    #[test]
    fn manual_speaker_names_win_over_the_diarizers_labels() {
        // The whole point of the sidecar: a reader wants names, not
        // "Speaker 2".
        let v = vault();
        let doc = transcript_with(vec![segment(0, 0.0, "Привет.", Some("Speaker 1"))]);
        std::fs::write(
            v.meeting.join(wire::TRANSCRIPT_FILE_NAME),
            doc.to_json().unwrap(),
        )
        .unwrap();
        std::fs::write(
            v.meeting.join("speakers.json"),
            r#"{"assignments": {"0": "Кирилл"}}"#,
        )
        .unwrap();

        let (md, _) = build(&v);
        assert!(md.contains("Кирилл"), "{md}");
    }

    #[test]
    fn a_malformed_speakers_file_is_ignored_rather_than_failing_the_export() {
        let v = vault();
        std::fs::write(v.meeting.join("speakers.json"), "{not json").unwrap();
        assert!(speaker_overrides(&v.meeting).is_empty());
    }

    #[test]
    fn only_this_meetings_items_are_included() {
        // Items live at the project level and cite the meeting they came
        // from; an export that ignored that would pull in the whole project.
        let v = vault();
        let mut mine = serde_json::Map::new();
        mine.insert("title".into(), serde_json::json!("Fix the thing"));
        mine.insert("type".into(), serde_json::json!("task"));
        mine.insert("source_meeting".into(), serde_json::json!("260812 - Demo"));

        let mut theirs = serde_json::Map::new();
        theirs.insert("title".into(), serde_json::json!("Other meeting item"));
        theirs.insert("source_meeting".into(), serde_json::json!("260901 - Other"));

        let parent = v.project.join(vault::paths::ACTION_ITEMS_DIR_NAME);
        wire::artifacts::write_item(&parent, "Fix the thing", &mine, "Do it.", &[]).unwrap();
        wire::artifacts::write_item(&parent, "Other meeting item", &theirs, "Nope.", &[]).unwrap();

        let (md, _) = build(&v);
        assert!(md.contains("Fix the thing"), "{md}");
        assert!(!md.contains("Other meeting item"), "{md}");
        assert!(md.contains("(`task`)"), "{md}");
    }

    #[test]
    fn an_items_own_heading_is_dropped_so_the_outline_stays_coherent() {
        assert_eq!(strip_leading_heading("# Title\n\nbody text"), "body text");
        assert_eq!(strip_leading_heading("body only"), "body only");
    }

    #[test]
    fn screenshot_links_are_rewritten_to_reach_the_item_folder() {
        // Without this the export shows broken images: the links were written
        // relative to the item folder, which is not where the export lives.
        let v = vault();
        let item_dir = v
            .project
            .join(vault::paths::ACTION_ITEMS_DIR_NAME)
            .join("fix-the-thing");

        let body = "See ![shot](screenshot-0100.png) here.";
        let out = relocate_screenshots(body, &item_dir, &v.export);

        assert!(
            out.contains("../../../action items/fix-the-thing/screenshot-0100.png"),
            "{out}"
        );
    }

    #[test]
    fn links_that_are_not_screenshots_are_left_alone() {
        let v = vault();
        let item_dir = v.project.join("action items").join("x");
        let body = "[docs](https://example.com/a) and ![other](sub/pic.png)";
        let out = relocate_screenshots(body, &item_dir, &v.export);
        assert!(out.contains("(https://example.com/a)"), "{out}");
        assert!(out.contains("(sub/pic.png)"), "{out}");
    }

    #[test]
    fn a_link_without_a_closing_paren_does_not_lose_the_rest_of_the_body() {
        let v = vault();
        let item_dir = v.project.join("action items").join("x");
        let out = relocate_screenshots("broken ](oops", &item_dir, &v.export);
        assert!(out.contains("oops"), "{out}");
    }

    #[test]
    fn the_job_writes_both_files_and_reports_them() {
        let v = vault();
        std::fs::write(v.meeting.join(vault::paths::SUMMARY_FILE_NAME), "Итоги.\n").unwrap();
        let doc = transcript_with(vec![segment(0, 0.0, "Привет.", None)]);
        std::fs::write(
            v.meeting.join(wire::TRANSCRIPT_FILE_NAME),
            doc.to_json().unwrap(),
        )
        .unwrap();

        let ctx = JobContext::detached(CancelToken::default());
        let outcome = export_meeting(&v.meeting, &v.export, &ctx).expect("export");

        assert!(v.export.join("export.md").is_file());
        assert!(v.export.join("export.pdf").is_file());

        let manifest: serde_json::Value =
            serde_json::from_str(outcome.result_json.as_deref().unwrap()).unwrap();
        assert_eq!(manifest["markdown"], "export.md");
        assert_eq!(manifest["pdf"], "export.pdf");
        assert!(outcome.warnings.is_empty(), "{:?}", outcome.warnings);
    }

    #[test]
    fn a_cancelled_export_stops_before_writing_anything() {
        let v = vault();
        let cancel = CancelToken::default();
        cancel.cancel();
        let ctx = JobContext::detached(cancel);

        let err = export_meeting(&v.meeting, &v.export, &ctx).expect_err("cancelled");
        assert_eq!(err.kind, ErrorKind::Cancelled);
        assert!(!v.export.join("export.md").exists());
    }
}
