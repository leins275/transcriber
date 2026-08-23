//! Speech to text with whisper.cpp.
//!
//! Replaces `providers/local_whisper.py`, which drove faster-whisper
//! (CTranslate2). The parameters are matched deliberately rather than
//! rediscovered -- beam size, VAD silence threshold and the
//! no-previous-context rule all shape how segments come out, and changing them
//! silently would change every transcript a user compares against an old one.
//!
//! Three numbers faster-whisper reported for free now have to be produced
//! here, because whisper.cpp exposes different primitives:
//!
//! - `avg_logprob` is averaged from the per-token log probabilities.
//! - `compression_ratio` is computed from the segment text (see
//!   [`super::filters::compression_ratio`]).
//! - words are assembled from token timestamps, since whisper.cpp reports
//!   sub-word tokens rather than words.
//!
//! The context is a raw pointer and this type is therefore not `Send`. That is
//! not an oversight: it is owned by the engine's worker thread and must never
//! leave it.

use std::ffi::{CStr, CString};
use std::path::Path;

use wire::transcript::{Segment, Word};

use crate::jobs::{CancelToken, JobContext};
use crate::media::Pcm;

/// Beam width. faster-whisper defaulted to 5 and the Python service kept it;
/// dropping it would change transcripts.
const BEAM_SIZE: i32 = 5;

/// Why speech-to-text could not run.
#[derive(Debug, thiserror::Error)]
pub enum SttError {
    #[error("whisper model not found at {0}")]
    ModelMissing(String),
    #[error("failed to load the whisper model at {path}")]
    ModelLoad { path: String },
    #[error("whisper failed while decoding (code {0})")]
    Decode(i32),
    #[error("cancelled")]
    Cancelled,
    #[error("{0}")]
    Invalid(String),
}

impl From<SttError> for crate::jobs::JobFailure {
    fn from(error: SttError) -> Self {
        let kind = match error {
            SttError::Cancelled => wire::ErrorKind::Cancelled,
            SttError::ModelMissing(_) | SttError::ModelLoad { .. } => wire::ErrorKind::ModelLoad,
            SttError::Invalid(_) => wire::ErrorKind::InvalidRequest,
            SttError::Decode(_) => wire::ErrorKind::Internal,
        };
        crate::jobs::JobFailure::new(kind, error.to_string())
    }
}

/// How to decode.
#[derive(Debug, Clone)]
pub struct TranscribeOptions {
    /// `None` asks whisper to detect the language.
    pub language: Option<String>,
    /// Word timestamps feed re-segmentation and the diarization vote.
    pub word_timestamps: bool,
    /// How much silence ends a speech chunk, for the VAD pass.
    pub vad_min_silence_ms: u32,
    /// `None` lets whisper.cpp choose by core count.
    pub threads: Option<u32>,
}

impl Default for TranscribeOptions {
    fn default() -> Self {
        TranscribeOptions {
            language: None,
            word_timestamps: true,
            vad_min_silence_ms: 500,
            threads: None,
        }
    }
}

/// What one decode produced.
#[derive(Debug, Clone, PartialEq)]
pub struct Transcription {
    pub language: Option<String>,
    pub segments: Vec<Segment>,
    pub text: String,
}

/// A loaded whisper model.
///
/// `Debug` prints the device rather than the context, which is an opaque
/// pointer into whisper's own allocations.
pub struct WhisperEngine {
    ctx: *mut whisper_sys::whisper_context,
    /// Kept alive for the lifetime of the context: whisper stores the pointer
    /// it is given rather than copying the string.
    _vad_model_path: Option<CString>,
    vad_model: Option<CString>,
    device: String,
}

impl WhisperEngine {
    /// Load a GGML model, and the VAD model that goes with it.
    ///
    /// `use_gpu` only expresses intent: whisper offloads to whichever ggml
    /// backend actually registered, so on a machine with no CUDA backend
    /// present this quietly stays on the CPU rather than failing.
    pub fn load(
        model_path: &Path,
        vad_model_path: Option<&Path>,
        use_gpu: bool,
    ) -> Result<Self, SttError> {
        if !model_path.is_file() {
            return Err(SttError::ModelMissing(model_path.display().to_string()));
        }
        let path = CString::new(model_path.to_string_lossy().as_bytes())
            .map_err(|_| SttError::Invalid("model path contains a NUL byte".to_string()))?;

        let vad_model = match vad_model_path {
            Some(path) if path.is_file() => Some(
                CString::new(path.to_string_lossy().as_bytes())
                    .map_err(|_| SttError::Invalid("VAD path contains a NUL byte".to_string()))?,
            ),
            // A missing VAD model degrades to decoding the whole stream
            // rather than failing the job: the transcript is still correct,
            // just segmented more coarsely.
            _ => None,
        };

        // SAFETY: `path` outlives the call, and whisper copies what it needs
        // from the params struct during initialisation.
        let ctx = unsafe {
            let mut params = whisper_sys::whisper_context_default_params();
            params.use_gpu = use_gpu;
            whisper_sys::whisper_init_from_file_with_params(path.as_ptr(), params)
        };

        if ctx.is_null() {
            return Err(SttError::ModelLoad {
                path: model_path.display().to_string(),
            });
        }

        Ok(WhisperEngine {
            ctx,
            _vad_model_path: None,
            vad_model,
            device: if use_gpu { "cuda" } else { "cpu" }.to_string(),
        })
    }

    /// What to record on the ledger row.
    pub fn device(&self) -> &str {
        &self.device
    }

    /// Decode `pcm`, reporting progress and honouring cancellation.
    pub fn transcribe(
        &mut self,
        pcm: &Pcm,
        options: &TranscribeOptions,
        job: &JobContext,
    ) -> Result<Transcription, SttError> {
        if pcm.is_empty() {
            return Err(SttError::Invalid(
                "the recording decoded to no audio".to_string(),
            ));
        }

        let language = options
            .language
            .as_ref()
            .map(|lang| CString::new(lang.as_bytes()))
            .transpose()
            .map_err(|_| SttError::Invalid("language contains a NUL byte".to_string()))?;

        // Both callbacks are handed a pointer to this, which lives on the
        // stack for the whole `whisper_full` call below.
        let mut callbacks = Callbacks {
            cancel: job.cancel_token(),
            progress: job,
        };

        // SAFETY: every pointer stored in `params` (`language`, the VAD path,
        // the callback user data) outlives the `whisper_full` call, and the
        // sample slice is not retained past it.
        let result = unsafe {
            let mut params =
                whisper_sys::whisper_full_default_params(whisper_sys::WHISPER_SAMPLING_BEAM_SEARCH);
            params.beam_search.beam_size = BEAM_SIZE;
            params.print_progress = false;
            params.print_realtime = false;
            params.print_timestamps = false;
            params.print_special = false;
            // The Python service ran with condition_on_previous_text=False: a
            // hallucination in one window must not seed the next one.
            params.no_context = true;
            params.token_timestamps = options.word_timestamps;
            if let Some(threads) = options.threads {
                params.n_threads = threads as i32;
            }

            match language.as_ref() {
                Some(lang) => params.language = lang.as_ptr(),
                None => params.detect_language = true,
            }

            if let Some(vad_model) = self.vad_model.as_ref() {
                params.vad = true;
                params.vad_model_path = vad_model.as_ptr();
                params.vad_params.min_silence_duration_ms = options.vad_min_silence_ms as i32;
            }

            params.abort_callback = Some(abort_callback);
            params.abort_callback_user_data =
                &mut callbacks as *mut Callbacks as *mut std::ffi::c_void;
            params.progress_callback = Some(progress_callback);
            params.progress_callback_user_data =
                &mut callbacks as *mut Callbacks as *mut std::ffi::c_void;

            whisper_sys::whisper_full(
                self.ctx,
                params,
                pcm.samples.as_ptr(),
                pcm.samples.len() as i32,
            )
        };

        // A cancelled decode returns an error code like any other failure;
        // the token is what tells the two apart.
        if job.is_cancelled() {
            return Err(SttError::Cancelled);
        }
        if result != 0 {
            return Err(SttError::Decode(result));
        }

        Ok(self.collect(options.word_timestamps))
    }

    /// Read whisper's segment table into the transcript's own shape.
    fn collect(&self, word_timestamps: bool) -> Transcription {
        // SAFETY: every accessor below is reading state `whisper_full` just
        // wrote, with indices bounded by the counts whisper itself reports.
        unsafe {
            let n_segments = whisper_sys::whisper_full_n_segments(self.ctx);
            let mut segments = Vec::with_capacity(n_segments.max(0) as usize);
            let mut text = String::new();

            for index in 0..n_segments {
                let segment_text =
                    cstr(whisper_sys::whisper_full_get_segment_text(self.ctx, index));
                text.push_str(&segment_text);

                let (avg_logprob, words) = self.tokens(index, word_timestamps);

                segments.push(Segment {
                    id: index as i64,
                    // whisper reports centiseconds; the transcript is in
                    // seconds.
                    start: whisper_sys::whisper_full_get_segment_t0(self.ctx, index) as f64 / 100.0,
                    end: whisper_sys::whisper_full_get_segment_t1(self.ctx, index) as f64 / 100.0,
                    avg_logprob,
                    no_speech_prob: Some(whisper_sys::whisper_full_get_segment_no_speech_prob(
                        self.ctx, index,
                    ) as f64),
                    compression_ratio: super::filters::compression_ratio(&segment_text),
                    words,
                    speaker: None,
                    text: segment_text,
                });
            }

            let language = {
                let id = whisper_sys::whisper_full_lang_id(self.ctx);
                (id >= 0).then(|| cstr(whisper_sys::whisper_lang_str(id)))
            };

            Transcription {
                language,
                segments,
                text,
            }
        }
    }

    /// Average log probability over a segment's real tokens, and the words
    /// assembled from their timestamps.
    ///
    /// # Safety
    /// `index` must be a segment index `whisper_full` produced.
    unsafe fn tokens(&self, index: i32, word_timestamps: bool) -> (Option<f64>, Option<Vec<Word>>) {
        let n_tokens = whisper_sys::whisper_full_n_tokens(self.ctx, index);
        if n_tokens <= 0 {
            return (None, None);
        }

        let mut logprob_sum = 0.0f64;
        let mut counted = 0usize;
        let mut words: Vec<Word> = Vec::new();

        for i in 0..n_tokens {
            let data = whisper_sys::whisper_full_get_token_data(self.ctx, index, i);
            let piece = cstr(whisper_sys::whisper_full_get_token_text(self.ctx, index, i));

            // Special tokens (`[_BEG_]`, timestamps, language markers) carry
            // no text a reader would recognise, and averaging their
            // probabilities would skew the segment's confidence.
            if piece.starts_with("[_") {
                continue;
            }

            logprob_sum += data.plog as f64;
            counted += 1;

            if !word_timestamps {
                continue;
            }

            let start = data.t0 as f64 / 100.0;
            let end = data.t1 as f64 / 100.0;

            // whisper emits sub-word tokens; a leading space is where one
            // word ends and the next begins, which is also why the joined
            // word text reproduces the original spacing exactly.
            match words.last_mut() {
                Some(last) if !piece.starts_with(' ') => {
                    last.word.push_str(&piece);
                    last.end = end;
                    // The word is only as confident as its least confident
                    // piece.
                    last.probability = match last.probability {
                        Some(existing) => Some(existing.min(data.p as f64)),
                        None => Some(data.p as f64),
                    };
                }
                _ => words.push(Word {
                    word: piece,
                    start,
                    end,
                    probability: Some(data.p as f64),
                }),
            }
        }

        let avg_logprob = (counted > 0).then(|| logprob_sum / counted as f64);
        let words = (word_timestamps && !words.is_empty()).then_some(words);
        (avg_logprob, words)
    }
}

impl std::fmt::Debug for WhisperEngine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WhisperEngine")
            .field("device", &self.device)
            .field("vad", &self.vad_model.is_some())
            .finish()
    }
}

impl Drop for WhisperEngine {
    fn drop(&mut self) {
        if !self.ctx.is_null() {
            // SAFETY: the context was created by whisper and is freed once.
            unsafe { whisper_sys::whisper_free(self.ctx) };
            self.ctx = std::ptr::null_mut();
        }
    }
}

struct Callbacks<'a> {
    cancel: CancelToken,
    progress: &'a JobContext,
}

/// whisper calls this between decode steps; returning true stops the decode.
///
/// # Safety
/// `user_data` is the `Callbacks` pointer installed on the params struct,
/// valid for the whole `whisper_full` call.
unsafe extern "C" fn abort_callback(user_data: *mut std::ffi::c_void) -> bool {
    let callbacks = &*(user_data as *const Callbacks);
    callbacks.cancel.is_cancelled()
}

/// # Safety
/// See [`abort_callback`].
unsafe extern "C" fn progress_callback(
    _ctx: *mut whisper_sys::whisper_context,
    _state: *mut whisper_sys::whisper_state,
    progress: std::os::raw::c_int,
    user_data: *mut std::ffi::c_void,
) {
    let callbacks = &*(user_data as *const Callbacks);
    callbacks.progress.set_progress(progress as f64 / 100.0);
}

/// # Safety
/// `ptr` must be a NUL-terminated string whisper owns.
unsafe fn cstr(ptr: *const std::os::raw::c_char) -> String {
    if ptr.is_null() {
        return String::new();
    }
    CStr::from_ptr(ptr).to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_model_is_reported_before_anything_is_loaded() {
        let err = WhisperEngine::load(Path::new("Z:\\nope\\ggml-large-v3.bin"), None, false)
            .expect_err("should fail");
        assert!(matches!(err, SttError::ModelMissing(_)), "{err:?}");
    }

    #[test]
    fn a_model_that_is_not_a_model_fails_to_load() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ggml-large-v3.bin");
        std::fs::write(&path, b"not a ggml model").unwrap();

        let err = WhisperEngine::load(&path, None, false).expect_err("should fail");
        assert!(matches!(err, SttError::ModelLoad { .. }), "{err:?}");
    }

    #[test]
    fn model_load_failures_are_attributed_as_such() {
        // The distinction the UI acts on: a broken model offers a re-download,
        // a broken recording does not.
        let failure: crate::jobs::JobFailure = SttError::ModelMissing("x".to_string()).into();
        assert_eq!(failure.kind, wire::ErrorKind::ModelLoad);

        let failure: crate::jobs::JobFailure = SttError::Cancelled.into();
        assert_eq!(failure.kind, wire::ErrorKind::Cancelled);
    }
}
