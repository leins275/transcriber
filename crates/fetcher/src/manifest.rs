//! What may be fetched: pinned URLs, sizes and digests, compiled in.
//!
//! Nothing here may be read from `config.json`, an environment variable or a
//! remote index. The Python original pinned a Hugging Face revision and a set
//! of wheel digests by hand for exactly this reason -- verification needs
//! something to compare against that an attacker who can answer the request
//! cannot also choose.
//!
//! [`PINS`] is that compiled-in table. A caller may also build a [`Payload`] in
//! code (the settings UI selecting one GGUF quantization out of a repo that
//! carries a dozen), which is still a value written by a programmer rather than
//! a value read from a file.

use crate::error::FetchError;

/// What to do with a payload once its bytes are on disk and verified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Install {
    /// Keep the downloaded file itself, at `<dest_dir>/<file_name>`.
    File,
    /// Treat the download as a zip and extract only the members under `prefix`
    /// into `<dest_dir>/<dest_subdir>`.
    ///
    /// Both halves are parameters because the Python this ports had two
    /// callers that disagreed: the CUDA wheels merged their `nvidia/` trees
    /// straight into the runtime directory (an empty `dest_subdir`), while the
    /// GPU build of the LLM runtime unpacked its own package tree into a
    /// subdirectory of its own so the two payloads stayed independent. Member
    /// paths keep their prefix, so `nvidia/cublas/bin/x.dll` lands at
    /// `<dest>/nvidia/cublas/bin/x.dll`.
    ZipTree { prefix: String, dest_subdir: String },
}

/// One file to fetch, and what it must turn out to be.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Payload {
    /// Stable id, used in progress events and log lines.
    pub name: String,
    /// The file name on disk, both for the download and for the installed
    /// copy of a [`Install::File`] payload.
    pub file_name: String,
    pub url: String,
    /// Exact byte length. A transfer that ends at any other length is an
    /// interruption to resume from, never a success.
    pub size: u64,
    /// Lowercase hex SHA-256 of the complete file.
    ///
    /// Required, unlike the Python's optional digest: that was optional only
    /// because a Hugging Face listing omits it for small non-LFS files, and a
    /// payload worth downloading is a payload worth pinning.
    pub sha256: String,
    pub install: Install,
}

impl Payload {
    /// A single file that is kept as-is.
    pub fn file(name: &str, file_name: &str, url: &str, size: u64, sha256: &str) -> Self {
        Payload {
            name: name.to_string(),
            file_name: file_name.to_string(),
            url: url.to_string(),
            size,
            sha256: sha256.to_string(),
            install: Install::File,
        }
    }
}

/// A compiled-in [`Payload`], spelled with `&'static str` so the table can be
/// a `const`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PinnedPayload {
    pub name: &'static str,
    pub file_name: &'static str,
    pub url: &'static str,
    pub size: u64,
    pub sha256: &'static str,
    /// `None` keeps the downloaded file; `Some((prefix, dest_subdir))` unpacks
    /// that part of it.
    pub zip_tree: Option<(&'static str, &'static str)>,
}

impl From<&PinnedPayload> for Payload {
    fn from(pin: &PinnedPayload) -> Self {
        Payload {
            name: pin.name.to_string(),
            file_name: pin.file_name.to_string(),
            url: pin.url.to_string(),
            size: pin.size,
            sha256: pin.sha256.to_string(),
            install: match pin.zip_tree {
                None => Install::File,
                Some((prefix, dest_subdir)) => Install::ZipTree {
                    prefix: prefix.to_string(),
                    dest_subdir: dest_subdir.to_string(),
                },
            },
        }
    }
}

/// Every payload this build knows how to fetch by name.
///
/// A `size` and a `sha256` are measurements of a specific artifact, so each
/// entry is added only once its bytes have been fetched and hashed -- an entry
/// carrying invented ones would fail every download it was used for while
/// looking authoritative. The whisper and VAD digests below were measured
/// locally; the GGUF's is the LFS object id Hugging Face publishes for that
/// revision, which is the same SHA-256.
///
/// Revisions are commit ids rather than `main`: a pin that follows a branch is
/// not a pin, and the digest would drift out from under it silently.
///
/// `every_pinned_url_is_on_the_allowlist` guards the table as it grows.
pub const PINS: &[PinnedPayload] = &[
    PinnedPayload {
        name: "whisper-large-v3",
        file_name: "ggml-large-v3.bin",
        url: "https://huggingface.co/ggerganov/whisper.cpp/resolve/\
              5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3.bin",
        size: 3_095_033_483,
        sha256: "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        zip_tree: None,
    },
    // Small, and not optional in practice: without it whisper decodes the
    // silence between utterances and invents words there. Measured against the
    // Python service, which always ran with its own VAD on, leaving this out
    // produced a third more words than were actually spoken.
    PinnedPayload {
        name: "whisper-vad",
        file_name: "ggml-silero-v5.1.2.bin",
        url: "https://huggingface.co/ggml-org/whisper-vad/resolve/\
              9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v5.1.2.bin",
        size: 885_098,
        sha256: "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf",
        zip_tree: None,
    },
    // --- installer payload -----------------------------------------------
    //
    // These three are staged into the installer at build time rather than
    // downloaded on a user's machine: they are needed before the app can do
    // anything, and a first run that has to fetch a decoder before it can
    // read a file is a worse first run.
    PinnedPayload {
        name: "ffmpeg",
        file_name: "ffmpeg-win64-lgpl.zip",
        // LGPL rather than GPL, and used across a process boundary, so the
        // app's own licensing is unaffected. Pinned to a dated build: BtbN's
        // `latest` tag moves, and a pin that follows a moving tag is not a
        // pin.
        url: "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-08-23-13-03/ffmpeg-n9.0.1-6-g9d4ca21220-win64-lgpl-9.0.zip",
        size: 147_007_734,
        sha256: "96ee3965c8f8ba3210e59374c8b1c58f7c9552ea877d930f3fb63fac94fefcec",
        zip_tree: Some(("", "ffmpeg")),
    },
    PinnedPayload {
        name: "onnxruntime",
        file_name: "onnxruntime-win-x64.zip",
        // The version ort 2.0.0-rc.10 was built against; its `ORT_API_VERSION`
        // is 22, which is ONNX Runtime 1.22. A mismatched library fails at
        // load rather than misbehaving, but only if the two are kept in step.
        url: "https://github.com/microsoft/onnxruntime/releases/download/v1.22.0/onnxruntime-win-x64-1.22.0.zip",
        size: 72_368_545,
        sha256: "174c616efc0271194488642a72f1a514e01487da4dfe84c49296d66e40ebe0da",
        zip_tree: Some(("", "onnxruntime")),
    },
    PinnedPayload {
        name: "diarization-segmentation",
        file_name: "segmentation-3.0.onnx",
        url: "https://github.com/thewh1teagle/pyannote-rs/releases/download/v0.1.0/segmentation-3.0.onnx",
        size: 5_983_836,
        sha256: "b78fc48113bb46fd247ae6a9aea737079550c647638db961df7e0e1e9f4ba62e",
        zip_tree: None,
    },
    PinnedPayload {
        name: "diarization-embedding",
        file_name: "wespeaker_en_voxceleb_CAM++.onnx",
        url: "https://github.com/thewh1teagle/pyannote-rs/releases/download/v0.1.0/wespeaker_en_voxceleb_CAM%2B%2B.onnx",
        size: 29_292_684,
        sha256: "c46fad10b5f81e1aa4a60c162714208577093655076c5450f8c469e522ec54ef",
        zip_tree: None,
    },
    PinnedPayload {
        name: "llm-qwen3.6-35b-a3b-q4-k-m",
        file_name: "Qwen3.6-35B-A3B-Q4_K_M.gguf",
        url: "https://huggingface.co/ggml-org/Qwen3.6-35B-A3B-GGUF/resolve/\
              baec3ebee244827cda0f4557eafa8b28f7545fa6/Qwen3.6-35B-A3B-Q4_K_M.gguf",
        size: 20_419_565_568,
        sha256: "671e47e0ec53c665d048b98c3ecbfd5236b5ca9c3e02ed19fc8f81f7b85140c7",
        zip_tree: None,
    },
];

/// The pinned payload named `name`.
pub fn pinned(name: &str) -> Option<Payload> {
    PINS.iter().find(|pin| pin.name == name).map(Payload::from)
}

/// The download URL for one file in a Hugging Face repository, at one
/// revision.
///
/// The revision belongs in the URL rather than in a `main` that moves: a pin
/// that follows a branch is not a pin, and the digest would drift out from
/// under it silently.
pub fn hugging_face_file(repo: &str, revision: &str, file: &str) -> String {
    format!("https://huggingface.co/{repo}/resolve/{revision}/{file}")
}

/// The download URL for a GitHub release asset.
pub fn github_release_asset(repo: &str, tag: &str, asset: &str) -> String {
    format!("https://github.com/{repo}/releases/download/{tag}/{asset}")
}

/// Refuse a payload whose URL no host on the allowlist serves.
///
/// Called before a transfer starts so a mistyped manifest entry fails at once
/// rather than after the first few payloads have already landed.
pub(crate) fn check_payloads(payloads: &[Payload]) -> Result<(), FetchError> {
    if payloads.is_empty() {
        return Err(FetchError::NothingToDownload);
    }
    for payload in payloads {
        crate::allowlist::check(&payload.url)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_pinned_url_is_on_the_allowlist() {
        // The guard on the table: a pin pointing at a host this crate refuses
        // is a download that can never succeed, and it must fail here rather
        // than in front of a user on first run.
        for pin in PINS {
            assert!(
                crate::allowlist::check(pin.url).is_ok(),
                "{} points at a host that is not allowlisted: {}",
                pin.name,
                pin.url
            );
        }
    }

    #[test]
    fn no_pinned_url_carries_whitespace() {
        // A line-continued string literal that loses its backslash leaves the
        // indentation *inside* the URL. The host check still passes, so
        // without this the damage only shows up as a 404 in front of a user.
        for pin in PINS {
            assert!(
                !pin.url.chars().any(char::is_whitespace),
                "{} has whitespace in its url: {}",
                pin.name,
                pin.url
            );
        }
    }

    #[test]
    fn every_pinned_digest_is_a_lowercase_sha256() {
        for pin in PINS {
            assert_eq!(pin.sha256.len(), 64, "{}", pin.name);
            assert!(
                pin.sha256
                    .chars()
                    .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)),
                "{} has a digest that is not lowercase hex",
                pin.name
            );
            assert!(pin.size > 0, "{} has no size to verify against", pin.name);
        }
    }

    #[test]
    fn a_hub_url_names_the_revision_rather_than_a_branch() {
        let url = hugging_face_file("ggml-org/whisper", "abc123", "ggml-large-v3.bin");
        assert_eq!(
            url,
            "https://huggingface.co/ggml-org/whisper/resolve/abc123/ggml-large-v3.bin"
        );
        assert!(crate::allowlist::check(&url).is_ok());
    }

    #[test]
    fn a_release_asset_url_is_on_the_allowlist() {
        let url = github_release_asset("owner/project", "v1.2.3", "runtime-win.zip");
        assert!(crate::allowlist::check(&url).is_ok());
    }

    #[test]
    fn a_payload_pointing_off_the_allowlist_is_refused_before_any_bytes_move() {
        let payload = Payload::file("x", "x.bin", "https://example.com/x.bin", 1, "ab");
        assert!(matches!(
            check_payloads(&[payload]),
            Err(FetchError::HostNotAllowed { .. })
        ));
    }

    #[test]
    fn a_selection_that_matches_nothing_is_an_error_not_an_empty_download() {
        // Python's `_list_selected_files`: a silent zero-file success would
        // write a `.ready` marker over nothing at all.
        assert!(matches!(
            check_payloads(&[]),
            Err(FetchError::NothingToDownload)
        ));
    }

    #[test]
    fn an_unknown_pin_name_is_none_rather_than_a_guess() {
        assert!(pinned("no-such-payload").is_none());
    }
}
