//! T7: the typed `speaker_match_threshold_strict` config key and the strict
//! threshold the shell resolves from it (blueprint FR-7, criteria 1 and 2).
//!
//! Everything here goes through the crate's public config surface against a
//! real `tempfile` directory holding a real `config.json` — the file *is*
//! the contract (`docs/config-contract.md`), shared byte-for-byte with the
//! Python service, so nothing is faked: no mocks, no in-memory stand-in for
//! the file.

use std::fs;

use serde_json::json;
use tempfile::tempdir;

use transcriber_desktop_lib::config::{
    config_path, load, save, set_diarization, set_meetings_root, strict_speaker_match_threshold,
    Settings,
};
use transcriber_desktop_lib::error::ErrorKind;

/// Writes `body` as the `config.json` of a fresh temp app-dir.
fn config_dir_holding(body: serde_json::Value) -> tempfile::TempDir {
    let dir = tempdir().expect("tempdir");
    fs::write(config_path(dir.path()), body.to_string()).expect("write config");
    dir
}

/// Thresholds are `f64` derived by subtraction, so binary rounding puts the
/// result a few ULPs off the decimal the contract documents (`0.35 - 0.1`
/// is `0.249999999999999972`). The documented value is what matters; the
/// last bit is not.
fn assert_threshold(actual: f64, expected: f64) {
    assert!(
        (actual - expected).abs() < 1e-12,
        "expected strict threshold {expected}, got {actual}"
    );
}

#[test]
fn an_untouched_install_matches_a_tenth_more_leniently_than_the_service_default() {
    let dir = tempdir().expect("tempdir");

    let settings = load(dir.path()).expect("load must not fail on absent file");

    assert_eq!(settings.speaker_match_threshold_strict, None);
    assert_threshold(strict_speaker_match_threshold(&settings), 0.4);
}

#[test]
fn an_explicit_strict_key_is_the_effective_threshold() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold_strict": 0.3,
    }));

    let settings = load(dir.path()).expect("load must succeed");

    assert_eq!(settings.speaker_match_threshold_strict, Some(0.3));
    assert_threshold(strict_speaker_match_threshold(&settings), 0.3);
}

#[test]
fn a_tuned_service_threshold_derives_a_strict_threshold_a_tenth_lower() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold": 0.35,
    }));

    let settings = load(dir.path()).expect("load must succeed");

    assert_threshold(strict_speaker_match_threshold(&settings), 0.25);
}

#[test]
fn a_low_service_threshold_stops_at_the_strict_floor() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold": 0.25,
    }));

    let settings = load(dir.path()).expect("load must succeed");

    assert_threshold(strict_speaker_match_threshold(&settings), 0.2);
}

#[test]
fn a_strict_key_above_one_is_ignored_in_favour_of_the_derived_threshold() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold": 0.35,
        "speaker_match_threshold_strict": 1.5,
    }));

    let settings = load(dir.path()).expect("an out-of-range strict key must still load");

    assert_threshold(strict_speaker_match_threshold(&settings), 0.25);
}

#[test]
fn a_negative_strict_key_is_ignored_in_favour_of_the_derived_threshold() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold": 0.35,
        "speaker_match_threshold_strict": -0.2,
    }));

    let settings = load(dir.path()).expect("an out-of-range strict key must still load");

    assert_threshold(strict_speaker_match_threshold(&settings), 0.25);
}

#[test]
fn saving_the_speaker_settings_preserves_the_strict_threshold_on_disk() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold_strict": 0.3,
        "speaker_match_threshold": 0.35,
    }));
    let mut settings = load(dir.path()).expect("load must succeed");

    set_diarization(dir.path(), &mut settings, true, None).expect("set_diarization must succeed");

    let on_disk: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(config_path(dir.path())).expect("read config"))
            .expect("saved config must be valid JSON");
    assert_eq!(
        on_disk.get("speaker_match_threshold_strict"),
        Some(&json!(0.3)),
        "the strict key must survive a load -> modify -> save round trip"
    );
    assert_eq!(
        on_disk.get("speaker_match_threshold"),
        Some(&json!(0.35)),
        "the service's own flat key must survive the same round trip"
    );
}

#[test]
fn choosing_a_meetings_root_preserves_the_strict_threshold_on_disk() {
    // The second round trip FR-7 c1 names. `set_meetings_root` mutates the
    // same `Settings` and calls the same `save`, but it is the one an
    // operator reaches first (the first-run wizard), so a key it dropped
    // would be gone before the feature was ever used.
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold_strict": 0.3,
        "speaker_match_threshold": 0.35,
    }));
    let mut settings = load(dir.path()).expect("load must succeed");
    // A real directory outside the app dir: `set_meetings_root` creates it
    // and write-probes it before saving.
    let vault = tempdir().expect("tempdir");
    let vault_path = vault.path().join("Meetings");

    set_meetings_root(
        dir.path(),
        dir.path(),
        &mut settings,
        vault_path.to_str().expect("temp path must be UTF-8"),
    )
    .expect("set_meetings_root must succeed");

    let on_disk: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(config_path(dir.path())).expect("read config"))
            .expect("saved config must be valid JSON");
    assert_eq!(
        on_disk.get("speaker_match_threshold_strict"),
        Some(&json!(0.3)),
        "the strict key must survive choosing a meetings root"
    );
    assert_eq!(
        on_disk.get("speaker_match_threshold"),
        Some(&json!(0.35)),
        "the service's own flat key must survive the same round trip"
    );
    assert_threshold(strict_speaker_match_threshold(&settings), 0.3);
}

#[test]
fn a_default_config_is_saved_without_a_strict_threshold_key() {
    let dir = tempdir().expect("tempdir");

    save(dir.path(), &Settings::default()).expect("save must succeed");

    let body = fs::read_to_string(config_path(dir.path())).expect("read config");
    assert!(
        !body.contains("speaker_match_threshold_strict"),
        "an unset strict threshold must be absent, never written as null, got: {body}"
    );
}

#[test]
fn a_non_numeric_strict_threshold_is_reported_as_a_malformed_config() {
    let dir = config_dir_holding(json!({
        "schema_version": 1,
        "speaker_match_threshold_strict": "0.3",
    }));

    let err = load(dir.path()).expect_err("a string strict threshold must not load");

    assert_eq!(err.kind(), ErrorKind::Config);
    assert!(
        err.message().contains("config.json"),
        "message must name the file, got: {}",
        err.message()
    );
}
