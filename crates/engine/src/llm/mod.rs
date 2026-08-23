//! The local LLM: derived knowledge from a transcript that already exists.
//!
//! The pure parts land first because they are what the rest is judged
//! against: how a transcript is cut to fit a context window, how a reasoning
//! model's thinking is kept out of the artifacts it writes, and how many
//! layers of a GGUF will fit in the VRAM actually free.

pub mod chunking;
pub mod gguf_meta;
pub mod reasoning;

pub use reasoning::split_reasoning;
