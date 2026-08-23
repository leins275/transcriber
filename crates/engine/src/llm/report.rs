//! The project-essence report: gathering the materials, then writing over them.
//!
//! Port of `services/transcription/src/transcription/llm/report.py`.
//!
//! A report is not made from transcripts. It is made from what a project
//! already knows about itself -- each meeting's summary, every extracted action
//! item, every recorded fact -- assembled into one materials document and
//! handed to the model. Raw transcript text stands in only where a meeting has
//! no summary yet, and only as an excerpt: the point of the report is the
//! distillation the project has already paid for, not a second pass over
//! everything ever said.
//!
//! Collection never fails. A project directory is a place the operator edits
//! by hand, so an unreadable meeting folder is skipped rather than raised: a
//! report over nine of ten meetings is worth more than no report.

use std::fs;
use std::path::Path;

use vault::paths::{ACTION_ITEMS_DIR_NAME, FACTS_DIR_NAME, REPORTS_DIR_NAME};
use wire::artifacts::list_items;

use crate::jobs::JobContext;
use crate::llm::chunking::{chunk_lines, estimate_tokens};
use crate::llm::prompts;
use crate::llm::summarize::{complete_text, Summary};
use crate::llm::{CompleteOptions, LlmEngineApi, LlmError};

/// How much raw transcript text stands in for a missing summary. A whole
/// transcript would drown the summaries it sits beside; the opening is enough
/// for the model to place the meeting.
const TRANSCRIPT_EXCERPT_CHARS: usize = 1500;

/// Defensive caps mirroring the desktop app's reads of the same files.
const MAX_TRANSCRIPT_BYTES: u64 = 64 * 1024 * 1024;
const MAX_SUMMARY_BYTES: u64 = 4 * 1024 * 1024;

/// One text document holding every material the report draws on.
///
/// Returns an empty string for a project with nothing in it; the caller turns
/// that into an "unsupported input" failure, which this layer cannot attribute.
pub fn collect_project_materials(project_dir: &Path) -> String {
    let mut sections: Vec<String> = Vec::new();

    for meeting_dir in meeting_dirs(project_dir) {
        let name = file_name(&meeting_dir);
        if let Some(summary) = load_summary(&meeting_dir) {
            sections.push(format!("## Meeting: {name}\n\n{summary}"));
            continue;
        }
        if let Some(text) = load_transcript_text(&meeting_dir) {
            let excerpt: String = text.chars().take(TRANSCRIPT_EXCERPT_CHARS).collect();
            let suffix = if text.chars().count() > TRANSCRIPT_EXCERPT_CHARS {
                "…"
            } else {
                ""
            };
            sections.push(format!(
                "## Meeting: {name} (no summary; transcript excerpt)\n\n{excerpt}{suffix}"
            ));
        }
    }

    // The front-matter key that carries the item's classification differs
    // between the two kinds; everything else about them is the same.
    for (heading, dir_name, type_key) in [
        ("Action items", ACTION_ITEMS_DIR_NAME, "type"),
        ("Facts and answered questions", FACTS_DIR_NAME, "kind"),
    ] {
        let items = list_items(&project_dir.join(dir_name));
        if items.is_empty() {
            continue;
        }
        let mut lines = vec![format!("## {heading}")];
        for item in items {
            let fallback = file_name(&item.dir);
            let title = meta_text(&item, "title").unwrap_or(fallback);
            let mut descriptor = format!("- **{title}**");
            if let Some(label) = meta_text(&item, type_key) {
                descriptor.push_str(&format!(" ({label})"));
            }
            if let Some(source) = meta_text(&item, "source_meeting") {
                descriptor.push_str(&format!(" — from {source}"));
            }
            lines.push(descriptor);

            let body = item.body.trim();
            if !body.is_empty() {
                // The item's own `# title` heading is already in the
                // descriptor above; the rest is indented into a detail block so
                // it cannot be mistaken for a section of the report.
                lines.extend(
                    body.lines()
                        .filter(|line| !line.starts_with("# "))
                        .filter(|line| !line.trim().is_empty())
                        .map(|line| format!("  {line}")),
                );
            }
        }
        sections.push(lines.join("\n"));
    }

    sections.join("\n\n")
}

/// The status report, map-reducing the materials when they overflow the budget.
///
/// The condensing pass reuses the transcript chunk-summary prompt rather than
/// a report-shaped one: at that point the materials are just long text, and a
/// second prompt to keep in step with the first would earn nothing.
pub fn report_from_materials(
    engine: &mut dyn LlmEngineApi,
    materials: &str,
    project: &str,
    budget_tokens: usize,
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<Summary, LlmError> {
    if materials.trim().is_empty() {
        // As in `summarize_chunks`: the caller rejects an empty project with a
        // proper attribution, and this guard is what stops a bug upstream from
        // producing a report about nothing.
        return Err(LlmError::Generation(
            "the project has no materials to report on".to_string(),
        ));
    }

    let mut report = Summary::default();
    let mut calls_done = 0usize;

    let source = if estimate_tokens(materials) <= budget_tokens {
        materials.to_string()
    } else {
        let lines: Vec<&str> = materials.lines().collect();
        let chunks = chunk_lines(&lines, budget_tokens)
            .map_err(|error| LlmError::Generation(error.to_string()))?;
        let mut partials = Vec::with_capacity(chunks.len());
        for (index, chunk) in chunks.iter().enumerate() {
            partials.push(complete_text(
                engine,
                &prompts::chunk_summary_messages(chunk, index, chunks.len()),
                options,
                job,
                &mut report.reasoning,
            )?);
            calls_done += 1;
            report_progress(job, calls_done);
        }
        partials.join("\n\n")
    };

    report.markdown = complete_text(
        engine,
        &prompts::report_messages(&source, project),
        options,
        job,
        &mut report.reasoning,
    )?;
    report_progress(job, calls_done + 1);
    Ok(report)
}

/// The meeting folders of a project, in a stable case-insensitive order.
///
/// The three reserved project-level directories are not meetings; the vault
/// crate owns those names and both sides pin them with tests.
fn meeting_dirs(project_dir: &Path) -> Vec<std::path::PathBuf> {
    let reserved: [String; 3] = [
        ACTION_ITEMS_DIR_NAME.to_lowercase(),
        FACTS_DIR_NAME.to_lowercase(),
        REPORTS_DIR_NAME.to_lowercase(),
    ];
    let Ok(entries) = fs::read_dir(project_dir) else {
        return Vec::new();
    };
    let mut dirs: Vec<std::path::PathBuf> = entries
        .flatten()
        .filter(|entry| entry.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .map(|entry| entry.path())
        .filter(|path| !reserved.contains(&file_name(path).to_lowercase()))
        .collect();
    dirs.sort_by_key(|path| file_name(path).to_lowercase());
    dirs
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// A front-matter value as the report should print it, or `None` when the key
/// is absent, null or empty.
///
/// Front matter is `key: <json value>`, so a string arrives quoted; anything
/// else (a number, a list of timestamps) prints as its JSON, which is what the
/// Python service's `str()` produced for the same values.
fn meta_text(item: &wire::artifacts::StoredItem, key: &str) -> Option<String> {
    let value = item.meta.get(key)?;
    let text = match value {
        serde_json::Value::Null => return None,
        serde_json::Value::String(text) => text.trim().to_string(),
        other => other.to_string(),
    };
    (!text.is_empty()).then_some(text)
}

fn load_summary(meeting_dir: &Path) -> Option<String> {
    let path = meeting_dir.join(vault::paths::SUMMARY_FILE_NAME);
    let text = read_capped(&path, MAX_SUMMARY_BYTES)?;
    let trimmed = text.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_string())
}

fn load_transcript_text(meeting_dir: &Path) -> Option<String> {
    let path = meeting_dir.join(wire::transcript::TRANSCRIPT_FILE_NAME);
    let text = read_capped(&path, MAX_TRANSCRIPT_BYTES)?;
    // Parsed as a bare value rather than a `TranscriptDoc`: a document written
    // by an older or newer writer still has the one field wanted here, and
    // refusing it over an unrelated field would lose a whole meeting.
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let body = value.get("text")?.as_str()?.trim();
    (!body.is_empty()).then(|| body.to_string())
}

/// The report's calls own a fifth of the bar each, starting after collection
/// and stopping short of the artifact writes the caller still has to do.
fn report_progress(job: &JobContext, calls_done: usize) {
    job.set_progress((0.05 + calls_done as f64 * 0.2).min(0.85));
}

/// Read a file, or `None` for anything -- missing, unreadable, absurdly large,
/// not UTF-8 -- that means there is nothing usable here.
fn read_capped(path: &Path, max_bytes: u64) -> Option<String> {
    let metadata = fs::metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > max_bytes {
        return None;
    }
    fs::read_to_string(path).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::CancelToken;
    use crate::llm::{Completion, LlmInfo, Message};
    use std::path::PathBuf;
    use tempfile::TempDir;

    /// An engine that answers from a script and records what it was asked.
    struct ScriptedEngine {
        answers: Vec<Result<Completion, LlmError>>,
        seen: Vec<Vec<Message>>,
    }

    impl ScriptedEngine {
        fn new(texts: &[&str]) -> Self {
            ScriptedEngine {
                answers: texts
                    .iter()
                    .map(|text| {
                        Ok(Completion {
                            text: text.to_string(),
                            reasoning: None,
                            prompt_tokens: None,
                            completion_tokens: None,
                        })
                    })
                    .collect(),
                seen: Vec::new(),
            }
        }
    }

    impl LlmEngineApi for ScriptedEngine {
        fn complete(
            &mut self,
            messages: &[Message],
            _options: &CompleteOptions,
            _job: &JobContext,
        ) -> Result<Completion, LlmError> {
            self.seen.push(messages.to_vec());
            if self.answers.is_empty() {
                return Err(LlmError::Generation("the script ran out".to_string()));
            }
            self.answers.remove(0)
        }

        fn describe(&self) -> LlmInfo {
            LlmInfo {
                model: "scripted".to_string(),
                device: "none".to_string(),
                gpu_layers: 0,
            }
        }
    }

    fn context() -> JobContext {
        JobContext::detached(CancelToken::default())
    }

    fn write(path: &Path, content: &str) {
        fs::create_dir_all(path.parent().expect("a parent")).expect("mkdir");
        fs::write(path, content).expect("write");
    }

    fn project(dir: &TempDir) -> PathBuf {
        dir.path().join("ELS")
    }

    #[test]
    fn a_meetings_summary_is_preferred_over_its_transcript() {
        let dir = TempDir::new().expect("tempdir");
        let meeting = project(&dir).join("260812 - Demo");
        write(&meeting.join("summary.md"), "# Demo\n\nIt went well.\n");
        write(
            &meeting.join("transcript.json"),
            r#"{"text": "raw words nobody needs here"}"#,
        );

        let materials = collect_project_materials(&project(&dir));

        assert!(materials.contains("## Meeting: 260812 - Demo"));
        assert!(materials.contains("It went well."));
        assert!(!materials.contains("raw words"));
    }

    #[test]
    fn a_meeting_without_a_summary_contributes_a_transcript_excerpt() {
        let dir = TempDir::new().expect("tempdir");
        let meeting = project(&dir).join("260812 - Demo");
        let long = "word ".repeat(1000);
        write(
            &meeting.join("transcript.json"),
            &serde_json::json!({ "text": long }).to_string(),
        );

        let materials = collect_project_materials(&project(&dir));

        assert!(materials.contains("(no summary; transcript excerpt)"));
        assert!(materials.ends_with('…'), "the excerpt is marked as cut");
        assert!(materials.chars().count() < 1800);
    }

    #[test]
    fn the_reserved_project_directories_are_never_read_as_meetings() {
        let dir = TempDir::new().expect("tempdir");
        for reserved in ["action items", "facts", "reports"] {
            write(
                &project(&dir).join(reserved).join("summary.md"),
                "not a meeting",
            );
        }
        write(
            &project(&dir).join("260812 - Demo").join("summary.md"),
            "a real meeting",
        );

        let materials = collect_project_materials(&project(&dir));

        assert!(materials.contains("a real meeting"));
        assert!(!materials.contains("not a meeting"));
    }

    #[test]
    fn items_are_listed_with_their_type_and_source_meeting() {
        let dir = TempDir::new().expect("tempdir");
        let item = project(&dir).join("action items").join("fix-login");
        write(
            &item.join("fix-login.md"),
            "---\ntype: \"task\"\ntitle: \"Fix login\"\nsource_meeting: \"260812 - Demo\"\n---\n\n\
# Fix login\n\nThe session cookie expires early.\n",
        );

        let materials = collect_project_materials(&project(&dir));

        assert!(materials.contains("## Action items"));
        assert!(materials.contains("- **Fix login** (task) — from 260812 - Demo"));
        assert!(materials.contains("  The session cookie expires early."));
        assert!(
            !materials.contains("# Fix login\n"),
            "the item's own heading is dropped: {materials}"
        );
    }

    #[test]
    fn a_project_with_nothing_in_it_collects_no_materials() {
        let dir = TempDir::new().expect("tempdir");
        assert_eq!(collect_project_materials(&project(&dir)), "");
        fs::create_dir_all(project(&dir)).expect("mkdir");
        assert_eq!(collect_project_materials(&project(&dir)), "");
    }

    #[test]
    fn materials_that_fit_the_budget_are_reported_in_one_call() {
        let mut engine = ScriptedEngine::new(&["  # Report  "]);
        let job = context();

        let report = report_from_materials(
            &mut engine,
            "## Meeting: Demo\n\nIt went well.",
            "ELS",
            10_000,
            &CompleteOptions::default(),
            &job,
        )
        .expect("reports");

        assert_eq!(report.markdown, "# Report");
        assert_eq!(engine.seen.len(), 1);
        assert!(engine.seen[0][1].content.contains("project ELS"));
    }

    #[test]
    fn materials_over_the_budget_are_condensed_before_the_report() {
        // Twelve ~19-token lines against a 100-token budget: three chunks,
        // then the report call over what they condensed to.
        let materials: String = (0..12)
            .map(|i| format!("line {i} {}\n", "x".repeat(50)))
            .collect();
        let mut engine = ScriptedEngine::new(&["part a", "part b", "part c", "# Report"]);
        let job = context();

        let report = report_from_materials(
            &mut engine,
            &materials,
            "ELS",
            // ~100 tokens is ~300 characters, so about five of these lines.
            100,
            &CompleteOptions::default(),
            &job,
        )
        .expect("reports");

        assert_eq!(report.markdown, "# Report");
        assert_eq!(
            engine.seen.len(),
            4,
            "three condensing calls and the report"
        );
        // The final call must see the condensed partials, not the raw lines.
        let last = &engine.seen[3][1].content;
        assert!(last.contains("part a"), "{last}");
        assert!(last.contains("part c"), "{last}");
        assert!(!last.contains("line 11"), "{last}");
    }

    #[test]
    fn a_project_with_no_materials_is_refused_rather_than_reported_on() {
        let mut engine = ScriptedEngine::new(&[]);
        let job = context();

        let error = report_from_materials(
            &mut engine,
            "   \n  ",
            "ELS",
            10_000,
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("refuses");

        assert!(matches!(error, LlmError::Generation(_)), "{error}");
        assert!(engine.seen.is_empty());
    }

    #[test]
    fn thinking_from_every_call_reaches_the_sidecar_and_not_the_report() {
        let mut engine = ScriptedEngine {
            answers: vec![Ok(Completion {
                text: "# Report".to_string(),
                reasoning: Some("weighing the materials".to_string()),
                prompt_tokens: None,
                completion_tokens: None,
            })],
            seen: Vec::new(),
        };
        let job = context();

        let report = report_from_materials(
            &mut engine,
            "materials",
            "ELS",
            10_000,
            &CompleteOptions::default(),
            &job,
        )
        .expect("reports");

        assert_eq!(report.markdown, "# Report");
        assert_eq!(
            report.reasoning_document().expect("a sidecar"),
            "weighing the materials\n"
        );
    }

    #[test]
    fn cancelling_stops_the_report_before_its_next_call() {
        let token = CancelToken::default();
        let mut engine = ScriptedEngine::new(&["# Report"]);
        let job = JobContext::detached(token.clone());
        token.cancel();

        let error = report_from_materials(
            &mut engine,
            "materials",
            "ELS",
            10_000,
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("stops");

        assert!(matches!(error, LlmError::Cancelled), "{error}");
        assert!(engine.seen.is_empty());
    }
}
