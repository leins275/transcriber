//! T3 (260909-roster-bounds-diarization): the shell's service seam carries
//! an optional per-job speaker cap and an optional per-job cross-meeting
//! matching threshold, and puts them on the wire only when they are set
//! (FR-2 c1-c4, FR-7 c3).
//!
//! Everything here is driven through the crate's public surface
//! (`service::SubmitRequest`, `service::LlmSubmitRequest`,
//! `service::http::HttpTranscriptionService`) against `wiremock` standing
//! in for the Python service's HTTP surface -- the seam's own
//! out-of-process boundary, which is exactly what this contract is about.
//! No Tauri runtime, no real sidecar, no assertion on a private field.
//!
//! Each case asserts the *whole* posted body against a hardcoded literal
//! rather than mounting an exact `body_json` matcher: an unmatched
//! `body_json` makes the mock server answer 404, which surfaces as an
//! opaque submit error instead of showing the body that was actually sent.
//! The pinning is identical (full-body equality, so a stray key fails the
//! test); the diagnostic is not.
//!
//! Deliberately self-contained (no `mod common`): the three helpers below
//! are this file's whole harness, and the shared e2e harness would compile
//! its unused pieces into this binary.

use wiremock::matchers::{method, path as path_matcher};
use wiremock::{Mock, MockServer, ResponseTemplate};

use transcriber_desktop_lib::service::http::HttpTranscriptionService;
use transcriber_desktop_lib::service::{
    LlmJobKind, LlmSubmitRequest, SubmitRequest, TranscriptionService,
};

// -- harness ---------------------------------------------------------------

/// Runs `future` on a fresh multi-thread Tokio runtime — this crate's
/// `tokio` dependency does not enable the `macros` feature, so
/// `#[tokio::test]` is unavailable (same pattern as `tests/job_phase.rs`).
fn run<F: std::future::Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime")
        .block_on(future)
}

/// A mock service that accepts any `POST /v1/jobs` with a `202`, so every
/// case fails on its body assertion rather than on a routing miss.
async fn accepting_service() -> (MockServer, HttpTranscriptionService) {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path_matcher("/v1/jobs"))
        .respond_with(ResponseTemplate::new(202).set_body_json(serde_json::json!({
            "job_id": "job-1"
        })))
        .expect(1)
        .mount(&server)
        .await;
    let service = HttpTranscriptionService::new(&server.uri(), None)
        .expect("loopback base url must be accepted");
    (server, service)
}

/// The JSON body of the one request the mock server received.
async fn posted_body(server: &MockServer) -> serde_json::Value {
    let requests = server
        .received_requests()
        .await
        .expect("mock server records requests");
    assert_eq!(requests.len(), 1, "exactly one submission is expected");
    serde_json::from_slice(&requests[0].body).expect("the posted body is json")
}

/// A transcribe submission with none of the optional fields set — the
/// shape the shell posts for a recording with no strict roster.
fn request() -> SubmitRequest {
    SubmitRequest {
        audio_path: "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4".to_string(),
        output_dir: "C:\\Meetings\\ELS\\260812 - Security issue".to_string(),
        language: None,
        original_file_name: None,
        max_speakers: None,
        speaker_match_threshold: None,
    }
}

/// A derived-job submission with none of the optional fields set.
fn llm_request() -> LlmSubmitRequest {
    LlmSubmitRequest {
        kind: LlmJobKind::Summarize,
        input_path: "C:\\Meetings\\ELS\\260812 - Security issue".to_string(),
        output_dir: "C:\\Meetings\\ELS\\260812 - Security issue".to_string(),
        max_speakers: None,
        speaker_match_threshold: None,
    }
}

// -- cases -----------------------------------------------------------------

#[test]
fn transcribe_submission_carries_the_speaker_cap_and_the_strict_threshold() {
    // FR-2 c1 / FR-7 c3: a meeting under a strict roster of three names is
    // submitted with the cap *and* the lowered matching threshold, and with
    // nothing else added to the body.
    run(async {
        let (server, service) = accepting_service().await;

        service
            .submit(SubmitRequest {
                max_speakers: Some(3),
                speaker_match_threshold: Some(0.4),
                ..request()
            })
            .await
            .expect("submit should succeed");

        assert_eq!(
            posted_body(&server).await,
            serde_json::json!({
                "audio_path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                "output_dir": "C:\\Meetings\\ELS\\260812 - Security issue",
                "max_speakers": 3,
                "speaker_match_threshold": 0.4,
            })
        );
    });
}

#[test]
fn transcribe_submission_without_bounds_posts_the_pre_feature_body() {
    // FR-2 c2 / FR-7 c3: open mode, `unsorted`, an unreadable roster and an
    // empty strict roster all reduce to "no bounds", and that body must be
    // byte-identical to the one this app posted before the feature — neither
    // key present, and never a `null`.
    run(async {
        let (server, service) = accepting_service().await;

        service
            .submit(request())
            .await
            .expect("submit should succeed");

        assert_eq!(
            posted_body(&server).await,
            serde_json::json!({
                "audio_path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                "output_dir": "C:\\Meetings\\ELS\\260812 - Security issue",
            })
        );
    });
}

#[test]
fn transcribe_submission_carries_the_speaker_cap_alone_when_no_threshold_is_set() {
    // FR-7 c3: the two fields serialize independently — a cap without a
    // threshold posts the cap and omits the threshold key entirely, so the
    // service keeps its own configured `speaker_match_threshold`.
    run(async {
        let (server, service) = accepting_service().await;

        service
            .submit(SubmitRequest {
                max_speakers: Some(3),
                ..request()
            })
            .await
            .expect("submit should succeed");

        assert_eq!(
            posted_body(&server).await,
            serde_json::json!({
                "audio_path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                "output_dir": "C:\\Meetings\\ELS\\260812 - Security issue",
                "max_speakers": 3,
            })
        );
    });
}

#[test]
fn diarize_submission_carries_the_speaker_cap_and_the_strict_threshold() {
    // FR-2 c3 / FR-7 c3: "Identify speakers" and the vault-wide backfill go
    // out as the derived `diarize` job, which runs the same diarization pass
    // and therefore carries the same two fields.
    run(async {
        let (server, service) = accepting_service().await;

        service
            .submit_llm(LlmSubmitRequest {
                kind: LlmJobKind::Diarize,
                max_speakers: Some(2),
                speaker_match_threshold: Some(0.3),
                ..llm_request()
            })
            .await
            .expect("submit_llm should succeed");

        assert_eq!(
            posted_body(&server).await,
            serde_json::json!({
                "job_type": "diarize",
                "input_path": "C:\\Meetings\\ELS\\260812 - Security issue",
                "output_dir": "C:\\Meetings\\ELS\\260812 - Security issue",
                "max_speakers": 2,
                "speaker_match_threshold": 0.3,
            })
        );
    });
}

#[test]
fn summarize_submission_posts_the_pre_feature_body() {
    // FR-2 c4: the drop chain's derived stages run no diarization pass, so
    // their bodies stay exactly the three keys they had before the feature.
    run(async {
        let (server, service) = accepting_service().await;

        service
            .submit_llm(llm_request())
            .await
            .expect("submit_llm should succeed");

        assert_eq!(
            posted_body(&server).await,
            serde_json::json!({
                "job_type": "summarize",
                "input_path": "C:\\Meetings\\ELS\\260812 - Security issue",
                "output_dir": "C:\\Meetings\\ELS\\260812 - Security issue",
            })
        );
    });
}
