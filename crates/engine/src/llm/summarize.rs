//! Map-reduce summarization over pre-budgeted transcript chunks.
//!
//! Port of `services/transcription/src/transcription/llm/summarize.py`, with
//! the Python callback replaced by the engine trait: the caller injects a
//! [`LlmEngineApi`], so this module is testable without a model and cannot
//! reach llama.cpp behind the caller's back.
//!
//! The chunking itself is not done here. The caller renders the transcript to
//! lines and cuts them to the model's budget with [`crate::llm::chunking`],
//! because only the caller knows the context window; this module decides how
//! many calls those chunks are worth and in what order.

use crate::jobs::JobContext;
use crate::llm::prompts;
use crate::llm::{CompleteOptions, LlmEngineApi, LlmError, Message};

/// A finished Markdown answer, plus the thinking that was kept out of it.
///
/// The two are separated by the engine (see [`crate::llm::split_reasoning`]);
/// this type keeps them separated all the way to the caller, because reasoning
/// must never reach `summary.md`, `report.md` or the UI. The caller may write
/// [`Summary::reasoning_document`] to a `*.reasoning.md` sidecar.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Summary {
    pub markdown: String,
    /// One entry per model call that thought out loud, in call order.
    pub reasoning: Vec<String>,
}

impl Summary {
    /// The sidecar document, or `None` when the model never thought out loud.
    ///
    /// Calls are separated by a horizontal rule, so a reader can tell one
    /// call's thinking from the next; the shape matches what the Python
    /// service wrote, since these sidecars already exist in operators' vaults.
    pub fn reasoning_document(&self) -> Option<String> {
        if self.reasoning.is_empty() {
            return None;
        }
        Some(format!("{}\n", self.reasoning.join("\n\n---\n\n")))
    }
}

/// Summarize a transcript given as pre-budgeted chunks.
///
/// One chunk goes straight to a single summary call; several go through map
/// (one compact summary per chunk) then reduce (merge into one), which is the
/// only shape that summarizes a transcript longer than the context window
/// without dropping any of it.
///
/// Progress is reported per completed call, not per token: the engine reports
/// nothing finer, and a bar that moves once per call is honest about a job
/// whose cost really is one call at a time.
pub fn summarize_chunks(
    engine: &mut dyn LlmEngineApi,
    chunks: &[String],
    options: &CompleteOptions,
    job: &JobContext,
) -> Result<Summary, LlmError> {
    if chunks.is_empty() {
        // The caller is expected to reject an empty transcript earlier, with
        // an attribution this layer cannot make ("unsupported input", not "the
        // assistant failed"). This guard exists so a bug upstream cannot
        // quietly produce an empty summary instead.
        return Err(LlmError::Generation(
            "cannot summarize an empty transcript".to_string(),
        ));
    }

    // The reduce call is a call too, so a two-chunk transcript costs three.
    let total_calls = chunks.len() + usize::from(chunks.len() > 1);
    let mut summary = Summary::default();
    let mut calls_done = 0usize;

    if chunks.len() == 1 {
        summary.markdown = complete_text(
            engine,
            &prompts::summary_messages(&chunks[0]),
            options,
            job,
            &mut summary.reasoning,
        )?;
        report(job, 1, total_calls);
        return Ok(summary);
    }

    let mut partials = Vec::with_capacity(chunks.len());
    for (index, chunk) in chunks.iter().enumerate() {
        partials.push(complete_text(
            engine,
            &prompts::chunk_summary_messages(chunk, index, chunks.len()),
            options,
            job,
            &mut summary.reasoning,
        )?);
        calls_done += 1;
        report(job, calls_done, total_calls);
    }

    summary.markdown = complete_text(
        engine,
        &prompts::merge_summaries_messages(&partials),
        options,
        job,
        &mut summary.reasoning,
    )?;
    report(job, total_calls, total_calls);
    Ok(summary)
}

/// One completion: the trimmed answer, with any thinking moved to `reasoning`.
///
/// Shared with [`crate::llm::report`], which runs the same map-reduce over a
/// project's materials rather than a transcript's lines.
pub(crate) fn complete_text(
    engine: &mut dyn LlmEngineApi,
    messages: &[Message],
    options: &CompleteOptions,
    job: &JobContext,
    reasoning: &mut Vec<String>,
) -> Result<String, LlmError> {
    // Checked here as well as inside the engine: a cancelled job must not pay
    // for the next call of a map-reduce just because the previous one returned.
    if job.is_cancelled() {
        return Err(LlmError::Cancelled);
    }
    let completion = engine.complete(messages, options, job)?;
    if let Some(thought) = completion.reasoning {
        let thought = thought.trim();
        if !thought.is_empty() {
            reasoning.push(thought.to_string());
        }
    }
    Ok(completion.text.trim().to_string())
}

/// Progress stops just short of done: the caller still has artifacts to write.
fn report(job: &JobContext, calls_done: usize, total_calls: usize) {
    let fraction = calls_done as f64 / total_calls.max(1) as f64;
    job.set_progress(fraction.min(0.99));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::CancelToken;
    use crate::llm::{Completion, LlmInfo};

    /// An engine that answers from a script and records what it was asked.
    struct ScriptedEngine {
        answers: Vec<Result<Completion, LlmError>>,
        seen: Vec<Vec<Message>>,
        /// Cancelled once this many calls have been answered, to stand in for
        /// the operator pressing stop mid-way through a map-reduce.
        cancel_after: Option<(usize, CancelToken)>,
    }

    impl ScriptedEngine {
        fn new(texts: &[&str]) -> Self {
            ScriptedEngine {
                answers: texts.iter().map(|t| Ok(answer(t, None))).collect(),
                seen: Vec::new(),
                cancel_after: None,
            }
        }
    }

    fn answer(text: &str, reasoning: Option<&str>) -> Completion {
        Completion {
            text: text.to_string(),
            reasoning: reasoning.map(str::to_string),
            prompt_tokens: None,
            completion_tokens: None,
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
            if let Some((after, token)) = &self.cancel_after {
                if self.seen.len() >= *after {
                    token.cancel();
                }
            }
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

    fn context() -> (JobContext, CancelToken) {
        let token = CancelToken::default();
        (JobContext::detached(token.clone()), token)
    }

    fn chunks(texts: &[&str]) -> Vec<String> {
        texts.iter().map(|t| t.to_string()).collect()
    }

    #[test]
    fn a_single_chunk_is_summarized_in_one_call() {
        let mut engine = ScriptedEngine::new(&["  # Summary\n\nIt went well.  "]);
        let (job, _token) = context();

        let summary = summarize_chunks(
            &mut engine,
            &chunks(&["[0:00] Hello"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("summarizes");

        assert_eq!(engine.seen.len(), 1);
        assert_eq!(summary.markdown, "# Summary\n\nIt went well.");
        assert!(summary.reasoning.is_empty());
    }

    #[test]
    fn several_chunks_are_mapped_then_reduced_into_one_answer() {
        let mut engine = ScriptedEngine::new(&["part one", "part two", "the merge"]);
        let (job, _token) = context();

        let summary = summarize_chunks(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("summarizes");

        assert_eq!(summary.markdown, "the merge");
        assert_eq!(engine.seen.len(), 3, "two maps and one reduce");
        // The reduce call must be given both partials, or the merge is a
        // summary of a summary of half the meeting.
        let reduce = &engine.seen[2][1].content;
        assert!(reduce.contains("part one"), "{reduce}");
        assert!(reduce.contains("part two"), "{reduce}");
    }

    #[test]
    fn thinking_is_collected_and_kept_out_of_the_markdown() {
        let mut engine = ScriptedEngine {
            answers: vec![
                Ok(answer("part one", Some("  first thought  "))),
                Ok(answer("part two", None)),
                Ok(answer("the merge", Some("second thought"))),
            ],
            seen: Vec::new(),
            cancel_after: None,
        };
        let (job, _token) = context();

        let summary = summarize_chunks(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect("summarizes");

        assert_eq!(summary.markdown, "the merge");
        assert_eq!(summary.reasoning, vec!["first thought", "second thought"]);
        assert_eq!(
            summary.reasoning_document().expect("a sidecar"),
            "first thought\n\n---\n\nsecond thought\n"
        );
    }

    #[test]
    fn a_model_that_never_thought_out_loud_leaves_no_sidecar() {
        let summary = Summary {
            markdown: "text".to_string(),
            reasoning: Vec::new(),
        };
        assert_eq!(summary.reasoning_document(), None);
    }

    #[test]
    fn an_empty_transcript_is_refused_rather_than_summarized() {
        let mut engine = ScriptedEngine::new(&[]);
        let (job, _token) = context();

        let error = summarize_chunks(&mut engine, &[], &CompleteOptions::default(), &job)
            .expect_err("refuses");

        assert!(matches!(error, LlmError::Generation(_)), "{error}");
        assert!(engine.seen.is_empty(), "no call is made");
    }

    #[test]
    fn cancelling_mid_map_stops_before_the_next_call() {
        let token = CancelToken::default();
        let mut engine = ScriptedEngine {
            answers: vec![
                Ok(answer("part one", None)),
                Ok(answer("part two", None)),
                Ok(answer("the merge", None)),
            ],
            seen: Vec::new(),
            cancel_after: Some((1, token.clone())),
        };
        let job = JobContext::detached(token);

        let error = summarize_chunks(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("stops");

        assert!(matches!(error, LlmError::Cancelled), "{error}");
        assert_eq!(engine.seen.len(), 1, "the second map call never happens");
    }

    #[test]
    fn a_failing_call_fails_the_summary_rather_than_returning_half_of_it() {
        let mut engine = ScriptedEngine {
            answers: vec![
                Ok(answer("part one", None)),
                Err(LlmError::Generation("out of context".to_string())),
            ],
            seen: Vec::new(),
            cancel_after: None,
        };
        let (job, _token) = context();

        let error = summarize_chunks(
            &mut engine,
            &chunks(&["a", "b"]),
            &CompleteOptions::default(),
            &job,
        )
        .expect_err("fails");

        assert!(matches!(error, LlmError::Generation(_)), "{error}");
    }
}
