//! The complete `AppError`/`ErrorKind` taxonomy shared with the UI (IPC
//! contract, plan.md).
//!
//! Every `#[tauri::command]` handler returns `Result<T, AppError>`.
//! `AppError` serializes to exactly `{"kind": "...", "message": "..."}` so
//! the frontend's `AppError` / `ErrorKind` TypeScript types stay a
//! mechanical mirror of this file. This file is frozen once wave-2 tasks
//! start consuming it: later tasks construct `AppError` values through the
//! constructors below but do not add or rename variants here.

use serde::Serialize;
use std::fmt;

/// The frozen set of error kinds from the plan's IPC contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    NotConfigured,
    InvalidArgument,
    OutsideRoot,
    UnsupportedExtension,
    NotAFile,
    Vault,
    Collision,
    ServiceUnavailable,
    Service,
    Config,
    Io,
    Internal,
}

/// The error type every Tauri command returns.
///
/// Internally tagged on `kind` with every variant carrying exactly one
/// `message` field, so serialization is exactly
/// `{"kind": "...", "message": "..."}` with no other shape possible.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AppError {
    NotConfigured { message: String },
    InvalidArgument { message: String },
    OutsideRoot { message: String },
    UnsupportedExtension { message: String },
    NotAFile { message: String },
    Vault { message: String },
    Collision { message: String },
    ServiceUnavailable { message: String },
    Service { message: String },
    Config { message: String },
    Io { message: String },
    Internal { message: String },
}

impl AppError {
    /// The `ErrorKind` this error carries, independent of its message.
    pub fn kind(&self) -> ErrorKind {
        match self {
            AppError::NotConfigured { .. } => ErrorKind::NotConfigured,
            AppError::InvalidArgument { .. } => ErrorKind::InvalidArgument,
            AppError::OutsideRoot { .. } => ErrorKind::OutsideRoot,
            AppError::UnsupportedExtension { .. } => ErrorKind::UnsupportedExtension,
            AppError::NotAFile { .. } => ErrorKind::NotAFile,
            AppError::Vault { .. } => ErrorKind::Vault,
            AppError::Collision { .. } => ErrorKind::Collision,
            AppError::ServiceUnavailable { .. } => ErrorKind::ServiceUnavailable,
            AppError::Service { .. } => ErrorKind::Service,
            AppError::Config { .. } => ErrorKind::Config,
            AppError::Io { .. } => ErrorKind::Io,
            AppError::Internal { .. } => ErrorKind::Internal,
        }
    }

    /// The message this error carries, independent of its kind.
    pub fn message(&self) -> &str {
        match self {
            AppError::NotConfigured { message }
            | AppError::InvalidArgument { message }
            | AppError::OutsideRoot { message }
            | AppError::UnsupportedExtension { message }
            | AppError::NotAFile { message }
            | AppError::Vault { message }
            | AppError::Collision { message }
            | AppError::ServiceUnavailable { message }
            | AppError::Service { message }
            | AppError::Config { message }
            | AppError::Io { message }
            | AppError::Internal { message } => message,
        }
    }

    pub fn not_configured(message: impl Into<String>) -> Self {
        AppError::NotConfigured {
            message: message.into(),
        }
    }

    pub fn invalid_argument(message: impl Into<String>) -> Self {
        AppError::InvalidArgument {
            message: message.into(),
        }
    }

    pub fn outside_root(message: impl Into<String>) -> Self {
        AppError::OutsideRoot {
            message: message.into(),
        }
    }

    pub fn unsupported_extension(message: impl Into<String>) -> Self {
        AppError::UnsupportedExtension {
            message: message.into(),
        }
    }

    pub fn not_a_file(message: impl Into<String>) -> Self {
        AppError::NotAFile {
            message: message.into(),
        }
    }

    pub fn vault(message: impl Into<String>) -> Self {
        AppError::Vault {
            message: message.into(),
        }
    }

    pub fn collision(message: impl Into<String>) -> Self {
        AppError::Collision {
            message: message.into(),
        }
    }

    pub fn service_unavailable(message: impl Into<String>) -> Self {
        AppError::ServiceUnavailable {
            message: message.into(),
        }
    }

    pub fn service(message: impl Into<String>) -> Self {
        AppError::Service {
            message: message.into(),
        }
    }

    pub fn config(message: impl Into<String>) -> Self {
        AppError::Config {
            message: message.into(),
        }
    }

    pub fn io(message: impl Into<String>) -> Self {
        AppError::Io {
            message: message.into(),
        }
    }

    pub fn internal(message: impl Into<String>) -> Self {
        AppError::Internal {
            message: message.into(),
        }
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message())
    }
}

impl std::error::Error for AppError {}

impl From<std::io::Error> for AppError {
    fn from(err: std::io::Error) -> Self {
        AppError::Io {
            message: err.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn assert_serializes(err: &AppError, kind: &str) {
        let value = serde_json::to_value(err).expect("AppError must serialize");
        assert_eq!(
            value,
            json!({ "kind": kind, "message": err.message() }),
            "AppError must serialize to exactly {{\"kind\", \"message\"}}"
        );
    }

    #[test]
    fn every_variant_serializes_to_kind_and_message() {
        assert_serializes(
            &AppError::not_configured("no meetings root"),
            "not_configured",
        );
        assert_serializes(&AppError::invalid_argument("bad arg"), "invalid_argument");
        assert_serializes(&AppError::outside_root("escapes root"), "outside_root");
        assert_serializes(
            &AppError::unsupported_extension("bad ext"),
            "unsupported_extension",
        );
        assert_serializes(&AppError::not_a_file("is a directory"), "not_a_file");
        assert_serializes(&AppError::vault("vault rejected"), "vault");
        assert_serializes(&AppError::collision("destination exists"), "collision");
        assert_serializes(
            &AppError::service_unavailable("service down"),
            "service_unavailable",
        );
        assert_serializes(&AppError::service("service error"), "service");
        assert_serializes(&AppError::config("malformed config"), "config");
        assert_serializes(&AppError::io("io failure"), "io");
        assert_serializes(&AppError::internal("unexpected"), "internal");
    }

    #[test]
    fn from_io_error_maps_to_io_kind_without_panicking() {
        let io_err = std::io::Error::new(std::io::ErrorKind::NotFound, "missing file");
        let app_err: AppError = io_err.into();
        assert_eq!(app_err.kind(), ErrorKind::Io);
        assert_eq!(app_err.message(), "missing file");
    }
}
