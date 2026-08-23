//! The local LLM, on llama.cpp.
//!
//! Replaces `llm/llama_cpp_local.py`. Two behaviours from there are worth
//! naming because they are easy to lose in a port:
//!
//! - **Cancellation is checked per token.** A summarisation of a long meeting
//!   runs for minutes; a user who presses stop should not wait for it.
//! - **GPU offload degrades, never fails.** `llm_gpu_layers = -1` fits as many
//!   whole layers as the free VRAM holds and falls back to zero -- CPU -- the
//!   moment any signal is missing: no NVIDIA driver, an unreadable GGUF
//!   header, a build without offload support. A pinned non-negative value is
//!   honoured verbatim, because someone who set it meant it.
//!
//! The model is a raw context behind `LlamaContext`, so this type is not
//! `Send`: it belongs to the engine's worker thread and never leaves it.

use std::num::NonZeroU32;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use llama_cpp_2::context::params::LlamaContextParams;
use llama_cpp_2::llama_backend::LlamaBackend;
use llama_cpp_2::llama_batch::LlamaBatch;
use llama_cpp_2::model::params::LlamaModelParams;
use llama_cpp_2::model::{AddBos, LlamaChatMessage, LlamaModel};
use llama_cpp_2::sampling::LlamaSampler;

use super::{CompleteOptions, Completion, LlmEngineApi, LlmError, LlmInfo, Message};
use crate::config::Config;
use crate::jobs::JobContext;

/// llama.cpp's backend is process-wide and refuses a second initialisation, so
/// it is created once and leaked: it has to outlive every model anyway, and a
/// runner rebuilt after a panic must find it already there rather than fail.
static BACKEND: OnceLock<Option<&'static LlamaBackend>> = OnceLock::new();

fn llama_backend() -> Result<&'static LlamaBackend, LlmError> {
    (*BACKEND.get_or_init(|| {
        LlamaBackend::init()
            .ok()
            .map(|backend| &*Box::leak(Box::new(backend)))
    }))
    .ok_or_else(|| LlmError::Generation("the llama backend could not be initialised".to_string()))
}

/// A loaded GGUF model.
pub struct LlamaEngine {
    model: LlamaModel,
    model_name: String,
    gpu_layers: i32,
    ctx_size: u32,
    threads: Option<u32>,
}

impl LlamaEngine {
    /// Load the configured GGUF.
    pub fn load(config: &Config) -> Result<Self, LlmError> {
        let model_file = crate::models::llm_model_file(config);
        if !crate::models::is_installed(&model_file) {
            return Err(LlmError::ModelMissing(model_file.display().to_string()));
        }

        // The compute backends have to be registered before any model is
        // loaded; without them ggml has no devices and aborts the process.
        crate::backends::ensure_loaded(&config.app_dir);
        let backend = llama_backend()?;

        let gpu_layers = resolve_gpu_layers(config, &model_file);
        let params = LlamaModelParams::default().with_n_gpu_layers(gpu_layers.max(0) as u32);

        let model = LlamaModel::load_from_file(backend, &model_file, &params).map_err(|err| {
            LlmError::ModelLoad {
                path: model_file.display().to_string(),
                detail: err.to_string(),
            }
        })?;

        Ok(LlamaEngine {
            model,
            model_name: config.llm_model.clone(),
            gpu_layers,
            ctx_size: config.llm_ctx,
            threads: config.llm_threads,
        })
    }
}

/// How many layers to put on the GPU.
fn resolve_gpu_layers(config: &Config, model_file: &Path) -> i32 {
    let configured = config.llm_gpu_layers;
    if configured >= 0 {
        return configured;
    }

    let Some(free_vram) = crate::gpu::free_vram_bytes() else {
        return 0;
    };
    let Some(block_count) = super::gguf_meta::read_block_count(model_file) else {
        return 0;
    };
    let Ok(size) = std::fs::metadata(model_file).map(|meta| meta.len()) else {
        return 0;
    };
    super::gguf_meta::fit_gpu_layers(free_vram, size, block_count)
}

impl std::fmt::Debug for LlamaEngine {
    /// Names the model and where it runs; the context behind it is an opaque
    /// handle into llama.cpp's own allocations.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LlamaEngine")
            .field("model", &self.model_name)
            .field("gpu_layers", &self.gpu_layers)
            .field("ctx", &self.ctx_size)
            .finish()
    }
}

impl LlmEngineApi for LlamaEngine {
    fn describe(&self) -> LlmInfo {
        LlmInfo {
            model: self.model_name.clone(),
            device: if self.gpu_layers == 0 { "cpu" } else { "cuda" }.to_string(),
            gpu_layers: self.gpu_layers,
        }
    }

    fn complete(
        &mut self,
        messages: &[Message],
        options: &CompleteOptions,
        job: &JobContext,
    ) -> Result<Completion, LlmError> {
        job.check_cancelled().map_err(|_| LlmError::Cancelled)?;

        let prompt = self.render_prompt(messages)?;
        let tokens = self
            .model
            .str_to_token(&prompt, AddBos::Always)
            .map_err(|err| LlmError::Generation(format!("could not tokenize the prompt: {err}")))?;

        let ctx_size = NonZeroU32::new(self.ctx_size).unwrap_or(NonZeroU32::new(4096).unwrap());
        let mut params = LlamaContextParams::default().with_n_ctx(Some(ctx_size));
        if let Some(threads) = self.threads {
            params = params.with_n_threads(threads as i32);
        }
        let mut context = self
            .model
            .new_context(llama_backend()?, params)
            .map_err(|err| LlmError::Generation(format!("could not open a context: {err}")))?;

        if tokens.len() >= self.ctx_size as usize {
            // The chunker sizes its input against this window, so overflowing
            // it means the budget and the context disagree -- worth saying
            // plainly rather than letting llama.cpp truncate silently.
            return Err(LlmError::Generation(format!(
                "the prompt is {} tokens but the context window is {}",
                tokens.len(),
                self.ctx_size
            )));
        }

        let mut batch = LlamaBatch::get_one(&tokens).map_err(|err| {
            LlmError::Generation(format!("could not build the prompt batch: {err}"))
        })?;
        context
            .decode(&mut batch)
            .map_err(|err| LlmError::Generation(format!("could not evaluate the prompt: {err}")))?;

        let mut sampler = self.sampler(options);
        let mut answer = String::new();
        let mut produced = 0u32;
        let mut position = tokens.len() as i32;
        // One decoder for the whole generation, not one per token: a token
        // boundary can fall in the middle of a UTF-8 character, which for
        // Cyrillic is the common case rather than an edge case. Decoding each
        // piece independently would turn half the alphabet into replacement
        // characters.
        let mut decoder = encoding_rs::UTF_8.new_decoder();

        while produced < options.max_tokens.max(1) {
            // Per token, not per batch: this is what makes stopping feel
            // immediate on a long generation.
            if job.is_cancelled() {
                return Err(LlmError::Cancelled);
            }

            let token = sampler.sample(&context, -1);
            if self.model.is_eog_token(token) {
                break;
            }
            sampler.accept(token);

            let piece = self
                .model
                .token_to_piece(token, &mut decoder, false, None)
                .unwrap_or_default();
            answer.push_str(&piece);
            produced += 1;

            let mut next = LlamaBatch::new(1, 1);
            next.add(token, position, &[0], true).map_err(|err| {
                LlmError::Generation(format!("could not build the token batch: {err}"))
            })?;
            position += 1;
            context.decode(&mut next).map_err(|err| {
                LlmError::Generation(format!("could not evaluate a token: {err}"))
            })?;
        }

        // A reasoning model puts its thinking in the answer; it belongs in the
        // sidecar file, never in the artifact.
        let (text, reasoning) = super::split_reasoning(&answer);

        Ok(Completion {
            text,
            reasoning,
            prompt_tokens: Some(tokens.len() as u32),
            completion_tokens: Some(produced),
        })
    }
}

impl LlamaEngine {
    /// The sampler chain. A grammar comes first so every later step chooses
    /// only among tokens the schema still allows.
    fn sampler(&self, options: &CompleteOptions) -> LlamaSampler {
        let mut chain = Vec::new();
        if let Some(grammar) = options.grammar.as_deref() {
            if let Ok(sampler) = LlamaSampler::grammar(&self.model, grammar, "root") {
                chain.push(sampler);
            }
        }
        chain.push(LlamaSampler::temp(options.temperature as f32));
        // Seeded rather than random: the same transcript should summarise the
        // same way twice, which is what makes a bad answer reproducible.
        chain.push(LlamaSampler::dist(0));
        LlamaSampler::chain_simple(chain)
    }

    /// Render the conversation with the model's own chat template.
    ///
    /// Using the template baked into the GGUF matters: a Qwen model prompted
    /// in Llama's format answers, but badly, and the failure looks like a bad
    /// model rather than a bad prompt.
    fn render_prompt(&self, messages: &[Message]) -> Result<String, LlmError> {
        let chat: Result<Vec<LlamaChatMessage>, _> = messages
            .iter()
            .map(|message| {
                LlamaChatMessage::new(message.role.as_str().to_string(), message.content.clone())
            })
            .collect();
        let chat = chat.map_err(|err| LlmError::Generation(format!("invalid message: {err}")))?;

        let template = self.model.chat_template(None).map_err(|err| {
            LlmError::Generation(format!("the model has no chat template: {err}"))
        })?;

        self.model
            .apply_chat_template(&template, &chat, true)
            .map_err(|err| LlmError::Generation(format!("could not render the prompt: {err}")))
    }
}

/// The GGUF this configuration would load, whether or not it is there yet.
pub fn model_file(config: &Config) -> PathBuf {
    crate::models::llm_model_file(config)
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
    fn a_missing_model_is_reported_before_anything_loads() {
        let dir = tempfile::tempdir().unwrap();
        let err = LlamaEngine::load(&config_in(dir.path())).expect_err("should fail");
        assert!(matches!(err, LlmError::ModelMissing(_)), "{err:?}");
    }

    #[test]
    fn a_pinned_layer_count_is_honoured_verbatim() {
        // Someone who set a number meant it; the auto-fit must not override
        // it on a machine whose VRAM says otherwise.
        let dir = tempfile::tempdir().unwrap();
        let mut config = config_in(dir.path());
        config.llm_gpu_layers = 12;
        assert_eq!(
            resolve_gpu_layers(&config, Path::new("nonexistent.gguf")),
            12
        );

        config.llm_gpu_layers = 0;
        assert_eq!(
            resolve_gpu_layers(&config, Path::new("nonexistent.gguf")),
            0
        );
    }

    #[test]
    fn auto_fit_falls_back_to_the_cpu_when_a_signal_is_missing() {
        // An unreadable GGUF is enough to give up on offload; running on the
        // CPU is slow, guessing at a layer count is a crash.
        let dir = tempfile::tempdir().unwrap();
        let mut config = config_in(dir.path());
        config.llm_gpu_layers = -1;
        assert_eq!(
            resolve_gpu_layers(&config, &dir.path().join("not-a-model.gguf")),
            0
        );
    }

    #[test]
    fn model_load_failures_are_attributed_as_model_problems() {
        let failure: crate::jobs::JobFailure = LlmError::ModelMissing("x".to_string()).into();
        assert_eq!(failure.kind, wire::ErrorKind::ModelLoad);

        let failure: crate::jobs::JobFailure = LlmError::Output("bad json".to_string()).into();
        assert_eq!(failure.kind, wire::ErrorKind::LlmOutput);
    }
}
