//! Speaker-turn alignment: attaching diarization output to transcript segments.
//!
//! Port of `services/transcription/src/transcription/diarization.py`.
//!
//! The diarizer answers "who spoke when" as a list of [`SpeakerTurn`]
//! intervals; whisper answers "what was said when" as [`Segment`]s. This
//! module joins the two: a segment goes to the speaker who owns most of its
//! speech time, weighted by word timestamps whenever the segment carries them.
//! Word-level voting matters because the two models disagree about edges --
//! whisper's segment envelope routinely brushes into a neighbouring turn while
//! every actual word stays inside one speaker's, and only the words say where
//! the speech really was.
//!
//! Raw diarizer labels (`SPEAKER_00`, `SPEAKER_01`, ...) are cluster ids in an
//! order no caller can predict, so they are renumbered `Speaker 1`,
//! `Speaker 2`, ... in order of first speech: the first voice heard is always
//! "Speaker 1", which is what a reader scanning the transcript expects and
//! what the desktop UI offers up for renaming.
//!
//! Everything here is pure arithmetic over owned data -- no audio, no model,
//! no filesystem -- which is what keeps the attribution rules testable
//! independently of whether a diarizer can be loaded at all.

use std::collections::{HashMap, HashSet};

use wire::transcript::Segment;

/// A segment (or word) that overlaps no turn at all is still attributed to the
/// nearest turn when its midpoint lies within this many seconds of it.
/// Diarization trims silence harder than whisper does, so a short utterance
/// can land entirely in the gap between two turns and would otherwise lose its
/// speaker for no better reason than a boundary disagreement.
pub const NEAREST_TURN_TOLERANCE_SEC: f64 = 2.0;

/// The prefix on a normalized, not-yet-renamed speaker label.
pub const SPEAKER_LABEL_PREFIX: &str = "Speaker ";

/// A turn whose end precedes its start.
///
/// Refused at construction rather than tolerated: such an interval can only
/// come from a broken diarizer, and overlap voting would score it as zero
/// everywhere, quietly dropping a speaker instead of reporting the fault.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("turn end {end} precedes start {start}")]
pub struct InvalidTurn {
    pub start: f64,
    pub end: f64,
}

/// One diarized interval: `speaker` held the floor from `start` to `end`.
///
/// The fields are read-only because the type carries the invariant checked in
/// [`SpeakerTurn::new`]; the alignment arithmetic below assumes it holds for
/// every turn it is handed.
#[derive(Debug, Clone, PartialEq)]
pub struct SpeakerTurn {
    start: f64,
    end: f64,
    speaker: String,
}

impl SpeakerTurn {
    /// Build a turn, rejecting a reversed interval.
    pub fn new(start: f64, end: f64, speaker: impl Into<String>) -> Result<Self, InvalidTurn> {
        if end < start {
            return Err(InvalidTurn { start, end });
        }
        Ok(SpeakerTurn {
            start,
            end,
            speaker: speaker.into(),
        })
    }

    pub fn start(&self) -> f64 {
        self.start
    }

    pub fn end(&self) -> f64 {
        self.end
    }

    /// The diarizer's own label, before [`normalize_labels`] renumbers it.
    pub fn speaker(&self) -> &str {
        &self.speaker
    }
}

/// Seconds shared by two intervals, clamped at zero for disjoint ones.
fn overlap(a_start: f64, a_end: f64, b_start: f64, b_end: f64) -> f64 {
    (a_end.min(b_end) - a_start.max(b_start)).max(0.0)
}

/// A per-speaker tally of overlapping seconds.
///
/// Kept in first-vote order, not sorted and not hashed, because that order is
/// the tie-break: two speakers with identical overlap resolve to whichever
/// turn was reached first, which makes the outcome reproducible across runs
/// instead of dependent on hash iteration order.
struct Votes<'a>(Vec<(&'a str, f64)>);

impl<'a> Votes<'a> {
    fn new() -> Self {
        Votes(Vec::new())
    }

    fn add(&mut self, speaker: &'a str, seconds: f64) {
        match self.0.iter().position(|(name, _)| *name == speaker) {
            Some(index) => self.0[index].1 += seconds,
            None => self.0.push((speaker, seconds)),
        }
    }

    fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The speaker with the most seconds, earliest voter winning a tie.
    fn winner(&self) -> Option<&'a str> {
        let mut best: Option<(&'a str, f64)> = None;
        for &(speaker, seconds) in &self.0 {
            let improves = match best {
                Some((_, most)) => seconds > most,
                None => true,
            };
            if improves {
                best = Some((speaker, seconds));
            }
        }
        best.map(|(speaker, _)| speaker)
    }
}

/// Add `[start, end]`'s overlap with every turn to the per-speaker tally.
fn vote_interval<'a>(start: f64, end: f64, turns: &'a [SpeakerTurn], votes: &mut Votes<'a>) {
    for turn in turns {
        let shared = overlap(start, end, turn.start, turn.end);
        if shared > 0.0 {
            votes.add(&turn.speaker, shared);
        }
    }
}

/// The speaker of the turn nearest `midpoint`, within the tolerance.
fn nearest_speaker(midpoint: f64, turns: &[SpeakerTurn]) -> Option<&str> {
    let mut best: Option<&str> = None;
    let mut best_distance = NEAREST_TURN_TOLERANCE_SEC;
    for turn in turns {
        if turn.start <= midpoint && midpoint <= turn.end {
            return Some(&turn.speaker);
        }
        let distance = if midpoint < turn.start {
            turn.start - midpoint
        } else {
            midpoint - turn.end
        };
        // Ties go to the later turn, so a segment sitting exactly between two
        // turns joins the speech that follows it rather than the speech it
        // already trails.
        if distance <= best_distance {
            best = Some(&turn.speaker);
            best_distance = distance;
        }
    }
    best
}

fn segment_speaker<'a>(segment: &Segment, turns: &'a [SpeakerTurn]) -> Option<&'a str> {
    let mut votes = Votes::new();
    if let Some(words) = segment.words.as_ref() {
        for word in words {
            vote_interval(word.start, word.end, turns, &mut votes);
        }
    }

    // The envelope is the fallback, not the second opinion: it is consulted
    // only when the words settled nothing, either because there were none or
    // because none of them touched a turn.
    if votes.is_empty() {
        vote_interval(segment.start, segment.end, turns, &mut votes);
        if votes.is_empty() {
            return nearest_speaker((segment.start + segment.end) / 2.0, turns);
        }
    }
    votes.winner()
}

/// Attribute every segment to a speaker from `turns`, by overlap vote.
///
/// The segments are taken by value and handed back labelled, so an unlabelled
/// copy cannot outlive this call and be written to a transcript by mistake.
///
/// A segment no turn can claim -- no overlap anywhere and nothing within
/// [`NEAREST_TURN_TOLERANCE_SEC`] of its midpoint -- keeps `speaker: None`.
/// That is a documented outcome, an honest "don't know" the UI renders as
/// unattributed, never a guess.
pub fn assign_speakers(mut segments: Vec<Segment>, turns: &[SpeakerTurn]) -> Vec<Segment> {
    for segment in &mut segments {
        let speaker = segment_speaker(segment, turns).map(str::to_string);
        segment.speaker = speaker;
    }
    segments
}

/// Renumber raw diarizer labels to `Speaker 1..N` in order of first speech.
///
/// Numbering follows the order the segments are given in, which is why this
/// runs over the whole transcript at once rather than segment by segment: the
/// number a label gets depends on every label before it.
///
/// Segments with no speaker pass through untouched and do not consume a
/// number.
pub fn normalize_labels(mut segments: Vec<Segment>) -> Vec<Segment> {
    let mut assigned: HashMap<String, String> = HashMap::new();
    for segment in &mut segments {
        let raw = match segment.speaker.take() {
            Some(raw) => raw,
            None => continue,
        };
        let next = assigned.len() + 1;
        let label = assigned
            .entry(raw)
            .or_insert_with(|| format!("{SPEAKER_LABEL_PREFIX}{next}"))
            .clone();
        segment.speaker = Some(label);
    }
    segments
}

/// Assign and normalize in one pass, reporting how many distinct speakers were
/// actually attributed.
///
/// The count is of speakers that reached a segment, not of turns the diarizer
/// produced: a cluster that never won a vote is not a speaker anyone can read
/// in the transcript, and reporting it would overstate the result.
pub fn label_segments(segments: Vec<Segment>, turns: &[SpeakerTurn]) -> (Vec<Segment>, usize) {
    let labelled = normalize_labels(assign_speakers(segments, turns));
    let speaker_count = labelled
        .iter()
        .filter_map(|segment| segment.speaker.as_deref())
        .collect::<HashSet<_>>()
        .len();
    (labelled, speaker_count)
}

#[cfg(test)]
mod tests {
    use super::*;

    use wire::transcript::Word;

    fn turn(start: f64, end: f64, speaker: &str) -> SpeakerTurn {
        SpeakerTurn::new(start, end, speaker).expect("valid turn")
    }

    fn seg(id: i64, start: f64, end: f64) -> Segment {
        Segment {
            id,
            start,
            end,
            text: "x".to_string(),
            avg_logprob: None,
            no_speech_prob: None,
            compression_ratio: None,
            words: None,
            speaker: None,
        }
    }

    fn with_words(mut segment: Segment, words: &[(f64, f64)]) -> Segment {
        segment.words = Some(
            words
                .iter()
                .map(|&(start, end)| Word {
                    word: " w".to_string(),
                    start,
                    end,
                    probability: None,
                })
                .collect(),
        );
        segment
    }

    fn with_speaker(mut segment: Segment, speaker: Option<&str>) -> Segment {
        segment.speaker = speaker.map(str::to_string);
        segment
    }

    fn speakers(segments: &[Segment]) -> Vec<Option<&str>> {
        segments
            .iter()
            .map(|segment| segment.speaker.as_deref())
            .collect()
    }

    #[test]
    fn a_turn_that_ends_before_it_starts_is_rejected() {
        let reversed = SpeakerTurn::new(2.0, 1.0, "A");
        assert_eq!(
            reversed,
            Err(InvalidTurn {
                start: 2.0,
                end: 1.0
            })
        );
        assert!(
            SpeakerTurn::new(1.0, 1.0, "A").is_ok(),
            "an instant is fine"
        );
    }

    #[test]
    fn segments_are_attributed_by_temporal_overlap() {
        let turns = [turn(0.0, 5.0, "SPEAKER_00"), turn(5.0, 10.0, "SPEAKER_01")];
        let segments = vec![seg(0, 0.0, 4.0), seg(1, 5.5, 9.0)];

        let labelled = assign_speakers(segments, &turns);

        assert_eq!(
            speakers(&labelled),
            [Some("SPEAKER_00"), Some("SPEAKER_01")]
        );
    }

    #[test]
    fn a_segment_straddling_two_turns_goes_to_the_majority_holder() {
        let turns = [turn(0.0, 3.0, "A"), turn(3.0, 10.0, "B")];
        // One second inside A's turn, four inside B's.
        let labelled = assign_speakers(vec![seg(0, 2.0, 7.0)], &turns);

        assert_eq!(speakers(&labelled), [Some("B")]);
    }

    #[test]
    fn a_segment_is_claimed_by_the_speaker_who_covers_most_of_its_words() {
        // The envelope [0, 10] overlaps B's turn more than A's, but every word
        // lies inside A's -- the words are where the speech is.
        let turns = [turn(0.0, 3.0, "A"), turn(3.0, 10.0, "B")];
        let segment = with_words(seg(0, 0.0, 10.0), &[(0.5, 1.0), (1.2, 1.8), (2.0, 2.8)]);

        let labelled = assign_speakers(vec![segment], &turns);

        assert_eq!(speakers(&labelled), [Some("A")]);
    }

    #[test]
    fn overlapping_turns_split_by_who_holds_more_of_the_segment() {
        // Crosstalk: both turns cover the segment, but B covers more of it.
        let turns = [turn(0.0, 2.0, "A"), turn(1.0, 6.0, "B")];

        let labelled = assign_speakers(vec![seg(0, 0.5, 5.0)], &turns);

        assert_eq!(speakers(&labelled), [Some("B")]);
    }

    #[test]
    fn a_segment_in_a_silence_gap_snaps_to_the_nearest_turn() {
        // Diarization trims silence harder than whisper; a segment falling
        // into the gap still belongs to whoever spoke nearest.
        let turns = [turn(0.0, 4.0, "A")];
        // Midpoint 5.0, one second past A's end.
        let labelled = assign_speakers(vec![seg(0, 4.5, 5.5)], &turns);

        assert_eq!(speakers(&labelled), [Some("A")]);
    }

    #[test]
    fn the_nearest_turn_is_only_reached_within_the_tolerance() {
        let turns = [turn(0.0, 4.0, "A")];
        // Midpoints exactly at the tolerance and just beyond it.
        let at_edge = seg(0, 5.5, 6.5);
        let past_edge = seg(1, 5.6, 6.6);

        let labelled = assign_speakers(vec![at_edge, past_edge], &turns);

        assert_eq!(speakers(&labelled), [Some("A"), None]);
    }

    #[test]
    fn a_segment_far_from_any_turn_stays_unattributed() {
        let turns = [turn(0.0, 1.0, "A")];

        let labelled = assign_speakers(vec![seg(0, 30.0, 31.0)], &turns);

        assert_eq!(speakers(&labelled), [None]);
    }

    #[test]
    fn no_turns_leaves_every_segment_unattributed() {
        let labelled = assign_speakers(vec![seg(0, 0.0, 1.0), seg(1, 1.0, 2.0)], &[]);

        assert_eq!(speakers(&labelled), [None, None]);
    }

    #[test]
    fn labels_are_renumbered_in_order_of_first_speech() {
        // The diarizer's cluster ids arrive in an arbitrary order; "Speaker 1"
        // must be the first voice heard, not the lowest cluster id.
        let segments = vec![
            with_speaker(seg(0, 0.0, 1.0), Some("SPEAKER_01")),
            with_speaker(seg(1, 1.0, 2.0), Some("SPEAKER_00")),
            with_speaker(seg(2, 2.0, 3.0), Some("SPEAKER_01")),
            with_speaker(seg(3, 3.0, 4.0), None),
        ];

        let renamed = normalize_labels(segments);

        assert_eq!(
            speakers(&renamed),
            [
                Some("Speaker 1"),
                Some("Speaker 2"),
                Some("Speaker 1"),
                None
            ]
        );
    }

    #[test]
    fn label_segments_reports_the_distinct_speaker_count() {
        let turns = [turn(0.0, 1.0, "SPEAKER_01"), turn(1.0, 2.0, "SPEAKER_00")];
        let segments = vec![seg(0, 0.0, 0.9), seg(1, 1.1, 1.9), seg(2, 50.0, 51.0)];

        let (labelled, speaker_count) = label_segments(segments, &turns);

        assert_eq!(speaker_count, 2);
        assert_eq!(
            speakers(&labelled),
            [Some("Speaker 1"), Some("Speaker 2"), None]
        );
    }
}
