//! Utterance-level re-segmentation from word timestamps.
//!
//! Whisper emits segments of up to ~30 seconds that freely span several
//! speakers' utterances. The desktop UI groups segments into turns by the
//! pause *between* segments, so a multi-utterance segment can never be split
//! downstream and reads as several speakers' lines run together. This pass
//! cuts each segment at sentence-ending punctuation and at word-level pauses,
//! giving the turn grouping the granularity it needs -- and giving the
//! diarization vote something finer than a half-minute block to attribute.
//!
//! Port of `services/transcription/src/transcription/segmentation.py`.

use wire::transcript::Segment;

/// Punctuation that ends an utterance. The ellipsis is included because
/// Whisper uses it for trailing-off speech, which is a boundary too.
pub const SENTENCE_ENDINGS: [char; 4] = ['.', '!', '?', '…'];

/// A pause at least this long starts a new utterance.
pub const DEFAULT_GAP_SEC: f64 = 0.6;

/// Split segments into utterance-sized pieces and renumber ids.
///
/// A segment is cut after a word ending in sentence punctuation, and before a
/// word that starts at least `gap_sec` after the previous one ended. A segment
/// without word timestamps passes through untouched -- there is nothing to cut
/// on, and inventing boundaries would be worse than leaving it whole.
pub fn resegment(segments: Vec<Segment>, gap_sec: f64) -> Vec<Segment> {
    let mut out: Vec<Segment> = segments
        .into_iter()
        .flat_map(|segment| split_segment(segment, gap_sec))
        .collect();
    for (index, segment) in out.iter_mut().enumerate() {
        segment.id = index as i64;
    }
    out
}

fn split_segment(segment: Segment, gap_sec: f64) -> Vec<Segment> {
    let Some(words) = segment.words.as_ref() else {
        return vec![segment];
    };
    if words.len() < 2 {
        return vec![segment];
    }

    let mut pieces: Vec<Vec<usize>> = Vec::new();
    let mut current: Vec<usize> = Vec::new();
    for index in 0..words.len() {
        current.push(index);
        let Some(following) = words.get(index + 1) else {
            break;
        };
        let word = &words[index];
        let ends_sentence = word
            .word
            .trim_end()
            .ends_with(|c| SENTENCE_ENDINGS.contains(&c));
        let long_pause = following.start - word.end >= gap_sec;
        if ends_sentence || long_pause {
            pieces.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        pieces.push(current);
    }

    if pieces.len() <= 1 {
        // One piece means the whole segment. Return the original rather than a
        // reconstruction: word timestamps can sit slightly inside the
        // segment's own start and end, and its text is authoritative.
        return vec![segment];
    }

    pieces
        .into_iter()
        .map(|piece| {
            let piece_words: Vec<_> = piece.iter().map(|i| words[*i].clone()).collect();
            Segment {
                id: segment.id,
                start: piece_words
                    .first()
                    .map(|w| w.start)
                    .unwrap_or(segment.start),
                end: piece_words.last().map(|w| w.end).unwrap_or(segment.end),
                // Word texts carry their own leading space, so concatenating
                // them reproduces the original spacing exactly.
                text: piece_words.iter().map(|w| w.word.as_str()).collect(),
                // The confidence numbers were measured on the parent's audio
                // and stay attached to the text they describe, so the filters
                // still have something to judge each child on.
                avg_logprob: segment.avg_logprob,
                no_speech_prob: segment.no_speech_prob,
                compression_ratio: segment.compression_ratio,
                words: Some(piece_words),
                speaker: segment.speaker.clone(),
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use wire::transcript::Word;

    fn word(text: &str, start: f64, end: f64) -> Word {
        Word {
            word: text.to_string(),
            start,
            end,
            probability: Some(0.9),
        }
    }

    fn segment_with(words: Vec<Word>) -> Segment {
        let text: String = words.iter().map(|w| w.word.as_str()).collect();
        let start = words.first().map(|w| w.start).unwrap_or(0.0);
        let end = words.last().map(|w| w.end).unwrap_or(0.0);
        Segment {
            id: 7,
            start,
            end,
            text,
            avg_logprob: Some(-0.3),
            no_speech_prob: Some(0.05),
            compression_ratio: Some(1.4),
            words: Some(words),
            speaker: None,
        }
    }

    #[test]
    fn a_sentence_ending_cuts_the_segment() {
        let segment = segment_with(vec![
            word("Hello", 0.0, 0.4),
            word(" there.", 0.4, 0.9),
            word(" Next", 1.0, 1.4),
            word(" one", 1.4, 1.8),
        ]);
        let out = resegment(vec![segment], DEFAULT_GAP_SEC);

        assert_eq!(out.len(), 2);
        assert_eq!(out[0].text, "Hello there.");
        assert_eq!(out[1].text, " Next one");
        assert_eq!(out[0].end, 0.9);
        assert_eq!(out[1].start, 1.0);
    }

    #[test]
    fn a_long_pause_cuts_the_segment_without_punctuation() {
        let segment = segment_with(vec![
            word("so", 0.0, 0.4),
            word(" anyway", 0.4, 0.9),
            // A full second of silence.
            word(" right", 1.9, 2.3),
        ]);
        let out = resegment(vec![segment], DEFAULT_GAP_SEC);

        assert_eq!(out.len(), 2);
        assert_eq!(out[1].text, " right");
    }

    #[test]
    fn a_pause_shorter_than_the_gap_does_not_cut() {
        let segment = segment_with(vec![
            word("so", 0.0, 0.4),
            word(" anyway", 0.8, 1.2),
            word(" right", 1.5, 1.9),
        ]);
        assert_eq!(resegment(vec![segment], DEFAULT_GAP_SEC).len(), 1);
    }

    #[test]
    fn an_unsplit_segment_is_returned_verbatim() {
        // Word timestamps can sit inside the segment's own bounds, so a
        // reconstruction would silently narrow it.
        let mut segment = segment_with(vec![word("Hello", 0.1, 0.4), word(" there", 0.4, 0.9)]);
        segment.start = 0.0;
        segment.end = 1.0;
        let out = resegment(vec![segment.clone()], DEFAULT_GAP_SEC);

        assert_eq!(out.len(), 1);
        assert_eq!(out[0].start, 0.0);
        assert_eq!(out[0].end, 1.0);
        assert_eq!(out[0].text, segment.text);
    }

    #[test]
    fn a_segment_without_word_timestamps_passes_through() {
        let segment = Segment {
            id: 3,
            start: 0.0,
            end: 30.0,
            text: "One long block. With two sentences.".to_string(),
            avg_logprob: Some(-0.3),
            no_speech_prob: Some(0.05),
            compression_ratio: Some(1.4),
            words: None,
            speaker: None,
        };
        let out = resegment(vec![segment], DEFAULT_GAP_SEC);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].text, "One long block. With two sentences.");
    }

    #[test]
    fn children_inherit_the_confidence_numbers() {
        // The filters run after this pass, so every child has to carry
        // something for them to judge.
        let segment = segment_with(vec![
            word("One.", 0.0, 0.4),
            word(" Two.", 0.4, 0.9),
            word(" Three", 0.9, 1.4),
        ]);
        let out = resegment(vec![segment], DEFAULT_GAP_SEC);
        assert!(out.len() > 1);
        for child in &out {
            assert_eq!(child.avg_logprob, Some(-0.3));
            assert_eq!(child.compression_ratio, Some(1.4));
        }
    }

    #[test]
    fn ids_are_renumbered_across_every_segment() {
        let first = segment_with(vec![word("One.", 0.0, 0.4), word(" Two", 0.4, 0.9)]);
        let second = segment_with(vec![word("Three.", 2.0, 2.4), word(" Four", 2.4, 2.9)]);
        let out = resegment(vec![first, second], DEFAULT_GAP_SEC);
        assert_eq!(
            out.iter().map(|s| s.id).collect::<Vec<_>>(),
            (0..out.len() as i64).collect::<Vec<_>>()
        );
    }

    #[test]
    fn an_ellipsis_ends_an_utterance() {
        let segment = segment_with(vec![word("well…", 0.0, 0.4), word(" anyway", 0.4, 0.9)]);
        assert_eq!(resegment(vec![segment], DEFAULT_GAP_SEC).len(), 2);
    }
}
