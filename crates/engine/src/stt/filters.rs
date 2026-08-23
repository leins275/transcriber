// Adapted from Vexa (Vexa-ai/vexa), Apache-2.0. Origin:
// core/meetings/services/transcription/src/transcription/main.py:99-118 and
// core/meetings/modules/whisper/src/confidence.ts:8-13
//
//! Silence and hallucination filtering heuristics (FR-12).
//!
//! Whisper will confidently transcribe silence, usually as a stock phrase it
//! saw often in training, and it will loop a phrase when it loses the thread.
//! Both are recognisable from the numbers the decoder already reports, which
//! is what these thresholds test. Port of
//! `services/transcription/src/transcription/filters.py`.
//!
//! One thing changed underneath: `compression_ratio` used to come from
//! faster-whisper. whisper.cpp does not report it, so [`compression_ratio`]
//! computes it here, with the same definition -- the deflate ratio of the
//! segment text, which is high exactly when the text repeats itself.

use std::io::Write;

use wire::transcript::Segment;

/// Above this, the decoder thinks the audio is probably not speech.
pub const NO_SPEECH_THRESHOLD: f64 = 0.6;
/// Below this *and* not-speech, the text is not worth keeping.
pub const LOG_PROB_THRESHOLD: f64 = -1.0;
/// Below this, the text is not worth keeping whatever the speech probability
/// says.
pub const LOG_PROB_HARD_THRESHOLD: f64 = -1.3;
/// Above this, the text compresses too well to be natural speech -- the
/// signature of a decoder stuck in a loop.
pub const COMPRESSION_RATIO_THRESHOLD: f64 = 2.4;

/// The deflate ratio of `text`: uncompressed length over compressed length.
///
/// Text that repeats itself compresses far better than speech does, which is
/// what makes this a hallucination signal rather than a size measurement.
/// Returns `None` for empty text, where the ratio has no meaning.
pub fn compression_ratio(text: &str) -> Option<f64> {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return None;
    }
    let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
    encoder.write_all(bytes).ok()?;
    let compressed = encoder.finish().ok()?;
    if compressed.is_empty() {
        return None;
    }
    Some(bytes.len() as f64 / compressed.len() as f64)
}

/// Whether one segment looks like silence or a hallucination.
pub fn is_low_confidence(segment: &Segment) -> bool {
    if looks_like_silence_segment(segment) {
        return true;
    }
    if segment
        .compression_ratio
        .is_some_and(|ratio| ratio > COMPRESSION_RATIO_THRESHOLD)
    {
        return true;
    }
    segment
        .avg_logprob
        .is_some_and(|logprob| logprob < LOG_PROB_HARD_THRESHOLD)
}

fn looks_like_silence_segment(segment: &Segment) -> bool {
    match (segment.no_speech_prob, segment.avg_logprob) {
        (Some(no_speech), Some(logprob)) => {
            no_speech > NO_SPEECH_THRESHOLD && logprob < LOG_PROB_THRESHOLD
        }
        _ => false,
    }
}

/// Whether every segment is silence-shaped -- which is how a recording with no
/// speech in it presents, rather than as an empty transcript.
///
/// An empty list counts as silence: there is nothing in it that is speech.
pub fn looks_like_silence(segments: &[Segment]) -> bool {
    segments.iter().all(looks_like_silence_segment)
}

/// Drop low-confidence segments and renumber the survivors, returning how many
/// were dropped.
///
/// With filtering off the input passes through untouched and the count is
/// zero: the setting is live, so a user who turns it off gets exactly what the
/// decoder produced.
pub fn apply_filters(segments: Vec<Segment>, enabled: bool) -> (Vec<Segment>, i64) {
    if !enabled {
        return (segments, 0);
    }

    let before = segments.len();
    let mut kept: Vec<Segment> = segments
        .into_iter()
        .filter(|segment| !is_low_confidence(segment))
        .collect();
    let filtered = (before - kept.len()) as i64;

    // Ids are positions in the final transcript, so they are assigned after
    // the drops rather than carried through them.
    for (index, segment) in kept.iter_mut().enumerate() {
        segment.id = index as i64;
    }
    (kept, filtered)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn segment(avg_logprob: f64, no_speech_prob: f64, compression_ratio: f64) -> Segment {
        Segment {
            id: 0,
            start: 0.0,
            end: 1.0,
            text: "hello".to_string(),
            avg_logprob: Some(avg_logprob),
            no_speech_prob: Some(no_speech_prob),
            compression_ratio: Some(compression_ratio),
            words: None,
            speaker: None,
        }
    }

    fn good() -> Segment {
        segment(-0.2, 0.1, 1.5)
    }

    #[test]
    fn a_confident_segment_survives() {
        assert!(!is_low_confidence(&good()));
    }

    #[test]
    fn silence_needs_both_signals_to_agree() {
        // Either signal alone is not enough: quiet-but-confident audio is real
        // speech, and an unsure decoder on obvious speech is still speech.
        assert!(is_low_confidence(&segment(-1.1, 0.7, 1.5)));
        assert!(!is_low_confidence(&segment(-0.2, 0.7, 1.5)));
        assert!(!is_low_confidence(&segment(-1.1, 0.1, 1.5)));
    }

    #[test]
    fn text_that_compresses_too_well_is_a_loop() {
        assert!(is_low_confidence(&segment(-0.2, 0.1, 2.5)));
    }

    #[test]
    fn a_hard_low_logprob_fails_on_its_own() {
        assert!(is_low_confidence(&segment(-1.4, 0.1, 1.5)));
    }

    #[test]
    fn missing_confidence_numbers_never_drop_a_segment() {
        // A provider that does not report these must not have its output
        // thrown away; absent is not the same as bad.
        let mut bare = good();
        bare.avg_logprob = None;
        bare.no_speech_prob = None;
        bare.compression_ratio = None;
        assert!(!is_low_confidence(&bare));
    }

    #[test]
    fn an_empty_transcript_counts_as_silence() {
        assert!(looks_like_silence(&[]));
    }

    #[test]
    fn silence_detection_needs_every_segment_to_be_silent() {
        let silent = segment(-1.1, 0.7, 1.5);
        assert!(looks_like_silence(&[silent.clone(), silent.clone()]));
        assert!(!looks_like_silence(&[silent, good()]));
    }

    #[test]
    fn filtering_renumbers_the_survivors() {
        let segments = vec![good(), segment(-1.4, 0.1, 1.5), good()];
        let (kept, filtered) = apply_filters(segments, true);
        assert_eq!(filtered, 1);
        assert_eq!(kept.len(), 2);
        assert_eq!(kept.iter().map(|s| s.id).collect::<Vec<_>>(), vec![0, 1]);
    }

    #[test]
    fn filtering_off_changes_nothing() {
        let segments = vec![good(), segment(-1.4, 0.1, 1.5)];
        let (kept, filtered) = apply_filters(segments.clone(), false);
        assert_eq!(filtered, 0);
        assert_eq!(kept, segments, "the toggle is live, not advisory");
    }

    #[test]
    fn repeated_text_compresses_far_better_than_speech() {
        // The property the threshold rests on, checked rather than assumed.
        let looped = "yeah ".repeat(60);
        let speech = "The quarterly review covers three separate proposals.";
        assert!(
            compression_ratio(&looped).unwrap() > COMPRESSION_RATIO_THRESHOLD,
            "a decoder loop should trip the threshold"
        );
        assert!(
            compression_ratio(speech).unwrap() < COMPRESSION_RATIO_THRESHOLD,
            "ordinary speech should not"
        );
    }

    #[test]
    fn empty_text_has_no_compression_ratio() {
        assert_eq!(compression_ratio(""), None);
    }
}
