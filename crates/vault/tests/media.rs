//! Tests for `vault::media` — the recording media extension allowlist
//! (FR-7).

use vault::error::VaultError;
use vault::media;

#[test]
fn accepts_all_ten_allowed_extensions_lowercase() {
    for ext in [
        "mp4", "mkv", "mov", "webm", "avi", "m4a", "mp3", "wav", "flac", "ogg",
    ] {
        let name = format!("recording.{ext}");
        let parsed = media::from_file_name(&name).unwrap_or_else(|e| {
            panic!("expected {name:?} to be accepted, got {e:?}");
        });
        assert_eq!(parsed.as_str(), ext);
    }
}

#[test]
fn normalizes_uppercase_extension_to_lowercase() {
    let parsed = media::from_file_name("recording.MP4").expect("MP4 should be accepted");
    assert_eq!(parsed.as_str(), "mp4");
}

#[test]
fn rejects_non_media_extensions() {
    for name in ["file.exe", "file.txt", "file.json", "file.md", "file."] {
        let result = media::from_file_name(name);
        assert!(result.is_err(), "expected {name:?} to be rejected");
    }
}

#[test]
fn rejects_name_with_no_extension_at_all() {
    let result = media::from_file_name("recording");
    assert!(matches!(
        result,
        Err(VaultError::UnsupportedMediaType { .. })
    ));
}

#[test]
fn rejects_empty_name() {
    let result = media::from_file_name("");
    assert!(matches!(
        result,
        Err(VaultError::UnsupportedMediaType { .. })
    ));
}

#[test]
fn error_carries_offending_extension() {
    match media::from_file_name("movie.exe") {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "exe"),
        other => panic!("expected UnsupportedMediaType, got {other:?}"),
    }
}

#[test]
fn resolves_on_last_extension_when_multiple_dots() {
    let result = media::from_file_name("movie.mp4.exe");
    match result {
        Err(VaultError::UnsupportedMediaType { ext }) => assert_eq!(ext, "exe"),
        other => panic!("expected UnsupportedMediaType on last extension, got {other:?}"),
    }
}

#[test]
fn rejects_non_ascii_extension() {
    let result = media::from_file_name("recording.mp\u{e9}");
    assert!(result.is_err());
}

#[test]
fn extension_extraction_never_panics_on_dots_only_names() {
    for name in [".", "..", "...", "...."] {
        let _ = media::from_file_name(name);
    }
}

#[test]
fn source_file_name_joins_ext() {
    let parsed = media::from_file_name("recording.mp4").expect("mp4 should be accepted");
    assert_eq!(parsed.source_file_name(), "source.mp4");
}

#[test]
fn stem_strips_extension() {
    assert_eq!(media::stem("recording.mp4"), "recording");
    assert_eq!(media::stem("recording.mp4.exe"), "recording.mp4");
    assert_eq!(media::stem("noext"), "noext");
}
