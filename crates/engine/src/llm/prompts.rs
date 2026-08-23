//! Prompt builders for the LLM job types.
//!
//! Port of `services/transcription/src/transcription/llm/prompts.py`.
//!
//! Pure string assembly: nothing here touches the filesystem, the model or the
//! job table, so every prompt is comparable byte for byte against the Python
//! service's. Transcripts are rendered as `[m:ss] Speaker: text` lines because
//! the extraction jobs ask the model to cite moments back in seconds, and a
//! marker it can read is the only thing that makes the citation possible.
//!
//! Every prompt carries the language rule: these are the operator's own
//! meetings, and a Russian meeting must come back with a Russian summary. The
//! model is told to answer in the transcript's language rather than being given
//! a detected language code, so a bilingual meeting is not forced into one.

use std::collections::HashMap;

use wire::transcript::Segment;

use crate::llm::Message;

/// Appended to every system prompt. Repeated in each rather than hoisted into
/// one shared preamble because each system prompt is one sentence about the
/// role plus this, and the model reads it best at the end.
const LANGUAGE_RULE: &str = "Write your answer in the same language the transcript is written in. \
Keep technical terms, product names and code identifiers as they appear.";

/// `m:ss` under an hour, `h:mm:ss` above it. The brackets belong to the caller.
pub fn format_timestamp(seconds: f64) -> String {
    // Truncating toward zero, and clamping a negative or non-finite input to
    // the start: this renders a marker, and there is no useful marker before
    // the recording begins.
    let total: u64 = if seconds.is_finite() && seconds > 0.0 {
        seconds as u64
    } else {
        0
    };
    let hours = total / 3600;
    let minutes = (total % 3600) / 60;
    let secs = total % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes}:{secs:02}")
    }
}

/// One `[m:ss] Speaker: text` line per segment.
///
/// `speaker_overrides` maps segment ids in their string form -- the
/// `speakers.json` sidecar's key shape, since JSON object keys are strings --
/// to operator-assigned names, which outrank the diarization label carried on
/// the segment itself. An empty override falls back to the diarized label
/// rather than erasing it.
///
/// Segments whose text is blank contribute no line at all: a marker with
/// nothing after it spends context and tells the model nothing.
pub fn render_transcript_lines(
    segments: &[Segment],
    speaker_overrides: &HashMap<String, String>,
) -> Vec<String> {
    let mut lines = Vec::with_capacity(segments.len());
    for segment in segments {
        let text = segment.text.trim();
        if text.is_empty() {
            continue;
        }
        let stamp = format_timestamp(segment.start);
        let speaker = named(
            speaker_overrides
                .get(&segment.id.to_string())
                .map(String::as_str),
        )
        .or_else(|| named(segment.speaker.as_deref()));
        match speaker {
            Some(name) => lines.push(format!("[{stamp}] {name}: {text}")),
            None => lines.push(format!("[{stamp}] {text}")),
        }
    }
    lines
}

/// A usable speaker name, or `None` for one that is absent or blank.
///
/// Blank is treated as absent at every level. Python decided this by
/// truthiness, which made `""` fall through to the diarized label but let a
/// whitespace-only name stand and render as `[0:00]   : text`; a cleared
/// assignment means the same thing however many spaces it holds.
fn named(name: Option<&str>) -> Option<&str> {
    name.map(str::trim).filter(|name| !name.is_empty())
}

/// Summarize a transcript that fits in one chunk.
pub fn summary_messages(transcript_text: &str) -> Vec<Message> {
    vec![
        Message::system(format!(
            "You are a meticulous meeting analyst. You write concise, \
well-structured Markdown summaries of meeting transcripts. {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "Summarize this meeting transcript as Markdown. Structure: a short \
overview paragraph, then sections for key discussion points, decisions made, \
and open questions. Omit a section when the meeting had nothing for it. Do not \
invent content that is not in the transcript.\n\nTranscript:\n\n{transcript_text}"
        )),
    ]
}

/// The map half of map-reduce: summarize one chunk of a long transcript.
///
/// `index` is zero-based; the prompt counts from one, as a person would.
pub fn chunk_summary_messages(chunk_text: &str, index: usize, total: usize) -> Vec<Message> {
    let part = index + 1;
    vec![
        Message::system(format!(
            "You are a meticulous meeting analyst summarizing one part of a \
longer meeting transcript. {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "This is part {part} of {total} of a meeting transcript. Write a \
compact Markdown summary of this part only: key points, decisions, open \
questions. Do not speculate about the other parts.\n\nTranscript part:\n\n{chunk_text}"
        )),
    ]
}

/// The reduce half of map-reduce: merge per-chunk summaries into one.
pub fn merge_summaries_messages(partial_summaries: &[String]) -> Vec<Message> {
    let numbered: Vec<String> = partial_summaries
        .iter()
        .enumerate()
        .map(|(index, summary)| format!("--- Part {} summary ---\n{summary}", index + 1))
        .collect();
    vec![
        Message::system(format!(
            "You are a meticulous meeting analyst. You merge partial summaries \
of one meeting into a single coherent Markdown summary. {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "Merge these partial summaries of one meeting into a single Markdown \
summary. Structure: a short overview paragraph, then sections for key \
discussion points, decisions made, and open questions. Deduplicate overlapping \
points.\n\n{}",
            numbered.join("\n\n")
        )),
    ]
}

/// The taxonomy the model classifies against; it matches
/// [`crate::llm::shapes::ItemType`] variant for variant, and the grammar will
/// not let it answer with anything else.
const ACTION_ITEM_RULES: &str = "An action item is concrete follow-up work someone should do. \
Classify each as: 'requirement' (a stated product/system requirement), 'epic' (a large body of \
work spanning multiple tasks), 'task' (a concrete, bounded piece of work), or 'spike' (a \
time-boxed investigation to answer a question). For each item give a short imperative title, a \
Markdown description with all relevant context from the discussion, and the timestamps (in \
seconds, from the [m:ss] markers) of the transcript moments where it was discussed.";

pub fn action_items_messages(chunk_text: &str) -> Vec<Message> {
    vec![
        Message::system(format!(
            "You extract action items from meeting transcripts and answer in \
strict JSON matching the provided schema. {ACTION_ITEM_RULES} {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "Extract every action item from this transcript part. If there are \
none, return an empty items list.\n\nTranscript:\n\n{chunk_text}"
        )),
    ]
}

/// The same for facts, matching [`crate::llm::shapes::FactKind`].
const FACT_RULES: &str = "A fact is a concrete piece of information stated in the meeting that is \
worth remembering (a constraint, a metric, a date, how something works). An answered question is \
a question someone asked that got a substantive answer. For each, give a short declarative title, \
a Markdown description (for answered questions: the question and its answer), and the timestamps \
(in seconds, from the [m:ss] markers) of the transcript moments involved.";

pub fn facts_messages(chunk_text: &str) -> Vec<Message> {
    vec![
        Message::system(format!(
            "You extract notable facts and answered questions from meeting \
transcripts and answer in strict JSON matching the provided schema. \
{FACT_RULES} {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "Extract the notable facts and answered questions from this \
transcript part. If there are none, return an empty items list.\n\nTranscript:\n\n{chunk_text}"
        )),
    ]
}

/// The one bounded retry after invalid structured output: show the model its
/// own answer and the validation error, and ask again.
///
/// The original exchange is kept in front so the transcript it was reading is
/// still in context; replacing it with the error alone would ask the model to
/// re-answer a question it can no longer see.
pub fn repair_messages(original: &[Message], raw_output: &str, error: &str) -> Vec<Message> {
    let mut messages = original.to_vec();
    messages.push(Message::assistant(raw_output));
    messages.push(Message::user(format!(
        "Your previous answer was not valid against the required JSON schema: \
{error}\nAnswer again with only valid JSON."
    )));
    messages
}

/// The project-essence status report over all collected materials.
pub fn report_messages(materials_text: &str, project: &str) -> Vec<Message> {
    vec![
        Message::system(format!(
            "You are a project analyst. From meeting summaries, action items and \
recorded facts you write a single project status report in Markdown. {LANGUAGE_RULE}"
        )),
        Message::user(format!(
            "Write a status report for project {project} based on the materials \
below. Structure: a project overview, current status, key decisions, open \
questions and risks, and a table of action items grouped by type (requirement / \
epic / task / spike). Base everything strictly on the materials; do not invent \
progress.\n\nMaterials:\n\n{materials_text}"
        )),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Role;

    fn segment(id: i64, start: f64, text: &str, speaker: Option<&str>) -> Segment {
        Segment {
            id,
            start,
            end: start + 1.0,
            text: text.to_string(),
            avg_logprob: None,
            no_speech_prob: None,
            compression_ratio: None,
            words: None,
            speaker: speaker.map(str::to_string),
        }
    }

    #[test]
    fn a_timestamp_under_an_hour_omits_the_hour_field() {
        assert_eq!(format_timestamp(0.0), "0:00");
        assert_eq!(format_timestamp(9.9), "0:09");
        assert_eq!(format_timestamp(65.0), "1:05");
        assert_eq!(format_timestamp(3599.0), "59:59");
    }

    #[test]
    fn an_hour_or_more_grows_a_third_field() {
        assert_eq!(format_timestamp(3600.0), "1:00:00");
        assert_eq!(format_timestamp(3725.0), "1:02:05");
    }

    #[test]
    fn a_timestamp_before_the_recording_renders_as_the_start() {
        assert_eq!(format_timestamp(-5.0), "0:00");
        assert_eq!(format_timestamp(f64::NAN), "0:00");
    }

    #[test]
    fn each_segment_becomes_one_marked_line() {
        let lines = render_transcript_lines(
            &[
                segment(0, 0.0, "Hello", Some("SPEAKER_00")),
                segment(1, 61.0, "Goodbye", Some("SPEAKER_01")),
            ],
            &HashMap::new(),
        );
        assert_eq!(
            lines,
            vec![
                "[0:00] SPEAKER_00: Hello".to_string(),
                "[1:01] SPEAKER_01: Goodbye".to_string(),
            ]
        );
    }

    #[test]
    fn an_operator_name_outranks_the_diarized_label() {
        let mut overrides = HashMap::new();
        overrides.insert("1".to_string(), "Nikita".to_string());
        let lines = render_transcript_lines(
            &[
                segment(0, 0.0, "Hello", Some("SPEAKER_00")),
                segment(1, 5.0, "Hi", Some("SPEAKER_01")),
            ],
            &overrides,
        );
        assert_eq!(lines[0], "[0:00] SPEAKER_00: Hello");
        assert_eq!(lines[1], "[0:05] Nikita: Hi");
    }

    #[test]
    fn an_empty_override_falls_back_to_the_diarized_label() {
        // A sidecar can hold a cleared assignment; that is a request to use
        // the diarized label again, not a request for an anonymous line.
        let mut overrides = HashMap::new();
        overrides.insert("0".to_string(), "  ".to_string());
        let lines =
            render_transcript_lines(&[segment(0, 0.0, "Hello", Some("SPEAKER_00"))], &overrides);
        assert_eq!(lines[0], "[0:00] SPEAKER_00: Hello");
    }

    #[test]
    fn a_segment_without_a_speaker_still_gets_its_marker() {
        let lines = render_transcript_lines(&[segment(0, 12.0, "Hello", None)], &HashMap::new());
        assert_eq!(lines, vec!["[0:12] Hello".to_string()]);
    }

    #[test]
    fn a_blank_segment_contributes_no_line() {
        let lines = render_transcript_lines(
            &[
                segment(0, 0.0, "   ", Some("SPEAKER_00")),
                segment(1, 1.0, " Hello ", Some("SPEAKER_00")),
            ],
            &HashMap::new(),
        );
        assert_eq!(lines, vec!["[0:01] SPEAKER_00: Hello".to_string()]);
    }

    #[test]
    fn every_prompt_asks_for_the_transcripts_own_language() {
        // The operator's meetings are not all in English; a summary in the
        // wrong language is unusable, so this rule is in all of them.
        let prompts = [
            summary_messages("t"),
            chunk_summary_messages("t", 0, 2),
            merge_summaries_messages(&["a".to_string()]),
            action_items_messages("t"),
            facts_messages("t"),
            report_messages("m", "ELS"),
        ];
        for messages in prompts {
            assert_eq!(messages[0].role, Role::System);
            assert!(
                messages[0].content.contains("same language"),
                "missing the language rule: {}",
                messages[0].content
            );
        }
    }

    #[test]
    fn the_chunk_prompt_counts_parts_from_one() {
        let messages = chunk_summary_messages("body", 0, 3);
        assert!(messages[1].content.contains("part 1 of 3"));
        assert!(messages[1].content.contains("body"));
    }

    #[test]
    fn merged_partials_are_numbered_and_separated() {
        let messages = merge_summaries_messages(&["first".to_string(), "second".to_string()]);
        assert!(messages[1]
            .content
            .contains("--- Part 1 summary ---\nfirst"));
        assert!(messages[1]
            .content
            .contains("--- Part 2 summary ---\nsecond"));
    }

    #[test]
    fn the_extraction_prompts_name_every_variant_of_their_schema() {
        // The grammar constrains the answer to these; the prompt has to define
        // them or the model is guessing at what the labels mean.
        let action = action_items_messages("t")[0].content.clone();
        for variant in ["requirement", "epic", "task", "spike"] {
            assert!(action.contains(variant), "{variant} missing from {action}");
        }
        let facts = facts_messages("t")[0].content.clone();
        assert!(facts.contains("answered question"));
    }

    #[test]
    fn the_extraction_prompts_say_an_empty_answer_is_allowed() {
        // A meeting with no action items is a real outcome; without this the
        // model invents one to fill the list.
        assert!(action_items_messages("t")[1]
            .content
            .contains("empty items list"));
        assert!(facts_messages("t")[1].content.contains("empty items list"));
    }

    #[test]
    fn a_repair_keeps_the_original_exchange_in_front_of_the_error() {
        let original = action_items_messages("the transcript");
        let repair = repair_messages(&original, "not json", "output does not match the schema");

        assert_eq!(repair.len(), original.len() + 2);
        assert_eq!(&repair[..original.len()], &original[..]);
        assert_eq!(repair[original.len()].role, Role::Assistant);
        assert_eq!(repair[original.len()].content, "not json");
        assert_eq!(repair[original.len() + 1].role, Role::User);
        assert!(repair[original.len() + 1]
            .content
            .contains("output does not match the schema"));
    }

    #[test]
    fn the_report_prompt_names_the_project_and_forbids_invented_progress() {
        let messages = report_messages("materials", "ELS");
        assert!(messages[1].content.contains("project ELS"));
        assert!(messages[1].content.contains("do not invent"));
        assert!(messages[1].content.contains("materials"));
    }
}
