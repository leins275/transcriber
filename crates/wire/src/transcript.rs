//! The `transcript.json` v1 document (FR-6).
//!
//! This is a port of `services/transcription/src/transcription/schema.py`,
//! and the port is deliberately literal: every field is declared in the same
//! order pydantic declares it, because serde -- like pydantic -- serializes in
//! declaration order, and the bytes of this file are a contract with vaults
//! that already exist on disk.
//!
//! Two rules follow from that contract and are easy to break by accident:
//!
//! 1. **Only `words`, `speaker` and `diarization` are omitted when absent.**
//!    Python drops exactly those three via `@model_serializer` so a transcript
//!    written with the features off stays byte-identical to one written before
//!    they existed. Every other optional field -- `language`, the three
//!    confidence numbers, `cost_usd`, `currency` -- is written as an explicit
//!    `null`. `cost_usd`/`currency` in particular are *format*, not surviving
//!    cloud logic: nothing local ever sets them, and they still have to be
//!    there.
//! 2. **Reading is permissive.** Unknown keys are ignored so a document from a
//!    newer writer still parses, and confidence fields stay `Option` so a
//!    document from a provider that never populated them still parses.

use serde::{Deserialize, Serialize};

use crate::error_kind::ErrorKind;

/// One transcript segment, in vexa's `verbose_json` mapper shape (FR-6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Segment {
    pub id: i64,
    pub start: f64,
    pub end: f64,
    pub text: String,
    /// The three confidence fields are optional because a provider may
    /// legitimately not report them -- they must never be fabricated. They are
    /// written as `null` rather than omitted.
    pub avg_logprob: Option<f64>,
    pub no_speech_prob: Option<f64>,
    pub compression_ratio: Option<f64>,
    /// Omitted (not nulled) when word timestamps were off.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub words: Option<Vec<Word>>,
    /// Set by the diarization pass; omitted both when diarization was off and
    /// when no turn could claim this segment.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub speaker: Option<String>,
}

/// One word timestamp inside a [`Segment`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Word {
    pub word: String,
    pub start: f64,
    pub end: f64,
    pub probability: Option<f64>,
}

/// Where the audio came from.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Source {
    pub path: String,
    pub filename: String,
    pub duration_sec: f64,
}

/// Which engine produced the transcript.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProviderInfo {
    pub name: String,
    pub model: String,
    pub device: String,
    pub compute_type: String,
}

/// Timing of the run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Stats {
    pub elapsed_sec: f64,
    pub realtime_factor: f64,
    /// Always `null` now that nothing bills per minute, and still written:
    /// removing the key would change the bytes of every new transcript
    /// relative to every old one.
    pub cost_usd: Option<f64>,
    pub currency: Option<String>,
}

/// Whether the diarization pass ran, and how it went.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DiarizationStatus {
    Succeeded,
    Failed,
}

/// What the transcript records about its diarization pass.
///
/// `status: "failed"` is a real, documented state: diarization degrades rather
/// than failing the whole job -- the transcript is still valuable without
/// speakers -- and this block is where the failure is attributed so it never
/// degrades *silently*.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DiarizationInfo {
    pub status: DiarizationStatus,
    pub model: String,
    pub device: Option<String>,
    pub speaker_count: Option<i64>,
    pub error_kind: Option<ErrorKind>,
    pub error_message: Option<String>,
}

/// The `transcript.json` v1 document.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TranscriptDoc {
    pub schema_version: u8,
    pub created_at: String,
    pub source: Source,
    pub provider: ProviderInfo,
    pub language: Option<String>,
    pub language_probability: Option<f64>,
    pub text: String,
    pub segments: Vec<Segment>,
    pub stats: Stats,
    /// Present only when diarization ran (succeeded or failed); omitted
    /// otherwise, keeping pre-feature readers' documents unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diarization: Option<DiarizationInfo>,
}

/// The only `schema_version` this crate writes.
pub const SCHEMA_VERSION: u8 = 1;

/// The file name a transcript is stored under, inside a meeting folder.
pub const TRANSCRIPT_FILE_NAME: &str = vault::paths::TRANSCRIPT_FILE_NAME;

impl TranscriptDoc {
    /// Serialize exactly as the Python service does (see [`crate::python_json`]).
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        crate::python_json::to_string(self)
    }

    /// Parse a `transcript.json`, tolerating documents from other writers.
    pub fn from_json(text: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_doc() -> TranscriptDoc {
        TranscriptDoc {
            schema_version: SCHEMA_VERSION,
            created_at: "2026-08-23T18:00:00+00:00".to_string(),
            source: Source {
                path: "C:\\vault\\ELS\\260812 - Demo\\source.mp4".to_string(),
                filename: "source.mp4".to_string(),
                duration_sec: 12.5,
            },
            provider: ProviderInfo {
                name: "local".to_string(),
                model: "large-v3".to_string(),
                device: "cuda".to_string(),
                compute_type: "float16".to_string(),
            },
            language: Some("ru".to_string()),
            language_probability: Some(0.98),
            text: "Привет".to_string(),
            segments: vec![Segment {
                id: 0,
                start: 0.0,
                end: 12.5,
                text: "Привет".to_string(),
                avg_logprob: Some(-0.25),
                no_speech_prob: Some(0.01),
                compression_ratio: Some(1.5),
                words: None,
                speaker: None,
            }],
            stats: Stats {
                elapsed_sec: 3.0,
                realtime_factor: 4.0,
                cost_usd: None,
                currency: None,
            },
            diarization: None,
        }
    }

    #[test]
    fn absent_optional_blocks_are_omitted_not_nulled() {
        let json = minimal_doc().to_json().unwrap();
        assert!(!json.contains("\"words\""), "words must be omitted: {json}");
        assert!(
            !json.contains("\"speaker\""),
            "speaker must be omitted: {json}"
        );
        assert!(
            !json.contains("\"diarization\""),
            "diarization must be omitted: {json}"
        );
    }

    #[test]
    fn nullable_fields_are_written_as_explicit_nulls() {
        // The other half of the rule: these keys must survive as `null`, or
        // every new transcript differs from every old one.
        let json = minimal_doc().to_json().unwrap();
        assert!(json.contains(r#""cost_usd": null"#), "{json}");
        assert!(json.contains(r#""currency": null"#), "{json}");
    }

    #[test]
    fn field_order_matches_pydantic_declaration_order() {
        let json = minimal_doc().to_json().unwrap();
        let order = [
            "schema_version",
            "created_at",
            "source",
            "provider",
            "language",
            "language_probability",
            "text",
            "segments",
            "stats",
        ];
        let mut cursor = 0;
        for key in order {
            let needle = format!("\"{key}\"");
            let at = json[cursor..]
                .find(&needle)
                .unwrap_or_else(|| panic!("{key} missing or out of order in {json}"));
            cursor += at + needle.len();
        }
    }

    #[test]
    fn unknown_keys_from_a_newer_writer_are_ignored() {
        let mut value: serde_json::Value = serde_json::from_str(&minimal_doc().to_json().unwrap())
            .expect("doc parses as generic json");
        value["some_future_field"] = serde_json::json!(42);
        value["segments"][0]["some_future_segment_field"] = serde_json::json!("x");

        let parsed = TranscriptDoc::from_json(&value.to_string()).expect("tolerates unknown keys");
        assert_eq!(parsed.segments.len(), 1);
    }

    #[test]
    fn round_trip_is_byte_stable() {
        let doc = minimal_doc();
        let once = doc.to_json().unwrap();
        let twice = TranscriptDoc::from_json(&once).unwrap().to_json().unwrap();
        assert_eq!(once, twice);
    }

    #[test]
    fn diarization_block_round_trips() {
        let mut doc = minimal_doc();
        doc.diarization = Some(DiarizationInfo {
            status: DiarizationStatus::Failed,
            model: "pyannote/speaker-diarization-3.1".to_string(),
            device: None,
            speaker_count: None,
            error_kind: Some(ErrorKind::ModelLoad),
            error_message: Some("model missing".to_string()),
        });
        let json = doc.to_json().unwrap();
        assert!(json.contains(r#""status": "failed""#), "{json}");
        assert!(json.contains(r#""error_kind": "model_load""#), "{json}");
        assert_eq!(TranscriptDoc::from_json(&json).unwrap(), doc);
    }
}
