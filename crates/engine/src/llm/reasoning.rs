//! Separating a reasoning model's chain-of-thought from its answer.
//!
//! Port of `services/transcription/src/transcription/llm/reasoning.py`.
//!
//! Qwen-family reasoning models think in `<think>...</think>` blocks. Two
//! shapes occur in practice:
//!
//! - the completion carries the full pair, or
//! - the chat template put the opening `<think>` in the *prompt*, so the
//!   completion is `<reasoning...></think><answer>` -- a lone closer with
//!   everything before it being thought. This is what llama.cpp's Qwen
//!   template actually produces, so it is the ordinary case, not the exotic
//!   one.
//!
//! The operator never wants the thinking in an artifact: the caller writes the
//! answer into `summary.md`/items/reports and may save the reasoning to a
//! sidecar file the UI does not show.
//!
//! Tag matching is done by hand rather than with a regex engine. Two literal
//! spellings, case-insensitively, is less code than a dependency, and
//! ASCII-lowercasing the haystack leaves every byte offset and character
//! boundary exactly where it was in the original text.

/// Both spellings of the opening tag. `<think>` is not a prefix of
/// `<thinking>` -- the next character after `think` differs -- so no position
/// can match two of these at once.
const OPENERS: [&str; 2] = ["<think>", "<thinking>"];
const CLOSERS: [&str; 2] = ["</think>", "</thinking>"];

/// Split a completion into `(answer, reasoning)`, where `reasoning` is `None`
/// when the model did not think out loud.
///
/// Both parts come back trimmed. Several thought blocks are joined by a blank
/// line, and an empty one contributes nothing at all.
pub fn split_reasoning(text: &str) -> (String, Option<String>) {
    let lower = text.to_ascii_lowercase();
    let mut reasoning_parts: Vec<String> = Vec::new();
    let mut cleaned = String::with_capacity(text.len());

    let mut cursor = 0;
    while let Some((open_start, open_end)) = find_tag(&lower, cursor, &OPENERS) {
        // An opener with no closer after it is left exactly where it stands: a
        // completion cut short by the token limit is still the only output
        // there is, and emptying it would lose the lot.
        let Some((close_start, close_end)) = find_tag(&lower, open_end, &CLOSERS) else {
            break;
        };
        cleaned.push_str(&text[cursor..open_start]);
        let thought = text[open_end..close_start].trim();
        if !thought.is_empty() {
            reasoning_parts.push(thought.to_string());
        }
        cursor = close_end;
    }
    cleaned.push_str(&text[cursor..]);

    if reasoning_parts.is_empty() {
        // The lone-closer shape: the template opened the tag in the prompt, so
        // everything up to the first closer is thought.
        let cleaned_lower = cleaned.to_ascii_lowercase();
        if let Some((start, end)) = find_tag(&cleaned_lower, 0, &CLOSERS) {
            let thought = cleaned[..start].trim();
            if !thought.is_empty() {
                reasoning_parts.push(thought.to_string());
            }
            cleaned = cleaned[end..].to_string();
        }
    }

    let reasoning = if reasoning_parts.is_empty() {
        None
    } else {
        Some(reasoning_parts.join("\n\n"))
    };
    (cleaned.trim().to_string(), reasoning)
}

/// The byte range of the earliest of `tags` occurring at or after `from`.
///
/// `lower` must be the ASCII-lowercased form of the text the caller intends to
/// slice, and `from` a byte offset into it.
fn find_tag(lower: &str, from: usize, tags: &[&str]) -> Option<(usize, usize)> {
    tags.iter()
        .filter_map(|tag| {
            lower[from..]
                .find(tag)
                .map(|at| (from + at, from + at + tag.len()))
        })
        .min_by_key(|(start, _)| *start)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lone_closing_tag_still_splits_the_reasoning() {
        // The shape llama.cpp's Qwen template produces: the opening tag was
        // spent on the prompt, so the completion begins mid-thought.
        let (answer, reasoning) = split_reasoning(
            "Here's a thinking process:\n1. Analyze.\n</think>\n\n# Summary\n\nThe answer.",
        );
        assert_eq!(answer, "# Summary\n\nThe answer.");
        assert!(reasoning.unwrap().contains("thinking process"));
    }

    #[test]
    fn a_matched_pair_is_lifted_out_of_the_answer() {
        let (answer, reasoning) = split_reasoning("<think>hmm</think>The answer.");
        assert_eq!(answer, "The answer.");
        assert_eq!(reasoning.as_deref(), Some("hmm"));
    }

    #[test]
    fn text_with_no_tag_at_all_is_all_answer() {
        let (answer, reasoning) = split_reasoning("Just an answer, no thinking.");
        assert_eq!(answer, "Just an answer, no thinking.");
        assert_eq!(reasoning, None);
    }

    #[test]
    fn the_long_spelling_and_odd_casing_are_both_recognised() {
        let (answer, reasoning) = split_reasoning("<Thinking>hmm</THINKING>Answer.");
        assert_eq!(answer, "Answer.");
        assert_eq!(reasoning.as_deref(), Some("hmm"));
    }

    #[test]
    fn several_thought_blocks_are_joined_by_a_blank_line() {
        let (answer, reasoning) = split_reasoning("<think>one</think>mid<think>two</think>end");
        assert_eq!(answer, "midend");
        assert_eq!(reasoning.as_deref(), Some("one\n\ntwo"));
    }

    #[test]
    fn an_empty_thought_block_leaves_no_reasoning_behind() {
        let (answer, reasoning) = split_reasoning("<think>   </think>Answer.");
        assert_eq!(answer, "Answer.");
        assert_eq!(reasoning, None);
    }

    #[test]
    fn a_closer_with_nothing_before_it_leaves_no_reasoning_behind() {
        let (answer, reasoning) = split_reasoning("</think>\n\nAnswer.");
        assert_eq!(answer, "Answer.");
        assert_eq!(reasoning, None);
    }

    #[test]
    fn an_unclosed_opening_tag_is_left_where_it_stands() {
        let (answer, reasoning) = split_reasoning("<think>truncated thought");
        assert_eq!(answer, "<think>truncated thought");
        assert_eq!(reasoning, None);
    }

    #[test]
    fn a_reasoning_prefix_leaves_the_json_that_follows_it_parseable() {
        // Extraction jobs feed the answer straight to a JSON parser; a
        // surviving preamble would cost a whole repair round-trip.
        let (answer, reasoning) = split_reasoning("Let me think.\n</think>\n{\"items\": []}");
        assert_eq!(answer, "{\"items\": []}");
        assert_eq!(reasoning.as_deref(), Some("Let me think."));
    }

    #[test]
    fn a_lone_closer_after_a_matched_pair_is_left_alone() {
        // Once a pair has been found the completion is the well-formed shape,
        // and a later `</think>` is the model quoting itself, not a boundary.
        let (answer, reasoning) = split_reasoning("<think>hmm</think>Answer: use </think> here.");
        assert_eq!(answer, "Answer: use </think> here.");
        assert_eq!(reasoning.as_deref(), Some("hmm"));
    }
}
