//! Action-item and fact extraction: the chunked calls, and the cleanup after.
//!
//! Port of `services/transcription/src/transcription/llm/extraction.py`
//! together with the `_constrained_items`/`_extract_sync` orchestration that
//! lived in `jobs.py`. The two belong together: the merge rules only make
//! sense next to the loop that produces the per-chunk lists they merge.
//!
//! A long transcript is extracted chunk by chunk, so the same action item can
//! be restated in two chunks and the model's cited timestamps are only as good
//! as its reading of the `[m:ss]` markers. Three passes fix that:
//!
//! 1. **One bounded repair retry per chunk.** A grammar makes malformed output
//!    nearly impossible; when it happens anyway the model is shown its own
//!    answer and the error once, and only a second failure fails the job.
//! 2. **Merge on normalized title**, so a restated item is one item whose
//!    citations are the union of both statements'.
//! 3. **Snap citations to segment starts**, so a screenshot taken at an item's
//!    timestamp lands on a frame that was actually on screen when it was
//!    discussed, not a second and a half into the next sentence.
//!
//! An empty result is a valid outcome throughout: a meeting with no action
//! items is an ordinary meeting, not a failed job.

use std::collections::HashMap;

use wire::transcript::Segment;

use crate::jobs::JobContext;
use crate::llm::prompts;
use crate::llm::shapes::{
    action_items_grammar, facts_grammar, parse_llm_json, ActionItemOut, ActionItemsOut, FactOut,
    FactsOut, MAX_TITLE_CHARS,
};
use crate::llm::{CompleteOptions, LlmEngineApi, LlmError, Message};

/// The share of an extraction job's progress bar the model calls own. The
/// remaining fifth belongs to the caller's screenshot pass and artifact
/// writes, which are slow enough to be worth showing separately.
pub const LLM_PROGRESS_SHARE: f64 = 0.8;

/// What merging and snapping need from an extracted item, so both shapes go
/// through one implementation rather than two that can drift.
pub trait ExtractedItem {
    fn title(&self) -> &str;
    fn set_title(&mut self, title: String);
    fn timestamps(&self) -> &[f64];
    fn set_timestamps(&mut self, timestamps: Vec<f64>);
}

impl ExtractedItem for ActionItemOut {
    fn title(&self) -> &str {
        &self.title
    }
    fn set_title(&mut self, title: String) {
        self.title = title;
    }
    fn timestamps(&self) -> &[f64] {
        &self.timestamps
    }
    fn set_timestamps(&mut self, timestamps: Vec<f64>) {
        self.timestamps = timestamps;
    }
}

impl ExtractedItem for FactOut {
    fn title(&self) -> &str {
        &self.title
    }
    fn set_title(&mut self, title: String) {
        self.title = title;
    }
    fn timestamps(&self) -> &[f64] {
        &self.timestamps
    }
    fn set_timestamps(&mut self, timestamps: Vec<f64>) {
        self.timestamps = timestamps;
    }
}

/// Extract every action item from `chunks`, merged across them.
///
/// An empty vector is a successful outcome.
pub fn extract_action_items(
    engine: &mut dyn LlmEngineApi,
    chunks: &[String],
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<Vec<ActionItemOut>, LlmError> {
    extract::<ActionItems>(engine, chunks, options, job)
}

/// Extract every notable fact and answered question from `chunks`.
pub fn extract_facts(
    engine: &mut dyn LlmEngineApi,
    chunks: &[String],
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<Vec<FactOut>, LlmError> {
    extract::<Facts>(engine, chunks, options, job)
}

/// The three things that differ between the two extractions: the prompt, the
/// grammar, and the wrapper the answer parses into. Everything else -- the
/// repair retry, the title trimming, the merge, the progress split -- is the
/// same, and is written once in [`extract`].
trait Shape {
    type Wrapper: serde::de::DeserializeOwned;
    type Item: ExtractedItem;
    /// The job type as the ledger and the UI spell it, for the failure message.
    const LABEL: &'static str;

    fn messages(chunk: &str) -> Vec<Message>;
    fn grammar() -> String;
    fn into_items(wrapper: Self::Wrapper) -> Vec<Self::Item>;
}

struct ActionItems;

impl Shape for ActionItems {
    type Wrapper = ActionItemsOut;
    type Item = ActionItemOut;
    const LABEL: &'static str = "action_items";

    fn messages(chunk: &str) -> Vec<Message> {
        prompts::action_items_messages(chunk)
    }
    fn grammar() -> String {
        action_items_grammar()
    }
    fn into_items(wrapper: ActionItemsOut) -> Vec<ActionItemOut> {
        wrapper.items
    }
}

struct Facts;

impl Shape for Facts {
    type Wrapper = FactsOut;
    type Item = FactOut;
    const LABEL: &'static str = "facts";

    fn messages(chunk: &str) -> Vec<Message> {
        prompts::facts_messages(chunk)
    }
    fn grammar() -> String {
        facts_grammar()
    }
    fn into_items(wrapper: FactsOut) -> Vec<FactOut> {
        wrapper.items
    }
}

fn extract<S: Shape>(
    engine: &mut dyn LlmEngineApi,
    chunks: &[String],
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<Vec<S::Item>, LlmError> {
    // Built once: the grammar is a few hundred bytes of GBNF assembled by
    // `format!`, and a transcript can be dozens of chunks.
    let grammar = S::grammar();
    let mut per_chunk: Vec<Vec<S::Item>> = Vec::with_capacity(chunks.len());

    for (index, chunk) in chunks.iter().enumerate() {
        if job.is_cancelled() {
            return Err(LlmError::Cancelled);
        }
        let wrapper = constrained_items::<S>(engine, &S::messages(chunk), &grammar, options, job)?;
        let mut items = S::into_items(wrapper);
        for item in &mut items {
            // Trimmed before the merge, so two statements of one item whose
            // titles differ only past the cap still merge into one.
            let trimmed = trim_title(item.title());
            item.set_title(trimmed);
        }
        per_chunk.push(items);
        let fraction = (index + 1) as f64 / chunks.len() as f64;
        job.set_progress((fraction * LLM_PROGRESS_SHARE).min(0.99));
    }

    Ok(merge_items(per_chunk))
}

/// One schema-constrained completion with the one bounded repair retry.
fn constrained_items<S: Shape>(
    engine: &mut dyn LlmEngineApi,
    messages: &[Message],
    grammar: &str,
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<S::Wrapper, LlmError> {
    let constrained = CompleteOptions {
        grammar: Some(grammar.to_string()),
        ..options.clone()
    };
    let first = engine.complete(messages, &constrained, job)?;
    let first_error = match parse_llm_json::<S::Wrapper>(&first.text) {
        Ok(wrapper) => return Ok(wrapper),
        Err(error) => error,
    };

    // The retry runs *unconstrained*. Re-running under the grammar that just
    // produced unusable output tends to reproduce it; letting the model answer
    // freely with its own answer and the error in front of it is the actual
    // second chance. `parse_llm_json` tolerates the code fence that an
    // unconstrained model then tends to add.
    let repair = prompts::repair_messages(messages, &first_error.raw, &first_error.message);
    let free = CompleteOptions {
        grammar: None,
        ..options.clone()
    };
    let second = engine.complete(&repair, &free, job)?;
    parse_llm_json::<S::Wrapper>(&second.text).map_err(|second_error| {
        LlmError::Output(format!(
            "the model returned invalid {} output even after a repair attempt: {}",
            S::LABEL,
            second_error.message
        ))
    })
}

/// Concatenate per-chunk item lists, keeping the first of any duplicates.
///
/// Duplicates are items whose normalized titles match -- the same action item
/// restated in two chunks. The first occurrence wins, and its timestamp list
/// absorbs the duplicate's, so no cited moment is lost.
pub fn merge_items<T: ExtractedItem>(per_chunk: Vec<Vec<T>>) -> Vec<T> {
    let mut merged: Vec<T> = Vec::new();
    let mut first_at: HashMap<String, usize> = HashMap::new();

    for items in per_chunk {
        for item in items {
            let key = normalized_title(item.title());
            match first_at.get(&key) {
                None => {
                    first_at.insert(key, merged.len());
                    merged.push(item);
                }
                Some(&at) => {
                    let mut stamps = merged[at].timestamps().to_vec();
                    for stamp in item.timestamps() {
                        if !stamps.contains(stamp) {
                            stamps.push(*stamp);
                        }
                    }
                    merged[at].set_timestamps(stamps);
                }
            }
        }
    }
    merged
}

/// The key two restatements of one item have in common: whitespace runs
/// collapsed, ends trimmed, case folded.
fn normalized_title(title: &str) -> String {
    title
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// Cap an over-long title at [`MAX_TITLE_CHARS`].
///
/// The model is asked for a short title and occasionally writes a paragraph,
/// which would become an unusable folder name. Cut on a character boundary,
/// not a byte one: a Cyrillic title is two bytes per character and slicing it
/// wrong would panic.
pub fn trim_title(title: &str) -> String {
    let title = title.trim();
    if title.chars().count() <= MAX_TITLE_CHARS {
        return title.to_string();
    }
    title
        .chars()
        .take(MAX_TITLE_CHARS)
        .collect::<String>()
        .trim_end()
        .to_string()
}

/// The start of every segment, which is what a citation snaps onto.
pub fn segment_starts(segments: &[Segment]) -> Vec<f64> {
    segments.iter().map(|segment| segment.start).collect()
}

/// Clamp cited timestamps into the recording and snap them to segment starts.
///
/// A timestamp outside the recording is a hallucinated citation and is dropped
/// rather than clamped: clamping would silently attribute an invented moment
/// to the first or last thing anyone said. The rest snap to the nearest
/// segment start, then deduplicate, preserving citation order.
pub fn snap_timestamps(
    timestamps: &[f64],
    segment_starts: &[f64],
    duration_sec: Option<f64>,
) -> Vec<f64> {
    let mut snapped: Vec<f64> = Vec::new();
    for &stamp in timestamps {
        // JSON cannot spell NaN, but it can spell an exponent that overflows
        // to infinity, and neither can be compared into a useful position.
        if !stamp.is_finite() || stamp < 0.0 {
            continue;
        }
        if duration_sec.is_some_and(|duration| stamp > duration) {
            continue;
        }
        let mut target = stamp;
        if let Some(&nearest) = nearest_start(segment_starts, stamp) {
            target = nearest;
        }
        if !snapped.contains(&target) {
            snapped.push(target);
        }
    }
    snapped
}

/// The closest start to `stamp`, ties going to the earlier one.
fn nearest_start(segment_starts: &[f64], stamp: f64) -> Option<&f64> {
    let mut best: Option<(&f64, f64)> = None;
    for start in segment_starts {
        let distance = (start - stamp).abs();
        let closer = match best {
            None => true,
            Some((_, closest)) => distance < closest,
        };
        if closer {
            best = Some((start, distance));
        }
    }
    best.map(|(start, _)| start)
}

/// Snap every item's citations in place, after merging and before writing.
///
/// Items whose every citation was hallucinated keep an empty list rather than
/// being dropped: the item itself may still be real, and an item without a
/// screenshot is worth more than no item.
pub fn snap_item_timestamps<T: ExtractedItem>(
    items: &mut [T],
    segment_starts: &[f64],
    duration_sec: Option<f64>,
) {
    for item in items {
        let snapped = snap_timestamps(item.timestamps(), segment_starts, duration_sec);
        item.set_timestamps(snapped);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::CancelToken;
    use crate::llm::shapes::{FactKind, ItemType};
    use crate::llm::{Completion, LlmInfo};

    /// An engine that answers from a script and records the options it was
    /// given, so the tests can see whether a call was grammar-constrained.
    struct ScriptedEngine {
        answers: Vec<Result<Completion, LlmError>>,
        seen: Vec<(Vec<Message>, CompleteOptions)>,
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
            options: &CompleteOptions,
            _job: &JobContext,
        ) -> Result<Completion, LlmError> {
            self.seen.push((messages.to_vec(), options.clone()));
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

    fn chunks(texts: &[&str]) -> Vec<String> {
        texts.iter().map(|t| t.to_string()).collect()
    }

    fn item(title: &str, timestamps: &[f64]) -> ActionItemOut {
        ActionItemOut {
            item_type: ItemType::Task,
            title: title.to_string(),
            description_md: String::new(),
            timestamps: timestamps.to_vec(),
        }
    }

    fn segment(id: i64, start: f64) -> Segment {
        Segment {
            id,
            start,
            end: start + 1.0,
            text: "text".to_string(),
            avg_logprob: None,
            no_speech_prob: None,
            compression_ratio: None,
            words: None,
            speaker: None,
        }
    }

    #[test]
    fn each_chunk_gets_one_grammar_constrained_call() {
        let mut engine = ScriptedEngine::new(&[
            r#"{"items": [{"type": "task", "title": "First", "description_md": "", "timestamps": []}]}"#,
            r#"{"items": [{"type": "epic", "title": "Second", "description_md": "", "timestamps": []}]}"#,
        ]);
        let job = context();

        let items = extract_action_items(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("extracts");

        assert_eq!(items.len(), 2);
        assert_eq!(items[1].item_type, ItemType::Epic);
        assert_eq!(engine.seen.len(), 2);
        for (_, options) in &engine.seen {
            let grammar = options.grammar.as_deref().expect("constrained");
            assert!(grammar.contains("itemtype"), "{grammar}");
        }
    }

    #[test]
    fn a_chunk_that_yields_no_items_does_not_fail_the_job() {
        // A meeting with nothing to do is an ordinary meeting.
        let mut engine = ScriptedEngine::new(&[r#"{"items": []}"#]);
        let job = context();

        let items = extract_action_items(
            &mut engine,
            &chunks(&["a"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("succeeds");

        assert!(items.is_empty());
    }

    #[test]
    fn malformed_output_is_repaired_once_and_the_retry_is_unconstrained() {
        let mut engine = ScriptedEngine::new(&[
            "I cannot do that",
            "```json\n{\"items\": [{\"type\": \"spike\", \"title\": \"Look into it\", \"description_md\": \"\", \"timestamps\": []}]}\n```",
        ]);
        let job = context();

        let items = extract_action_items(
            &mut engine,
            &chunks(&["a"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("repairs");

        assert_eq!(items.len(), 1);
        assert_eq!(items[0].item_type, ItemType::Spike);
        assert_eq!(engine.seen.len(), 2, "exactly one retry");
        assert!(
            engine.seen[0].1.grammar.is_some(),
            "the first call is constrained"
        );
        assert!(engine.seen[1].1.grammar.is_none(), "the retry is not");

        // The retry has to show the model its own answer and the error.
        let repair = &engine.seen[1].0;
        assert_eq!(repair[repair.len() - 2].content, "I cannot do that");
        assert!(repair[repair.len() - 1].content.contains("schema"));
    }

    #[test]
    fn a_second_malformed_answer_fails_the_job_rather_than_retrying_again() {
        let mut engine = ScriptedEngine::new(&["not json", "still not json"]);
        let job = context();

        let error = extract_facts(
            &mut engine,
            &chunks(&["a"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("gives up");

        assert!(matches!(error, LlmError::Output(_)), "{error}");
        assert!(error.to_string().contains("facts"), "{error}");
        assert_eq!(engine.seen.len(), 2, "the retry is bounded at one");
    }

    #[test]
    fn cancelling_stops_before_the_next_chunks_call() {
        let token = CancelToken::default();
        let mut engine = ScriptedEngine::new(&[r#"{"items": []}"#, r#"{"items": []}"#]);
        let job = JobContext::detached(token.clone());
        token.cancel();

        let error = extract_action_items(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("stops");

        assert!(matches!(error, LlmError::Cancelled), "{error}");
        assert!(engine.seen.is_empty());
    }

    #[test]
    fn facts_default_to_the_plain_kind_when_the_model_omits_it() {
        let mut engine = ScriptedEngine::new(&[r#"{"items": [{"title": "The API is public"}]}"#]);
        let job = context();

        let items = extract_facts(
            &mut engine,
            &chunks(&["a"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("extracts");

        assert_eq!(items[0].kind, FactKind::Fact);
    }

    #[test]
    fn an_item_restated_in_two_chunks_becomes_one_with_both_citations() {
        let merged = merge_items(vec![
            vec![item("Fix the  login", &[10.0, 20.0])],
            vec![item("  fix   the login  ", &[20.0, 35.0])],
        ]);

        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].title, "Fix the  login", "the first wording wins");
        assert_eq!(merged[0].timestamps, vec![10.0, 20.0, 35.0]);
    }

    #[test]
    fn items_with_different_titles_are_kept_in_first_seen_order() {
        let merged = merge_items(vec![
            vec![item("Second", &[]), item("First", &[])],
            vec![item("Third", &[])],
        ]);

        let titles: Vec<&str> = merged.iter().map(|i| i.title.as_str()).collect();
        assert_eq!(titles, vec!["Second", "First", "Third"]);
    }

    #[test]
    fn merging_nothing_produces_nothing() {
        let merged: Vec<ActionItemOut> = merge_items(vec![Vec::new(), Vec::new()]);
        assert!(merged.is_empty());
    }

    #[test]
    fn a_citation_snaps_to_the_nearest_segment_start() {
        let starts = segment_starts(&[segment(0, 0.0), segment(1, 10.0), segment(2, 25.0)]);
        assert_eq!(snap_timestamps(&[11.5], &starts, Some(60.0)), vec![10.0]);
        assert_eq!(snap_timestamps(&[23.0], &starts, Some(60.0)), vec![25.0]);
    }

    #[test]
    fn a_citation_outside_the_recording_is_dropped_not_clamped() {
        let starts = vec![0.0, 10.0];
        assert_eq!(
            snap_timestamps(&[-1.0, 5.0, 900.0], &starts, Some(60.0)),
            vec![0.0],
            "5.0 snaps to 0.0; the other two are hallucinations"
        );
    }

    #[test]
    fn two_citations_snapping_to_one_segment_become_one() {
        let starts = vec![0.0, 10.0];
        assert_eq!(
            snap_timestamps(&[9.0, 11.0, 1.0], &starts, None),
            vec![10.0, 0.0]
        );
    }

    #[test]
    fn without_segments_a_citation_is_kept_as_the_model_gave_it() {
        assert_eq!(snap_timestamps(&[12.5], &[], Some(60.0)), vec![12.5]);
    }

    #[test]
    fn an_unknown_duration_only_drops_negative_citations() {
        assert_eq!(snap_timestamps(&[-1.0, 900.0], &[], None), vec![900.0]);
    }

    #[test]
    fn snapping_an_item_whose_citations_were_all_invented_leaves_it_in_place() {
        let mut items = vec![item("Real item", &[900.0])];
        snap_item_timestamps(&mut items, &[0.0, 10.0], Some(60.0));

        assert_eq!(items.len(), 1, "the item survives");
        assert!(items[0].timestamps.is_empty());
    }

    #[test]
    fn an_over_long_title_is_cut_on_a_character_boundary() {
        let long = "д".repeat(MAX_TITLE_CHARS + 50);
        let trimmed = trim_title(&long);
        assert_eq!(trimmed.chars().count(), MAX_TITLE_CHARS);
    }

    #[test]
    fn a_short_title_is_only_trimmed_of_its_edges() {
        assert_eq!(trim_title("  Fix the login  "), "Fix the login");
    }

    #[test]
    fn an_over_long_title_is_trimmed_before_items_are_merged() {
        // Two chunks restating one item with a runaway title must still merge.
        let long = format!("{} tail", "x".repeat(MAX_TITLE_CHARS));
        let body = format!(
            r#"{{"items": [{{"type": "task", "title": "{long}", "description_md": "", "timestamps": []}}]}}"#
        );
        let mut engine = ScriptedEngine::new(&[&body, &body]);
        let job = context();

        let items = extract_action_items(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("extracts");

        assert_eq!(items.len(), 1);
        assert_eq!(items[0].title.chars().count(), MAX_TITLE_CHARS);
    }
}
