//! The host allowlist every outbound request passes.
//!
//! An allowlist rather than a blocklist, and constants rather than settings:
//! the point of confining outbound TLS to this crate is lost if the set of
//! reachable hosts can be widened by editing a JSON file, or by a manifest
//! entry someone pasted in without reading it.
//!
//! Redirects are the part that is easy to get wrong. A Hugging Face download
//! URL answers with a 302 to an LFS CDN host, so redirects must be followed --
//! and a followed redirect is a second request to a host nobody checked.
//! [`HttpTransport`](crate::transport::HttpTransport) therefore installs
//! [`is_allowed`] as the client's redirect policy, so every hop is checked, not
//! just the first.

use reqwest::Url;

use crate::error::FetchError;

/// Hosts reachable by exact name.
///
/// `objects.githubusercontent.com` and `release-assets.githubusercontent.com`
/// are here because they are where a GitHub release asset actually lives once
/// `github.com` has redirected.
const ALLOWED_HOSTS: &[&str] = &[
    "huggingface.co",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
];

/// The Hugging Face LFS CDN answers under per-region names such as
/// `cdn-lfs-us-1.huggingface.co`, which cannot be enumerated ahead of time.
/// Both halves of the pattern are required, so `cdn-lfs.example.com` and
/// `evil-cdn-lfs.huggingface.co.example.com` are both refused.
const CDN_PREFIX: &str = "cdn-lfs";
const CDN_SUFFIX: &str = ".huggingface.co";

/// Whether `url` may be requested.
///
/// Plain HTTP is refused whatever the host: a payload's integrity is checked by
/// digest, but the request itself must not be readable or rewritable in
/// transit. A non-default port is refused for the same reason it would look odd
/// in a review -- nothing this crate fetches is served anywhere but 443.
pub fn is_allowed(url: &Url) -> bool {
    if url.scheme() != "https" {
        return false;
    }
    if !matches!(url.port(), None | Some(443)) {
        return false;
    }
    // Credentials in a URL would be sent to the host as a header; nothing here
    // authenticates, so their presence means the URL is not what it claims.
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    // A trailing dot names the same host to DNS but not to `==`.
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    if ALLOWED_HOSTS.contains(&host.as_str()) {
        return true;
    }
    host.starts_with(CDN_PREFIX) && host.ends_with(CDN_SUFFIX)
}

/// Parse `url` and refuse it unless [`is_allowed`] accepts it.
///
/// Returns the parsed URL so a caller that has checked does not have to parse
/// twice.
pub fn check(url: &str) -> Result<Url, FetchError> {
    let parsed = Url::parse(url).map_err(|_| FetchError::HostNotAllowed {
        url: url.to_string(),
    })?;
    if is_allowed(&parsed) {
        Ok(parsed)
    } else {
        Err(FetchError::HostNotAllowed {
            url: url.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn allowed(url: &str) -> bool {
        Url::parse(url).map(|u| is_allowed(&u)).unwrap_or(false)
    }

    #[test]
    fn the_hosts_the_manifest_names_are_reachable() {
        assert!(allowed(
            "https://huggingface.co/ggml-org/whisper/resolve/main/ggml-large-v3.bin"
        ));
        assert!(allowed(
            "https://github.com/owner/repo/releases/download/v1/a.zip"
        ));
        assert!(allowed("https://objects.githubusercontent.com/x"));
        assert!(allowed("https://release-assets.githubusercontent.com/x"));
    }

    #[test]
    fn the_lfs_cdn_is_reachable_under_any_region_name() {
        // The host a Hugging Face download redirects to, which changes per
        // region and cannot be listed exhaustively.
        assert!(allowed("https://cdn-lfs.huggingface.co/repos/x"));
        assert!(allowed("https://cdn-lfs-us-1.huggingface.co/repos/x"));
    }

    #[test]
    fn a_host_that_merely_contains_an_allowed_name_is_refused() {
        assert!(!allowed("https://huggingface.co.example.com/x"));
        assert!(!allowed("https://cdn-lfs.huggingface.co.example.com/x"));
        assert!(!allowed("https://notgithub.com/x"));
        assert!(!allowed("https://cdn-lfs.example.com/x"));
    }

    #[test]
    fn a_trailing_dot_does_not_slip_a_host_past_the_comparison() {
        assert!(allowed("https://huggingface.co./x"));
        assert!(!allowed("https://huggingface.co.example.com./x"));
    }

    #[test]
    fn plain_http_is_refused_even_to_an_allowed_host() {
        assert!(!allowed("http://huggingface.co/x"));
    }

    #[test]
    fn an_unexpected_port_or_embedded_credentials_are_refused() {
        assert!(!allowed("https://huggingface.co:8443/x"));
        assert!(allowed("https://huggingface.co:443/x"));
        assert!(!allowed("https://user:pass@huggingface.co/x"));
    }

    #[test]
    fn check_reports_the_url_it_refused() {
        let err = check("https://example.com/model.bin").unwrap_err();
        assert!(matches!(err, FetchError::HostNotAllowed { .. }));
        assert!(err.to_string().contains("example.com"));
    }

    #[test]
    fn something_that_is_not_a_url_at_all_is_refused_rather_than_panicking() {
        assert!(check("not a url").is_err());
        assert!(check("file:///C:/windows/system32").is_err());
    }
}
