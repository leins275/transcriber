//! The local LLM: derived knowledge from a transcript that already exists.
//!
//! The pure parts land first because they are what the rest is judged
//! against: how a transcript is cut to fit a context window, how a reasoning
//! model's thinking is kept out of the artifacts it writes, and how many
//! layers of a GGUF will fit in the VRAM actually free.

pub mod chunking;
pub mod extraction;
pub mod gguf_meta;
pub mod llama;
pub mod prompts;
pub mod reasoning;
pub mod report;
pub mod shapes;
pub mod summarize;

pub use llama::LlamaEngine;
pub use reasoning::split_reasoning;

/// One chat message on its way to the model.
#[derive(Debug, Clone, PartialEq)]
pub struct Message {
    pub role: Role,
    pub content: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
        }
    }
}

impl Message {
    pub fn system(content: impl Into<String>) -> Self {
        Message {
            role: Role::System,
            content: content.into(),
        }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Message {
            role: Role::User,
            content: content.into(),
        }
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Message {
            role: Role::Assistant,
            content: content.into(),
        }
    }
}

/// How to run one completion.
#[derive(Debug, Clone, Default)]
pub struct CompleteOptions {
    /// GBNF that the output must conform to. Constraining generation is what
    /// makes a malformed answer nearly impossible rather than merely
    /// detectable; see [`shapes`].
    pub grammar: Option<String>,
    pub max_tokens: u32,
    pub temperature: f64,
}

/// What one completion produced.
#[derive(Debug, Clone, PartialEq)]
pub struct Completion {
    /// The answer, with any chain-of-thought already removed.
    pub text: String,
    /// The thinking a reasoning model emitted, kept for the sidecar file and
    /// deliberately out of the artifact itself.
    pub reasoning: Option<String>,
    pub prompt_tokens: Option<u32>,
    pub completion_tokens: Option<u32>,
}

/// Why a completion could not be produced.
#[derive(Debug, thiserror::Error)]
pub enum LlmError {
    #[error("the assistant model is not installed yet ({0})")]
    ModelMissing(String),
    #[error("failed to load the assistant model at {path}: {detail}")]
    ModelLoad { path: String, detail: String },
    #[error("cancelled")]
    Cancelled,
    #[error("the assistant failed while generating: {0}")]
    Generation(String),
    #[error("the assistant's answer could not be used: {0}")]
    Output(String),
}

impl From<LlmError> for crate::jobs::JobFailure {
    fn from(error: LlmError) -> Self {
        let kind = match error {
            LlmError::Cancelled => wire::ErrorKind::Cancelled,
            LlmError::ModelMissing(_) | LlmError::ModelLoad { .. } => wire::ErrorKind::ModelLoad,
            LlmError::Output(_) => wire::ErrorKind::LlmOutput,
            LlmError::Generation(_) => wire::ErrorKind::Internal,
        };
        crate::jobs::JobFailure::new(kind, error.to_string())
    }
}

/// The only interface the jobs depend on, so nothing above this module ever
/// touches llama.cpp directly.
pub trait LlmEngineApi {
    fn complete(
        &mut self,
        messages: &[Message],
        options: &CompleteOptions,
        job: &crate::jobs::JobContext,
    ) -> Result<Completion, LlmError>;

    /// What to report about the loaded model.
    fn describe(&self) -> LlmInfo;
}

/// What job status and health report about the assistant.
#[derive(Debug, Clone, PartialEq)]
pub struct LlmInfo {
    pub model: String,
    pub device: String,
    pub gpu_layers: i32,
}
