//! The failure taxonomy (`errors.py::ErrorKind`).
//!
//! These strings reach three places that outlive any one process: the job
//! ledger's `error_kind` column, the `diarization` block inside
//! `transcript.json`, and the desktop UI's error mapping. They are wire
//! values, so they are spelled out here rather than derived from the variant
//! names.

use serde::{Deserialize, Serialize};
use std::fmt;

/// Every way a job can fail, attributed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    InvalidRequest,
    UnsupportedInput,
    AudioDecode,
    ModelLoad,
    ProviderAuth,
    ProviderPaymentRequired,
    ProviderRateLimited,
    ProviderUnavailable,
    Timeout,
    Cancelled,
    /// An LLM's structured output failed schema validation even after the one
    /// bounded repair retry.
    LlmOutput,
    Internal,
}

impl ErrorKind {
    /// The wire string, as written to the ledger and to `transcript.json`.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InvalidRequest => "invalid_request",
            ErrorKind::UnsupportedInput => "unsupported_input",
            ErrorKind::AudioDecode => "audio_decode",
            ErrorKind::ModelLoad => "model_load",
            ErrorKind::ProviderAuth => "provider_auth",
            ErrorKind::ProviderPaymentRequired => "provider_payment_required",
            ErrorKind::ProviderRateLimited => "provider_rate_limited",
            ErrorKind::ProviderUnavailable => "provider_unavailable",
            ErrorKind::Timeout => "timeout",
            ErrorKind::Cancelled => "cancelled",
            ErrorKind::LlmOutput => "llm_output",
            ErrorKind::Internal => "internal",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `serde(rename_all = "snake_case")` and [`ErrorKind::as_str`] are two
    /// spellings of one contract; this is what keeps them one.
    #[test]
    fn serde_and_as_str_agree_on_every_variant() {
        let all = [
            ErrorKind::InvalidRequest,
            ErrorKind::UnsupportedInput,
            ErrorKind::AudioDecode,
            ErrorKind::ModelLoad,
            ErrorKind::ProviderAuth,
            ErrorKind::ProviderPaymentRequired,
            ErrorKind::ProviderRateLimited,
            ErrorKind::ProviderUnavailable,
            ErrorKind::Timeout,
            ErrorKind::Cancelled,
            ErrorKind::LlmOutput,
            ErrorKind::Internal,
        ];
        for kind in all {
            let json = serde_json::to_string(&kind).unwrap();
            assert_eq!(json, format!("\"{}\"", kind.as_str()));
            let back: ErrorKind = serde_json::from_str(&json).unwrap();
            assert_eq!(back, kind);
        }
    }

    /// The exact strings `errors.py` publishes. Spelled out literally: a
    /// renamed variant must fail here, not silently rewrite a ledger column.
    #[test]
    fn wire_strings_match_the_python_taxonomy() {
        assert_eq!(ErrorKind::InvalidRequest.as_str(), "invalid_request");
        assert_eq!(ErrorKind::UnsupportedInput.as_str(), "unsupported_input");
        assert_eq!(ErrorKind::AudioDecode.as_str(), "audio_decode");
        assert_eq!(ErrorKind::ModelLoad.as_str(), "model_load");
        assert_eq!(ErrorKind::ProviderAuth.as_str(), "provider_auth");
        assert_eq!(
            ErrorKind::ProviderPaymentRequired.as_str(),
            "provider_payment_required"
        );
        assert_eq!(
            ErrorKind::ProviderRateLimited.as_str(),
            "provider_rate_limited"
        );
        assert_eq!(
            ErrorKind::ProviderUnavailable.as_str(),
            "provider_unavailable"
        );
        assert_eq!(ErrorKind::Timeout.as_str(), "timeout");
        assert_eq!(ErrorKind::Cancelled.as_str(), "cancelled");
        assert_eq!(ErrorKind::LlmOutput.as_str(), "llm_output");
        assert_eq!(ErrorKind::Internal.as_str(), "internal");
    }
}
