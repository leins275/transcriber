//! Speaker diarization: who spoke when, and which words were theirs.
//!
//! Split in two on purpose. [`align`] is pure logic -- turns in, labelled
//! segments out -- and it is where everything a reader sees in a transcript is
//! decided: which speaker claims a segment, and what the speakers are called.
//! The model that produces the turns sits behind its own seam, so the part
//! that shapes the transcript stays testable without one.

pub mod align;

pub use align::{label_segments, SpeakerTurn};
