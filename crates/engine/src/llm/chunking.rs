//! Token-budget chunking for long transcripts.
//!
//! Port of `services/transcription/src/transcription/llm/chunking.py`.
//!
//! The budget is a crude character-count heuristic -- one token per three
//! characters -- deliberately biased low: a chunk that underfills the context
//! window costs one extra LLM call, while a chunk that overflows it costs the
//! whole job. Three characters is safe for Cyrillic as well as Latin text,
//! which BPE tokenizers split far more finely than English.

/// A budget no chunk could ever satisfy. Callers derive the budget from the
/// model's context window, so zero means a misconfigured model rather than a
/// long transcript.
#[derive(Debug, thiserror::Error)]
#[error("budget_tokens must be positive, got {0}")]
pub struct InvalidBudget(pub usize);

/// A conservative token estimate for `text`.
///
/// Counted in characters rather than bytes, matching Python's `len()` on a
/// `str`: a Cyrillic transcript is two bytes per character, and byte-counting
/// would halve every chunk for no reason.
pub fn estimate_tokens(text: &str) -> usize {
    (text.chars().count() / 3).max(1)
}

/// Group `lines` into newline-joined chunks of at most `budget_tokens`.
///
/// Boundaries always fall between lines (transcript segments), never inside
/// one. A single line larger than the whole budget still becomes its own
/// chunk -- the estimate is conservative enough that this only happens for
/// degenerate input, and dropping text would be worse.
pub fn chunk_lines<S: AsRef<str>>(
    lines: &[S],
    budget_tokens: usize,
) -> Result<Vec<String>, InvalidBudget> {
    if budget_tokens == 0 {
        return Err(InvalidBudget(budget_tokens));
    }

    let mut chunks: Vec<String> = Vec::new();
    let mut current: Vec<&str> = Vec::new();
    let mut current_tokens = 0;
    for line in lines {
        let line = line.as_ref();
        let line_tokens = estimate_tokens(line);
        if !current.is_empty() && current_tokens + line_tokens > budget_tokens {
            chunks.push(current.join("\n"));
            current.clear();
            current_tokens = 0;
        }
        current.push(line);
        current_tokens += line_tokens;
    }
    if !current.is_empty() {
        chunks.push(current.join("\n"));
    }
    Ok(chunks)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunking_respects_the_budget_and_never_splits_a_line() {
        let body = "x".repeat(50);
        let lines: Vec<String> = (0..40).map(|i| format!("line {i} {body}")).collect();
        // ~100 tokens is ~300 characters, so about five of these lines.
        let budget = 100;
        let chunks = chunk_lines(&lines, budget).expect("a positive budget");

        assert!(chunks.len() > 1, "40 lines must not fit in one chunk");

        let regrouped: Vec<&str> = chunks.iter().flat_map(|chunk| chunk.lines()).collect();
        let original: Vec<&str> = lines.iter().map(String::as_str).collect();
        assert_eq!(regrouped, original, "every line survives, in order");

        // A chunk may exceed the budget only by the line that would not fit
        // before the boundary was taken -- never by more.
        for chunk in &chunks {
            assert!(estimate_tokens(chunk) <= budget + estimate_tokens(&lines[0]));
        }
    }

    #[test]
    fn a_single_oversized_line_still_becomes_its_own_chunk() {
        let huge = "y".repeat(10_000);
        let chunks = chunk_lines(std::slice::from_ref(&huge), 10).unwrap();
        assert_eq!(chunks, vec![huge]);
    }

    #[test]
    fn a_zero_budget_is_refused_rather_than_looping() {
        assert!(chunk_lines(&["a"], 0).is_err());
    }

    #[test]
    fn no_line_is_ever_estimated_at_zero_tokens() {
        // A run of empty segments must still advance the budget, or a
        // transcript of them would collapse into one unbounded chunk.
        assert_eq!(estimate_tokens(""), 1);
        assert_eq!(estimate_tokens("ab"), 1);
    }

    #[test]
    fn cyrillic_text_is_measured_in_characters_not_bytes() {
        // "да" is two characters but four bytes; byte-counting would double
        // the estimate and halve every chunk.
        assert_eq!(estimate_tokens(&"да".repeat(3)), estimate_tokens("abcdef"));
    }

    #[test]
    fn no_lines_at_all_produce_no_chunks() {
        let empty: [&str; 0] = [];
        assert!(chunk_lines(&empty, 100).unwrap().is_empty());
    }
}
