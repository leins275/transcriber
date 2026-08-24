//! The derived-knowledge jobs: summaries, action items, facts, reports.
//!
//! Everything here reads material that already exists in the vault -- a
//! transcript, or a project's artifacts -- and writes more of it. The model
//! calls themselves live in [`crate::llm`]; what this owns is the part that
//! touches the vault: what to read, how to cut it to fit a context window,
//! where the results land, and which failures are allowed to degrade instead
//! of ending the job.
//!
//! Port of the LLM half of `jobs.py`.

use std::path::{Path, PathBuf};

use wire::transcript::TranscriptDoc;
use wire::ErrorKind;

use crate::jobs::{JobContext, JobFailure, JobOutcome};
use crate::llm::extraction::{self, LLM_PROGRESS_SHARE};
use crate::llm::shapes::{ActionItemOut, FactOut};
use crate::llm::{prompts, report, summarize, CompleteOptions, LlmEngineApi};
use crate::media::{frames, MediaDecoder};

/// The smallest context worth chunking against. A tiny configured window would
/// otherwise produce chunks too small to summarise coherently.
const MIN_CHUNK_BUDGET: usize = 1024;

/// Everything a derived job needs from the vault and the engines.
pub struct JobInputs<'a> {
    pub llm: &'a mut dyn LlmEngineApi,
    pub decoder: &'a dyn MediaDecoder,
    pub options: CompleteOptions,
    pub ctx_tokens: u32,
}

/// The chunk budget: half the context window, since the prompt and the answer
/// share it.
pub fn chunk_budget(ctx_tokens: u32) -> usize {
    MIN_CHUNK_BUDGET.max(ctx_tokens as usize / 2)
}

/// Read a meeting's transcript, refusing an empty one by name.
///
/// "There is nothing to summarise" is a fact about the input, not a failure of
/// the assistant, and the two are attributed differently so the UI can say
/// which happened.
pub fn load_transcript(meeting_dir: &Path) -> Result<TranscriptDoc, JobFailure> {
    let path = meeting_dir.join(wire::TRANSCRIPT_FILE_NAME);
    let text = std::fs::read_to_string(&path).map_err(|err| {
        JobFailure::new(
            ErrorKind::UnsupportedInput,
            format!("no transcript to work from at {}: {err}", path.display()),
        )
    })?;
    let doc = TranscriptDoc::from_json(&text).map_err(|err| {
        JobFailure::new(
            ErrorKind::UnsupportedInput,
            format!("{} could not be read: {err}", path.display()),
        )
    })?;
    if doc.segments.is_empty() {
        return Err(JobFailure::new(
            ErrorKind::UnsupportedInput,
            "the transcript has no segments".to_string(),
        ));
    }
    Ok(doc)
}

/// Speaker display names the user assigned, if any.
///
/// Best-effort: a missing or malformed `speakers.json` means the diarizer's
/// own labels are used, which is a worse transcript to read but not a failure.
fn speaker_names(meeting_dir: &Path) -> std::collections::HashMap<String, String> {
    std::fs::read_to_string(meeting_dir.join("speakers.json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// The transcript as the model reads it: one `[m:ss] Speaker: text` line per
/// segment, cut into context-sized chunks.
fn transcript_chunks(doc: &TranscriptDoc, meeting_dir: &Path, budget: usize) -> Vec<String> {
    let lines = prompts::render_transcript_lines(&doc.segments, &speaker_names(meeting_dir));
    crate::llm::chunking::chunk_lines(&lines, budget).unwrap_or_default()
}

/// Write `<meeting>/summary.md`, plus the reasoning sidecar when the model
/// produced any.
pub fn summarize_meeting(
    inputs: &mut JobInputs<'_>,
    meeting_dir: &Path,
    output_dir: &Path,
    ctx: &JobContext,
) -> Result<JobOutcome, JobFailure> {
    let doc = load_transcript(meeting_dir)?;
    let chunks = transcript_chunks(&doc, meeting_dir, chunk_budget(inputs.ctx_tokens));

    let summary = summarize::summarize_chunks(inputs.llm, &chunks, &inputs.options, ctx)?;

    wire::atomic::write_text(
        &output_dir.join(vault::paths::SUMMARY_FILE_NAME),
        &format!("{}\n", summary.markdown.trim_end()),
    )
    .map_err(write_failure)?;

    // The thinking goes beside the summary, never inside it: a reader wants
    // the answer, and the reasoning is for when the answer looks wrong.
    if let Some(reasoning) = summary.reasoning_document() {
        wire::atomic::write_text(&output_dir.join("summary.reasoning.md"), &reasoning)
            .map_err(write_failure)?;
    }

    ctx.set_progress(1.0);
    Ok(JobOutcome {
        result_json: Some(
            serde_json::json!({"summary": vault::paths::SUMMARY_FILE_NAME}).to_string(),
        ),
        ..Default::default()
    })
}

/// Which kind of item an extraction job is producing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExtractionKind {
    ActionItems,
    Facts,
}

impl ExtractionKind {
    fn dir_name(self) -> &'static str {
        match self {
            ExtractionKind::ActionItems => vault::paths::ACTION_ITEMS_DIR_NAME,
            ExtractionKind::Facts => vault::paths::FACTS_DIR_NAME,
        }
    }
}

/// Extract items from a meeting and write one folder per item, with the
/// screenshots they cite.
pub fn extract_items(
    inputs: &mut JobInputs<'_>,
    kind: ExtractionKind,
    meeting_dir: &Path,
    output_dir: &Path,
    ctx: &JobContext,
) -> Result<JobOutcome, JobFailure> {
    let doc = load_transcript(meeting_dir)?;
    let chunks = transcript_chunks(&doc, meeting_dir, chunk_budget(inputs.ctx_tokens));
    let starts = extraction::segment_starts(&doc.segments);
    let duration = Some(doc.source.duration_sec);

    let mut warnings = Vec::new();
    let written = match kind {
        ExtractionKind::ActionItems => {
            let mut items =
                extraction::extract_action_items(inputs.llm, &chunks, &inputs.options, ctx)?;
            extraction::snap_item_timestamps(&mut items, &starts, duration);
            write_items(
                inputs,
                kind,
                &items,
                meeting_dir,
                output_dir,
                doc.source.duration_sec,
                ctx,
                &mut warnings,
            )?
        }
        ExtractionKind::Facts => {
            let mut items = extraction::extract_facts(inputs.llm, &chunks, &inputs.options, ctx)?;
            extraction::snap_item_timestamps(&mut items, &starts, duration);
            write_items(
                inputs,
                kind,
                &items,
                meeting_dir,
                output_dir,
                doc.source.duration_sec,
                ctx,
                &mut warnings,
            )?
        }
    };

    ctx.set_progress(1.0);
    Ok(JobOutcome {
        result_json: Some(
            serde_json::json!({"kind": kind.dir_name(), "items": written}).to_string(),
        ),
        warnings,
        ..Default::default()
    })
}

/// The facts about the job that every item's front matter records, gathered
/// once rather than per item.
///
/// These keys are not decoration: `source_meeting` is what the export job
/// filters a project's items by, so an item written without it is invisible to
/// the export that should have included it.
struct ItemContext {
    project: String,
    meeting: String,
    recording: Option<String>,
    created: String,
    model: String,
    job_id: String,
}

/// One extracted item, whichever kind, reduced to what writing it needs.
trait Writable {
    fn title(&self) -> &str;
    fn body(&self) -> &str;
    fn stamps(&self) -> &[f64];
    /// The type discriminator, under the key that kind of item uses.
    fn type_entry(&self) -> (&'static str, serde_json::Value);
}

impl Writable for ActionItemOut {
    fn title(&self) -> &str {
        &self.title
    }
    fn body(&self) -> &str {
        &self.description_md
    }
    fn stamps(&self) -> &[f64] {
        &self.timestamps
    }
    fn type_entry(&self) -> (&'static str, serde_json::Value) {
        (
            "type",
            serde_json::to_value(self.item_type).unwrap_or(serde_json::Value::Null),
        )
    }
}

impl Writable for FactOut {
    fn title(&self) -> &str {
        &self.title
    }
    fn body(&self) -> &str {
        &self.description_md
    }
    fn stamps(&self) -> &[f64] {
        &self.timestamps
    }
    fn type_entry(&self) -> (&'static str, serde_json::Value) {
        (
            "kind",
            serde_json::to_value(self.kind).unwrap_or(serde_json::Value::Null),
        )
    }
}

/// Build one item's front matter, in the key order the Python service wrote.
///
/// Order matters because the block is rendered line by line: a reordered map
/// produces a different file for the same item, and every artifact already in
/// a vault was written this way.
fn front_matter<T: Writable>(
    item: &T,
    context: &ItemContext,
    screenshots: &str,
) -> serde_json::Map<String, serde_json::Value> {
    let mut meta = serde_json::Map::new();
    let (type_key, type_value) = item.type_entry();
    meta.insert(type_key.to_string(), type_value);
    meta.insert("title".into(), serde_json::json!(item.title()));
    meta.insert("source_project".into(), serde_json::json!(context.project));
    meta.insert("source_meeting".into(), serde_json::json!(context.meeting));
    meta.insert(
        "source_recording".into(),
        match &context.recording {
            Some(name) => serde_json::json!(name),
            None => serde_json::Value::Null,
        },
    );
    meta.insert("timestamps".into(), serde_json::json!(item.stamps()));
    meta.insert("created".into(), serde_json::json!(context.created));
    meta.insert("model".into(), serde_json::json!(context.model));
    meta.insert("job_id".into(), serde_json::json!(context.job_id));
    meta.insert("screenshots".into(), serde_json::json!(screenshots));
    meta
}

#[allow(clippy::too_many_arguments)]
fn write_items<T: Writable>(
    inputs: &mut JobInputs<'_>,
    kind: ExtractionKind,
    items: &[T],
    meeting_dir: &Path,
    output_dir: &Path,
    duration_sec: f64,
    ctx: &JobContext,
    warnings: &mut Vec<String>,
) -> Result<usize, JobFailure> {
    if items.is_empty() {
        // A meeting with no action items is a real answer.
        return Ok(0);
    }

    let parent = output_dir.join(kind.dir_name());
    let source = source_file(meeting_dir);
    let context = ItemContext {
        // The items live under the project folder, which is the parent of
        // where this job writes.
        project: name_of(output_dir),
        meeting: name_of(meeting_dir),
        recording: source.as_ref().map(|path| name_of(path)),
        created: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        model: inputs.llm.describe().model,
        job_id: ctx.job_id().to_string(),
    };

    // Screenshots share the last fifth of the progress bar; the model owns the
    // rest, because it is where the time actually goes.
    let mut screenshots_broken = false;
    for (index, item) in items.iter().enumerate() {
        ctx.check_cancelled()?;

        let planned = frames::plan(item.stamps(), Some(duration_sec));
        let mut images = Vec::new();
        let mut screenshots = if planned.is_empty() {
            "none"
        } else {
            "succeeded"
        }
        .to_string();

        if screenshots_broken {
            screenshots = "failed".to_string();
        } else if let (Some(source), false) = (source.as_ref(), planned.is_empty()) {
            match inputs
                .decoder
                .extract_frames(source, &planned, &ctx.cancel_token())
            {
                Ok(extracted) => {
                    images = extracted
                        .into_iter()
                        .map(|(stamp, png)| (frames::screenshot_name(stamp), png))
                        .collect();
                    if images.is_empty() {
                        screenshots = "none".to_string();
                    }
                }
                Err(err) => {
                    // One broken decode means the rest will break too: say so
                    // once, record it per item, and write the items without
                    // pictures rather than failing work the model already did.
                    screenshots_broken = true;
                    screenshots = "failed".to_string();
                    warnings.push(format!(
                        "screenshots failed: {err}; items were written without images"
                    ));
                }
            }
        } else if source.is_none() {
            screenshots = "none".to_string();
        }

        wire::artifacts::write_item(
            &parent,
            item.title(),
            &front_matter(item, &context, &screenshots),
            item.body(),
            &images,
        )
        .map_err(|err| {
            JobFailure::new(
                ErrorKind::Internal,
                format!("could not write an item: {err}"),
            )
        })?;

        let done = (index + 1) as f64 / items.len() as f64;
        ctx.set_progress(LLM_PROGRESS_SHARE + (1.0 - LLM_PROGRESS_SHARE) * done);
    }

    Ok(items.len())
}

/// A path's last component, as a string.
fn name_of(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The recording inside a meeting folder, if it still has one.
fn source_file(meeting_dir: &Path) -> Option<PathBuf> {
    std::fs::read_dir(meeting_dir)
        .ok()?
        .flatten()
        .find_map(|entry| {
            let path = entry.path();
            let stem_matches = path
                .file_stem()
                .is_some_and(|stem| stem == vault::paths::SOURCE_STEM);
            (stem_matches && path.is_file()).then_some(path)
        })
}

/// Write `<project>/reports/<date>/report.md` from everything the project has.
pub fn write_report(
    inputs: &mut JobInputs<'_>,
    project_dir: &Path,
    output_dir: &Path,
    ctx: &JobContext,
) -> Result<JobOutcome, JobFailure> {
    let materials = report::collect_project_materials(project_dir);
    if materials.trim().is_empty() {
        return Err(JobFailure::new(
            ErrorKind::UnsupportedInput,
            "the project has no summaries or items to report on".to_string(),
        ));
    }

    let project = project_dir
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();

    let report = report::report_from_materials(
        inputs.llm,
        &materials,
        &project,
        chunk_budget(inputs.ctx_tokens),
        &inputs.options,
        ctx,
    )?;

    wire::atomic::write_text(
        &output_dir.join("report.md"),
        &format!("{}\n", report.markdown.trim_end()),
    )
    .map_err(write_failure)?;
    if let Some(reasoning) = report.reasoning_document() {
        wire::atomic::write_text(&output_dir.join("report.reasoning.md"), &reasoning)
            .map_err(write_failure)?;
    }

    ctx.set_progress(1.0);
    Ok(JobOutcome {
        result_json: Some(serde_json::json!({"report": "report.md"}).to_string()),
        ..Default::default()
    })
}

fn write_failure(err: std::io::Error) -> JobFailure {
    JobFailure::new(ErrorKind::Internal, format!("could not write: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_chunk_budget_never_falls_below_a_usable_size() {
        // A tiny configured window would otherwise produce chunks too small to
        // summarise coherently.
        assert_eq!(chunk_budget(16384), 8192);
        assert_eq!(chunk_budget(512), MIN_CHUNK_BUDGET);
    }

    #[test]
    fn a_meeting_without_a_transcript_is_unsupported_input_not_a_broken_assistant() {
        let dir = tempfile::tempdir().unwrap();
        let err = load_transcript(dir.path()).expect_err("should fail");
        assert_eq!(err.kind, ErrorKind::UnsupportedInput);
    }

    #[test]
    fn an_empty_transcript_is_refused_by_name() {
        let dir = tempfile::tempdir().unwrap();
        let doc = TranscriptDoc {
            schema_version: wire::SCHEMA_VERSION,
            created_at: "2026-08-23T18:00:00+00:00".to_string(),
            source: wire::transcript::Source {
                path: "a.mp4".to_string(),
                filename: "a.mp4".to_string(),
                duration_sec: 0.0,
            },
            provider: wire::transcript::ProviderInfo {
                name: "local".to_string(),
                model: "large-v3".to_string(),
                device: "cpu".to_string(),
                compute_type: "ggml".to_string(),
            },
            language: None,
            language_probability: None,
            text: String::new(),
            segments: Vec::new(),
            stats: wire::transcript::Stats {
                elapsed_sec: 0.0,
                realtime_factor: 0.0,
                cost_usd: None,
                currency: None,
            },
            diarization: None,
        };
        std::fs::write(
            dir.path().join(wire::TRANSCRIPT_FILE_NAME),
            doc.to_json().unwrap(),
        )
        .unwrap();

        let err = load_transcript(dir.path()).expect_err("should fail");
        assert_eq!(err.kind, ErrorKind::UnsupportedInput);
        assert!(err.message.contains("no segments"), "{}", err.message);
    }

    #[test]
    fn a_missing_speakers_file_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        assert!(speaker_names(dir.path()).is_empty());
    }

    #[test]
    fn a_malformed_speakers_file_falls_back_to_the_diarizers_labels() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("speakers.json"), "{not json").unwrap();
        assert!(speaker_names(dir.path()).is_empty());
    }

    #[test]
    fn the_recording_is_found_whatever_container_it_is_in() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("source.webm"), b"x").unwrap();
        assert_eq!(
            source_file(dir.path()).unwrap().file_name().unwrap(),
            "source.webm"
        );

        let empty = tempfile::tempdir().unwrap();
        assert!(source_file(empty.path()).is_none());
    }
}
