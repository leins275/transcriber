//! The real [`JobRunner`]: the pipeline `jobs.py` ran, in Rust.
//!
//! It owns the engines and decides which to load per job, which is what keeps
//! the "never two large models resident at once" rule in one place. Whisper is
//! loaded on the first transcription and kept for the session, because loading
//! a multi-gigabyte model per job would dominate the runtime of a short one.
//!
//! The pipeline order is the Python service's, and the order matters:
//! decode → transcribe → re-segment → filter → write. Re-segmentation runs
//! before filtering because the filters judge confidence per segment, and a
//! half-minute block spanning several utterances is the wrong unit to judge;
//! ids are assigned last, by the filter pass, so they number the transcript
//! that was actually written.

pub mod export;
pub mod llm_jobs;

use std::path::{Path, PathBuf};

use wire::transcript::{
    DiarizationInfo, DiarizationStatus, ProviderInfo, Source, Stats, TranscriptDoc, SCHEMA_VERSION,
};
use wire::ErrorKind;

use crate::config::Config;
use crate::diarize::{self, DiarizeError, Diarizer, PyannoteDiarizer};
use crate::jobs::{JobContext, JobFailure, JobKind, JobOutcome, JobRequest, JobRunner};
use crate::media::{ffmpeg::FfmpegDecoder, MediaDecoder};
use crate::models;
use crate::stt::{filters, segmentation, TranscribeOptions, WhisperEngine};

/// Runs jobs with the real engines.
pub struct EngineRunner {
    config: Config,
    decoder: Box<dyn MediaDecoder>,
    /// Loaded lazily: a session that never transcribes never pays for the
    /// model, and the app starts long before the first job.
    whisper: Option<WhisperEngine>,
    /// Also lazy, and for a stronger reason: the GGUF is the largest thing
    /// this process ever loads.
    llm: Option<crate::llm::LlamaEngine>,
}

impl EngineRunner {
    pub fn new(config: Config) -> Self {
        let decoder = Box::new(FfmpegDecoder::new(&config.app_dir));
        EngineRunner {
            config,
            decoder,
            whisper: None,
            llm: None,
        }
    }

    /// Substitute the decoder (tests, and anyone reproducing a decode).
    pub fn with_decoder(mut self, decoder: Box<dyn MediaDecoder>) -> Self {
        self.decoder = decoder;
        self
    }

    fn whisper(&mut self) -> Result<&mut WhisperEngine, JobFailure> {
        if self.whisper.is_none() {
            let model = models::whisper_model_file(&self.config);
            if !models::is_installed(&model) {
                // Distinct from "the file is broken": this is what the UI
                // turns into a download offer rather than an error to report.
                return Err(JobFailure::new(
                    ErrorKind::ModelLoad,
                    format!(
                        "the speech model is not installed yet ({})",
                        model.display()
                    ),
                ));
            }
            // Before any model touches ggml: with dynamic backends, an
            // unloaded backend set means zero devices and a hard abort inside
            // whisper rather than an error we could report.
            crate::backends::ensure_loaded(&self.config.app_dir);

            let vad = models::whisper_vad_model_file(&self.config);
            let use_gpu = self.config.device != "cpu";
            self.whisper = Some(WhisperEngine::load(&model, Some(&vad), use_gpu)?);
        }
        Ok(self.whisper.as_mut().expect("just loaded"))
    }

    fn transcribe(&mut self, job: &JobRequest, ctx: &JobContext) -> Result<JobOutcome, JobFailure> {
        let source = PathBuf::from(&job.input_path);
        let output_dir = PathBuf::from(&job.output_dir);
        ctx.check_cancelled()?;

        let started = std::time::Instant::now();
        let pcm = self.decoder.decode_pcm(&source, &ctx.cancel_token())?;
        let duration_sec = pcm.duration_sec();
        ctx.check_cancelled()?;

        let options = TranscribeOptions {
            language: job
                .language
                .clone()
                .or_else(|| self.config.language.clone()),
            word_timestamps: self.config.word_timestamps,
            vad_min_silence_ms: self.config.vad_min_silence_ms,
            threads: None,
        };
        let resegment_gap = self.config.resegment_gap_sec;
        let filter_enabled = self.config.filter_hallucinations;
        let model = self.config.model.clone();

        let engine = self.whisper()?;
        let device = engine.device().to_string();
        let transcription = engine.transcribe(&pcm, &options, ctx)?;

        let segments = segmentation::resegment(transcription.segments, resegment_gap);
        let (segments, filtered) = filters::apply_filters(segments, filter_enabled);

        // Diarization runs last, on the segments a reader will actually see,
        // and it degrades rather than failing: a transcript without speakers
        // is still the transcript. The `diarization` block is where a failure
        // is attributed, so it never degrades silently.
        let (segments, diarization) = self.diarize(segments, &pcm, ctx);

        let elapsed_sec = started.elapsed().as_secs_f64();
        let doc = TranscriptDoc {
            schema_version: SCHEMA_VERSION,
            created_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
            source: Source {
                path: source.display().to_string(),
                filename: source
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default(),
                duration_sec,
            },
            provider: ProviderInfo {
                name: "local".to_string(),
                model,
                device,
                // whisper.cpp reads the quantisation from the model file, so
                // the file *is* the compute type -- there is no separate knob
                // to report the way CTranslate2 had.
                compute_type: "ggml".to_string(),
            },
            language: transcription.language,
            // whisper.cpp reports the detected language but not how sure it
            // was, so this stays null rather than inventing a number.
            language_probability: None,
            text: transcription.text,
            segments,
            stats: Stats {
                elapsed_sec,
                realtime_factor: if duration_sec > 0.0 {
                    elapsed_sec / duration_sec
                } else {
                    0.0
                },
                cost_usd: None,
                currency: None,
            },
            diarization,
        };

        let segment_count = doc.segments.len() as i64;
        write_transcript(&doc, &output_dir)?;
        ctx.set_progress(1.0);

        Ok(JobOutcome {
            audio_duration_sec: Some(duration_sec),
            segment_count: Some(segment_count),
            filtered_segment_count: filtered,
            result_json: None,
            warnings: Vec::new(),
        })
    }
}

impl EngineRunner {
    /// Attribute speakers, if asked to and if it works.
    ///
    /// Returns the segments either way, plus the block the transcript records
    /// about the attempt. Every failure path here is a `status: "failed"`
    /// block rather than an error, because the caller has a complete
    /// transcript in hand and throwing it away over missing speaker labels
    /// would be the worse outcome.
    fn diarize(
        &mut self,
        segments: Vec<wire::transcript::Segment>,
        pcm: &crate::media::Pcm,
        ctx: &JobContext,
    ) -> (Vec<wire::transcript::Segment>, Option<DiarizationInfo>) {
        if !self.config.diarize {
            return (segments, None);
        }

        let mut diarizer = match PyannoteDiarizer::new(&self.config) {
            Ok(diarizer) => diarizer,
            Err(err) => return (segments, Some(failed_diarization(&self.config, err))),
        };

        match diarizer.turns(pcm, ctx) {
            Ok(turns) => {
                let (segments, speaker_count) = diarize::label_segments(segments, &turns);
                (
                    segments,
                    Some(DiarizationInfo {
                        status: DiarizationStatus::Succeeded,
                        model: diarizer.model_name(),
                        device: Some("cpu".to_string()),
                        speaker_count: Some(speaker_count as i64),
                        error_kind: None,
                        error_message: None,
                    }),
                )
            }
            Err(err) => (segments, Some(failed_diarization(&self.config, err))),
        }
    }
}

fn failed_diarization(config: &Config, error: DiarizeError) -> DiarizationInfo {
    let kind = match error {
        DiarizeError::ModelsMissing(_) | DiarizeError::RuntimeLoad { .. } => ErrorKind::ModelLoad,
        DiarizeError::Cancelled => ErrorKind::Cancelled,
        DiarizeError::Failed(_) => ErrorKind::Internal,
    };
    DiarizationInfo {
        status: DiarizationStatus::Failed,
        model: config.diarization_model.clone(),
        device: None,
        speaker_count: None,
        error_kind: Some(kind),
        error_message: Some(error.to_string()),
    }
}

fn write_transcript(doc: &TranscriptDoc, output_dir: &Path) -> Result<(), JobFailure> {
    let json = doc.to_json().map_err(|err| {
        JobFailure::new(
            ErrorKind::Internal,
            format!("could not serialize the transcript: {err}"),
        )
    })?;
    wire::atomic::write_transcript(&output_dir.join(wire::TRANSCRIPT_FILE_NAME), &json).map_err(
        |err| {
            JobFailure::new(
                ErrorKind::Internal,
                format!("could not write the transcript: {err}"),
            )
        },
    )
}

impl EngineRunner {
    fn run_llm_job(
        &mut self,
        job: &JobRequest,
        ctx: &JobContext,
    ) -> Result<JobOutcome, JobFailure> {
        let input = PathBuf::from(&job.input_path);
        let output = PathBuf::from(&job.output_dir);
        let kind = job.kind;

        // Destructured so the engine and the decoder are borrowed as separate
        // fields rather than through `self` twice.
        let EngineRunner {
            config,
            decoder,
            whisper,
            llm,
        } = self;

        let options = crate::llm::CompleteOptions {
            grammar: None,
            max_tokens: config.llm_max_output_tokens,
            temperature: config.llm_temperature,
        };
        let ctx_tokens = config.llm_ctx;
        let keep_loaded = config.llm_keep_loaded;

        if llm.is_none() {
            // The two models must never be resident together: whisper is a few
            // gigabytes and the GGUF is tens, and a machine that can hold
            // either can rarely hold both. Dropping whisper here is what makes
            // the queue's one-job-at-a-time rule actually bound memory.
            *whisper = None;
            *llm = Some(crate::llm::LlamaEngine::load(config)?);
        }

        let mut inputs = llm_jobs::JobInputs {
            llm: llm.as_mut().expect("just loaded"),
            decoder: &**decoder,
            options,
            ctx_tokens,
        };

        let outcome = match kind {
            JobKind::Summarize => llm_jobs::summarize_meeting(&mut inputs, &input, &output, ctx),
            JobKind::ActionItems => llm_jobs::extract_items(
                &mut inputs,
                llm_jobs::ExtractionKind::ActionItems,
                &input,
                &output,
                ctx,
            ),
            JobKind::Facts => llm_jobs::extract_items(
                &mut inputs,
                llm_jobs::ExtractionKind::Facts,
                &input,
                &output,
                ctx,
            ),
            JobKind::Report => llm_jobs::write_report(&mut inputs, &input, &output, ctx),
            JobKind::Transcribe | JobKind::Export => unreachable!("handled by the caller"),
        };

        // Releasing the model between jobs is the default: its working set is
        // the largest thing this process holds, and reloading is mmap-fast.
        if !keep_loaded {
            self.llm = None;
        }
        outcome
    }
}

impl JobRunner for EngineRunner {
    fn run(&mut self, job: &JobRequest, ctx: &JobContext) -> Result<JobOutcome, JobFailure> {
        match job.kind {
            JobKind::Transcribe => self.transcribe(job, ctx),
            JobKind::Summarize | JobKind::ActionItems | JobKind::Facts | JobKind::Report => {
                self.run_llm_job(job, ctx)
            }
            // Deterministic assembly with no model behind it, so it needs
            // neither engine loaded.
            JobKind::Export => {
                export::export_meeting(Path::new(&job.input_path), Path::new(&job.output_dir), ctx)
            }
        }
    }

    fn device(&self) -> String {
        self.whisper
            .as_ref()
            .map(|engine| engine.device().to_string())
            .unwrap_or_else(|| self.config.device.clone())
    }
}
