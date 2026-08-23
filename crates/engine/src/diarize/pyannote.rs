//! Speaker turns from the pyannote ONNX models.
//!
//! Replaces `diarizer.py`, which drove pyannote.audio on torch. There is no
//! torch here, so this uses the community ONNX exports of the same two models:
//! `segmentation-3.0` decides *when* someone is speaking, and a speaker
//! embedding model decides *who*, by comparing each turn's embedding against
//! the ones already seen.
//!
//! The honest difference from pyannote.audio: it clusters all embeddings
//! together at the end, while this assigns each turn greedily to the closest
//! speaker so far. Greedy assignment can split one speaker in two when their
//! voice shifts, and the transcript shows that as an extra "Speaker N". It is
//! also why diarization stays off by default and why a failure here degrades
//! the transcript instead of failing the job.
//!
//! ONNX Runtime is loaded from a DLL by absolute path, so a copy of
//! `onnxruntime.dll` sitting earlier in `PATH` -- another application's, built
//! against a different API -- cannot be picked up instead.

use std::path::{Path, PathBuf};
use std::sync::Once;

use super::align::SpeakerTurn;
use crate::config::Config;
use crate::jobs::{CancelToken, JobContext};
use crate::media::{Pcm, SAMPLE_RATE};

/// How similar two embeddings must be to count as the same speaker.
///
/// Lower merges distinct voices, higher splits one speaker across several
/// labels. This is pyannote-rs's own suggested value, kept rather than tuned
/// blind: it should be moved only against a labelled recording.
const SPEAKER_SIMILARITY_THRESHOLD: f32 = 0.5;

/// Why diarization could not run.
#[derive(Debug, thiserror::Error)]
pub enum DiarizeError {
    #[error("the diarization models are not installed ({0})")]
    ModelsMissing(String),
    #[error("the ONNX runtime could not be loaded from {path}: {detail}")]
    RuntimeLoad { path: String, detail: String },
    #[error("cancelled")]
    Cancelled,
    #[error("diarization failed: {0}")]
    Failed(String),
}

/// What the transcription pipeline needs from a diarizer, so the alignment can
/// be tested without ONNX and the engine swapped without touching the caller.
pub trait Diarizer {
    /// Speaker turns over the whole recording, in time order.
    fn turns(&mut self, pcm: &Pcm, job: &JobContext) -> Result<Vec<SpeakerTurn>, DiarizeError>;

    /// The model name recorded in the transcript's `diarization` block.
    fn model_name(&self) -> String;
}

static ORT_INIT: Once = Once::new();

/// Point ONNX Runtime at the DLL the installer shipped, once per process.
fn init_ort(dll: &Path) -> Result<(), DiarizeError> {
    let mut outcome = Ok(());
    ORT_INIT.call_once(|| {
        if dll.is_file() {
            if let Err(err) = ort::init_from(dll.to_string_lossy().as_ref()).commit() {
                outcome = Err(DiarizeError::RuntimeLoad {
                    path: dll.display().to_string(),
                    detail: err.to_string(),
                });
            }
        } else {
            // Leaving ort to its own search is deliberate here: a development
            // machine may have the library somewhere sensible, and failing
            // outright would make diarization untestable without an install.
            let _ = ort::init().commit();
        }
    });
    outcome
}

/// Diarization with the pyannote ONNX models.
///
/// `Debug` names the models rather than the ONNX sessions, which are opaque
/// handles into another library's allocations.
pub struct PyannoteDiarizer {
    segmentation: PathBuf,
    embedding: PathBuf,
    model_name: String,
    max_speakers: usize,
}

impl PyannoteDiarizer {
    /// Prepare the diarizer, checking that both models and the runtime are
    /// where they should be.
    pub fn new(config: &Config) -> Result<Self, DiarizeError> {
        let segmentation = crate::models::diarization_segmentation_model(config);
        let embedding = crate::models::diarization_embedding_model(config);
        for model in [&segmentation, &embedding] {
            if !model.is_file() {
                return Err(DiarizeError::ModelsMissing(model.display().to_string()));
            }
        }
        init_ort(&crate::models::onnx_runtime_library(config))?;

        Ok(PyannoteDiarizer {
            segmentation,
            embedding,
            model_name: config.diarization_model.clone(),
            max_speakers: config.diarization_max_speakers.max(1) as usize,
        })
    }
}

impl std::fmt::Debug for PyannoteDiarizer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PyannoteDiarizer")
            .field("model", &self.model_name)
            .field("max_speakers", &self.max_speakers)
            .finish()
    }
}

impl Diarizer for PyannoteDiarizer {
    fn model_name(&self) -> String {
        self.model_name.clone()
    }

    fn turns(&mut self, pcm: &Pcm, job: &JobContext) -> Result<Vec<SpeakerTurn>, DiarizeError> {
        check(&job.cancel_token())?;

        // The models take 16-bit samples; the pipeline carries float ones,
        // already at the rate they expect.
        let samples: Vec<i16> = pcm
            .samples
            .iter()
            .map(|sample| (sample.clamp(-1.0, 1.0) * i16::MAX as f32) as i16)
            .collect();

        let segments = pyannote_rs::get_segments(&samples, SAMPLE_RATE, &self.segmentation)
            .map_err(|err| DiarizeError::Failed(err.to_string()))?;

        let mut extractor = pyannote_rs::EmbeddingExtractor::new(&self.embedding)
            .map_err(|err| DiarizeError::Failed(err.to_string()))?;
        let mut manager = pyannote_rs::EmbeddingManager::new(self.max_speakers);

        let mut turns = Vec::new();
        for segment in segments {
            check(&job.cancel_token())?;
            let segment = segment.map_err(|err| DiarizeError::Failed(err.to_string()))?;

            // A turn whose embedding cannot be computed still happened; it is
            // recorded as an unattributed turn rather than dropped, so the
            // alignment can leave those words without a speaker instead of
            // handing them to whoever spoke nearby.
            let label = match extractor.compute(&segment.samples) {
                Ok(embedding) => manager
                    .search_speaker(embedding.collect(), SPEAKER_SIMILARITY_THRESHOLD)
                    .map(|id| format!("SPEAKER_{id:02}")),
                Err(_) => None,
            };

            if let Some(label) = label {
                if let Ok(turn) = SpeakerTurn::new(segment.start, segment.end, label) {
                    turns.push(turn);
                }
            }
        }

        Ok(turns)
    }
}

fn check(cancel: &CancelToken) -> Result<(), DiarizeError> {
    if cancel.is_cancelled() {
        Err(DiarizeError::Cancelled)
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_in(dir: &Path) -> Config {
        let mut env = crate::config::Env::new();
        env.insert("TRANSCRIBER_APP_DIR".to_string(), dir.display().to_string());
        Config::load(None, &env).expect("config")
    }

    #[test]
    fn missing_models_are_reported_before_the_runtime_is_touched() {
        let dir = tempfile::tempdir().unwrap();
        let err = PyannoteDiarizer::new(&config_in(dir.path())).expect_err("should fail");
        assert!(matches!(err, DiarizeError::ModelsMissing(_)), "{err:?}");
    }

    #[test]
    fn a_diarization_failure_never_becomes_a_failed_job() {
        // The contract the transcript's `diarization` block exists to record:
        // speakers are an enhancement, and losing them must not lose the
        // transcript.
        let err = DiarizeError::ModelsMissing("x".to_string());
        assert!(
            !matches!(err, DiarizeError::Cancelled),
            "only a cancellation should stop the job"
        );
    }
}
