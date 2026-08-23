//! HTTP binding to F2's transcription service (FR-12, FR-13, FR-14, NFR-4,
//! NFR-5).
//!
//! Route paths and body/response shapes are taken from the merged F2 code
//! in `services/transcription/src/transcription/{app.py,schema.py}`, not
//! from the spec text:
//!
//! - `POST /v1/jobs` — body `{audio_path, output_dir, language?}` (F2's
//!   `JobCreate` has more optional fields; this app never sets them) — a
//!   `202` response body is `{"job_id": "..."}`.
//! - `GET /v1/jobs/{id}` — response includes `status`, `progress`,
//!   `error_kind`, `error_message` among other fields this seam ignores.
//! - `GET /health` — `{"status": "ok", ...}`; any other status string (F2
//!   does not currently emit one) is treated as not ready.
//! - Every `/v1/*` route requires `Authorization: Bearer <token>` when F2
//!   was started with a token (`require_token` in `app.py`); `/health`
//!   never requires one.
//! - F2's `require_token` rejects a bad/missing token with `401` and body
//!   `{"detail": "unauthorized"}` (a plain FastAPI `HTTPException`, not the
//!   `ServiceError` taxonomy shape used by every other failure body).
//!
//! NFR-5 (loopback only) is enforced twice: `reqwest` is declared with no
//! TLS feature compiled in (see `Cargo.toml`), and construction here rejects
//! any base URL whose scheme isn't `http` or whose host isn't a loopback
//! address.

use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use super::{
    JobStatus, LedgerJob, LlmSubmitRequest, ModelDownloadState, ModelDownloadStatus, ServiceError,
    ServiceHealth, SubmitRequest, TranscriptionService,
};

/// Default per-request timeout, applied to `submit()`/`health()` (a longer
/// bound is fine for those — E11's fix only tightens `status()`, below).
const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Default TCP connect timeout.
const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
/// Upper bound on a single `status()` call specifically (E11, NFR-4): with
/// `jobs::POLL_INTERVAL` at 1.5s, a `status()` call that itself takes up to
/// [`DEFAULT_REQUEST_TIMEOUT`] (10s) would let displayed job status go far
/// more than 2s stale against a slow-but-technically-reachable service.
/// Applied as a per-request override in [`HttpTranscriptionService::status`]
/// so it never lengthens whatever timeout the caller configured (it can
/// only tighten it).
const STATUS_REQUEST_TIMEOUT: Duration = Duration::from_millis(1200);

/// Binds [`TranscriptionService`] to F2's real HTTP API.
pub struct HttpTranscriptionService {
    /// Validated, trailing-slash-stripped loopback base URL, e.g.
    /// `http://127.0.0.1:51234`.
    base_url: String,
    token: Option<String>,
    client: reqwest::Client,
    /// The `request_timeout` this client was constructed with — `status()`
    /// applies `min(this, STATUS_REQUEST_TIMEOUT)` as a per-request
    /// override, so a test-provided tighter bound (e.g. 200ms) is still
    /// honored rather than being loosened back up to 1.2s.
    request_timeout: Duration,
}

impl HttpTranscriptionService {
    /// Construct a client against `base_url` with the default timeouts.
    /// Rejects any non-loopback or non-`http` base URL (NFR-5) before a
    /// single request is ever made.
    pub fn new(base_url: &str, token: Option<String>) -> Result<Self, ServiceError> {
        Self::with_timeouts(
            base_url,
            token,
            DEFAULT_CONNECT_TIMEOUT,
            DEFAULT_REQUEST_TIMEOUT,
        )
    }

    /// Construct a client with explicit timeouts (used by callers that need
    /// a tighter bound, and by this module's own tests).
    pub fn with_timeouts(
        base_url: &str,
        token: Option<String>,
        connect_timeout: Duration,
        request_timeout: Duration,
    ) -> Result<Self, ServiceError> {
        let base_url = validate_loopback_base_url(base_url)?;
        let client = reqwest::Client::builder()
            .connect_timeout(connect_timeout)
            .timeout(request_timeout)
            .build()
            .map_err(|err| ServiceError::Unavailable {
                detail: format!("failed to build http client: {err}"),
            })?;
        Ok(HttpTranscriptionService {
            base_url,
            token,
            client,
            request_timeout,
        })
    }

    /// The validated base URL this client was constructed with.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    fn endpoint(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    fn authorize(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.token {
            Some(token) => builder.bearer_auth(token),
            None => builder,
        }
    }

    /// Map a `reqwest` send-level failure (refused, timed out, DNS, TLS,
    /// etc.) onto [`ServiceError::Unavailable`], naming this client's base
    /// URL so the UI can tell the operator what it tried to reach.
    fn unavailable(&self, err: reqwest::Error) -> ServiceError {
        ServiceError::Unavailable {
            detail: format!("{}: {err}", self.base_url),
        }
    }
}

/// `POST /v1/jobs` request body — F2's `JobCreate` keys this app sets.
#[derive(Serialize)]
struct SubmitBody<'a> {
    audio_path: &'a str,
    output_dir: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    language: Option<&'a str>,
}

/// `POST /v1/jobs` request body for a derived (LLM) job -- F2's `JobCreate`
/// with `job_type` + `input_path` instead of `audio_path`.
#[derive(Serialize)]
struct LlmSubmitBody<'a> {
    job_type: &'a str,
    input_path: &'a str,
    output_dir: &'a str,
}

/// `POST /v1/jobs` `202` response body.
#[derive(Deserialize)]
struct SubmitResponse {
    job_id: String,
}

/// `GET /v1/jobs/{id}` response body, reduced to the fields this seam uses.
#[derive(Deserialize)]
struct StatusResponse {
    status: String,
    progress: f64,
    #[serde(default)]
    error_kind: Option<String>,
    #[serde(default)]
    error_message: Option<String>,
}

/// `GET /health` response body, reduced to the fields this seam uses.
/// `model_present` (T13, FR-17) defaults to `false` when the field is
/// absent so a test fixture written before T13 (or a build of F2 older than
/// it) still decodes rather than failing the whole health check.
#[derive(Deserialize)]
struct HealthResponse {
    status: String,
    #[serde(default)]
    model_present: bool,
    /// E13: `None` when the field is absent (an older F2 build), same
    /// default-on-absence convention as `model_present` above.
    #[serde(default)]
    cuda_runtime_present: Option<bool>,
    /// `None` when the field is absent (a build of F2 older than the LLM
    /// feature), same convention as above.
    #[serde(default)]
    llm_model_present: Option<bool>,
}

/// `GET`/`POST`/`DELETE /v1/model/download` response body (T13, FR-12).
#[derive(Deserialize)]
struct ModelDownloadResponse {
    state: String,
    downloaded_bytes: u64,
    total_bytes: u64,
    percent: f64,
    #[serde(default)]
    error_kind: Option<String>,
    #[serde(default)]
    error_message: Option<String>,
    /// E13: only present when a `SetupDownload`'s CUDA-runtime phase failed
    /// and the setup continued into the model phase anyway (E4) -- absent
    /// otherwise, hence `#[serde(default)]`.
    #[serde(default)]
    cuda_warning: Option<String>,
}

impl ModelDownloadResponse {
    fn into_status(self) -> Result<ModelDownloadStatus, ServiceError> {
        let state =
            ModelDownloadState::from_wire(&self.state).ok_or_else(|| ServiceError::Decode {
                message: format!("unrecognised model download state {:?}", self.state),
            })?;
        Ok(ModelDownloadStatus {
            state,
            downloaded_bytes: self.downloaded_bytes,
            total_bytes: self.total_bytes,
            percent: self.percent,
            error_kind: self.error_kind,
            error_message: self.error_message,
            cuda_warning: self.cuda_warning,
        })
    }
}

/// `GET /v1/jobs` response body -- one element per ledger row. F2 returns
/// the sqlite row as-is (`SELECT *`), so this deserializes only the columns
/// the UI shows and lets the rest through unread; every one is
/// `#[serde(default)]` because the row is filled in over the job's lifetime
/// (see [`LedgerJob`]).
#[derive(Deserialize)]
struct LedgerJobResponse {
    job_id: String,
    status: String,
    #[serde(default)]
    created_at: Option<String>,
    #[serde(default)]
    started_at: Option<String>,
    #[serde(default)]
    finished_at: Option<String>,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    device: Option<String>,
    #[serde(default)]
    source_path: Option<String>,
    #[serde(default)]
    output_path: Option<String>,
    #[serde(default)]
    audio_duration_sec: Option<f64>,
    #[serde(default)]
    elapsed_sec: Option<f64>,
    #[serde(default)]
    realtime_factor: Option<f64>,
    #[serde(default)]
    language: Option<String>,
    #[serde(default)]
    segment_count: Option<i64>,
    #[serde(default)]
    error_kind: Option<String>,
    #[serde(default)]
    error_message: Option<String>,
    #[serde(default)]
    service_version: Option<String>,
}

impl From<LedgerJobResponse> for LedgerJob {
    fn from(row: LedgerJobResponse) -> Self {
        LedgerJob {
            job_id: row.job_id,
            status: row.status,
            created_at: row.created_at,
            started_at: row.started_at,
            finished_at: row.finished_at,
            provider: row.provider,
            model: row.model,
            device: row.device,
            source_path: row.source_path,
            output_path: row.output_path,
            audio_duration_sec: row.audio_duration_sec,
            elapsed_sec: row.elapsed_sec,
            realtime_factor: row.realtime_factor,
            language: row.language,
            segment_count: row.segment_count,
            error_kind: row.error_kind,
            error_message: row.error_message,
            service_version: row.service_version,
        }
    }
}

/// Reject any base URL that is not plain `http` on a loopback host (NFR-5),
/// and normalize away a trailing slash so `endpoint()` never doubles one.
fn validate_loopback_base_url(raw: &str) -> Result<String, ServiceError> {
    let url = reqwest::Url::parse(raw).map_err(|err| ServiceError::Unavailable {
        detail: format!("invalid base url {raw:?}: {err}"),
    })?;

    if url.scheme() != "http" {
        return Err(ServiceError::Unavailable {
            detail: format!(
                "base url {raw:?} must use http, not {:?} (NFR-5)",
                url.scheme()
            ),
        });
    }

    let is_loopback = match url.host_str() {
        Some("localhost") => true,
        Some(host) => host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback()),
        None => false,
    };
    if !is_loopback {
        return Err(ServiceError::Unavailable {
            detail: format!("base url {raw:?} is not a loopback address (NFR-5)"),
        });
    }

    Ok(raw.trim_end_matches('/').to_string())
}

/// Build a [`ServiceError`] from a non-2xx response. `401`/`403` map to
/// [`ServiceError::Auth`] (F2's `require_token` bearer-auth failure);
/// everything else maps to [`ServiceError::Http`]. The message prefers the
/// taxonomy's `error_message` field, then a plain `detail` field (F2's
/// `HTTPException` shape for the 401 case), then the raw body.
async fn service_error_from_response(response: reqwest::Response) -> ServiceError {
    let status = response.status().as_u16();
    let body_text = response.text().await.unwrap_or_default();
    let message = extract_message(&body_text).unwrap_or_else(|| body_text.clone());

    if status == 401 || status == 403 {
        ServiceError::Auth { message }
    } else {
        ServiceError::Http { status, message }
    }
}

fn extract_message(body: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(body).ok()?;
    value
        .get("error_message")
        .and_then(|v| v.as_str())
        .or_else(|| value.get("detail").and_then(|v| v.as_str()))
        .map(str::to_string)
}

#[async_trait]
impl TranscriptionService for HttpTranscriptionService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        let response = self
            .client
            .get(self.endpoint("/health"))
            .send()
            .await
            .map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: HealthResponse = response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })?;
        Ok(ServiceHealth {
            ready: parsed.status == "ok",
            detail: None,
            model_present: parsed.model_present,
            cuda_runtime_present: parsed.cuda_runtime_present,
            llm_model_present: parsed.llm_model_present,
        })
    }

    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
        let body = SubmitBody {
            audio_path: &req.audio_path,
            output_dir: &req.output_dir,
            language: req.language.as_deref(),
        };
        let request = self.authorize(self.client.post(self.endpoint("/v1/jobs")).json(&body));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: SubmitResponse = response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })?;
        Ok(parsed.job_id)
    }

    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError> {
        // E11 / NFR-4: never let a single status poll take longer than
        // `STATUS_REQUEST_TIMEOUT` (this can only tighten -- never loosen --
        // whatever timeout the client itself was built with).
        let bounded_timeout = self.request_timeout.min(STATUS_REQUEST_TIMEOUT);
        let request = self
            .authorize(
                self.client
                    .get(self.endpoint(&format!("/v1/jobs/{job_id}"))),
            )
            .timeout(bounded_timeout);
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: StatusResponse = response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })?;
        JobStatus::from_wire(
            &parsed.status,
            parsed.progress,
            parsed.error_kind,
            parsed.error_message,
        )
        .ok_or_else(|| ServiceError::Decode {
            message: format!("unrecognised job status {:?}", parsed.status),
        })
    }

    async fn model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.get(self.endpoint("/v1/model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn start_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.post(self.endpoint("/v1/model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn cancel(&self, job_id: &str) -> Result<(), ServiceError> {
        let request = self.authorize(
            self.client
                .delete(self.endpoint(&format!("/v1/jobs/{job_id}"))),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        Ok(())
    }

    async fn list_ledger_jobs(&self, limit: u32) -> Result<Vec<LedgerJob>, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint("/v1/jobs"))
                .query(&[("limit", limit.to_string())]),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let rows: Vec<LedgerJobResponse> =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(rows.into_iter().map(LedgerJob::from).collect())
    }

    async fn cancel_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.delete(self.endpoint("/v1/model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn submit_llm(&self, req: LlmSubmitRequest) -> Result<String, ServiceError> {
        let body = LlmSubmitBody {
            job_type: req.kind.wire_name(),
            input_path: &req.input_path,
            output_dir: &req.output_dir,
        };
        let request = self.authorize(self.client.post(self.endpoint("/v1/jobs")).json(&body));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: SubmitResponse = response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })?;
        Ok(parsed.job_id)
    }

    async fn llm_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.get(self.endpoint("/v1/llm-model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn start_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.post(self.endpoint("/v1/llm-model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn cancel_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(self.client.delete(self.endpoint("/v1/llm-model/download")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: ModelDownloadResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use wiremock::matchers::{body_json, header, header_exists, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::super::{
        JobState, ModelDownloadState, ServiceError, SubmitRequest, TranscriptionService,
    };
    use super::HttpTranscriptionService;

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime")
            .block_on(future)
    }

    fn request() -> SubmitRequest {
        SubmitRequest {
            audio_path: "C:\\Meetings\\ELS\\260812\\source.mp4".to_string(),
            output_dir: "C:\\Meetings\\ELS\\260812".to_string(),
            language: None,
        }
    }

    #[test]
    fn submit_posts_exact_body_keys_and_returns_job_id_from_202() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(body_json(serde_json::json!({
                    "audio_path": "C:\\Meetings\\ELS\\260812\\source.mp4",
                    "output_dir": "C:\\Meetings\\ELS\\260812",
                })))
                .respond_with(
                    ResponseTemplate::new(202)
                        .set_body_json(serde_json::json!({"job_id": "job-1"})),
                )
                .expect(1)
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let job_id = service
                .submit(request())
                .await
                .expect("submit should succeed");
            assert_eq!(job_id, "job-1");
        });
    }

    #[test]
    fn submit_sends_bearer_token_when_configured_and_omits_it_otherwise() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(header("Authorization", "Bearer secret-token"))
                .respond_with(
                    ResponseTemplate::new(202)
                        .set_body_json(serde_json::json!({"job_id": "job-1"})),
                )
                .expect(1)
                .mount(&server)
                .await;

            let service =
                HttpTranscriptionService::new(&server.uri(), Some("secret-token".to_string()))
                    .expect("loopback base url must be accepted");
            service
                .submit(request())
                .await
                .expect("submit should succeed");
        });

        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .respond_with(
                    ResponseTemplate::new(202)
                        .set_body_json(serde_json::json!({"job_id": "job-1"})),
                )
                .expect(1)
                .mount(&server)
                .await;
            // Assert no Authorization header ever reaches the server when no
            // token is configured.
            Mock::given(header_exists("Authorization"))
                .respond_with(ResponseTemplate::new(500))
                .expect(0)
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            service
                .submit(request())
                .await
                .expect("submit should succeed");
        });
    }

    #[test]
    fn status_maps_each_wire_status_to_the_seams_four_states() {
        run(async {
            let server = MockServer::start().await;
            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");

            let cases: &[(&str, JobState)] = &[
                ("queued", JobState::Queued),
                ("running", JobState::Running),
                ("succeeded", JobState::Done),
                ("failed", JobState::Failed),
                ("cancelled", JobState::Failed),
            ];

            for (wire, expected) in cases {
                Mock::given(method("GET"))
                    .and(path(format!("/v1/jobs/{wire}")))
                    .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                        "job_id": wire,
                        "status": wire,
                        "progress": 0.5,
                        "error_kind": serde_json::Value::Null,
                        "error_message": serde_json::Value::Null,
                    })))
                    .mount(&server)
                    .await;

                let status = service.status(wire).await.expect("status should succeed");
                assert_eq!(status.state, *expected, "wire status {wire}");
                assert_eq!(status.progress, 0.5);
            }
        });
    }

    #[test]
    fn status_passes_progress_error_kind_and_error_message_through_unchanged() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/job-failed"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "job_id": "job-failed",
                    "status": "failed",
                    "progress": 0.75,
                    "error_kind": "provider_unavailable",
                    "error_message": "provider is unavailable",
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let status = service
                .status("job-failed")
                .await
                .expect("status should succeed");
            assert_eq!(status.state, JobState::Failed);
            assert_eq!(status.progress, 0.75);
            assert_eq!(status.error_kind.as_deref(), Some("provider_unavailable"));
            assert_eq!(
                status.error_message.as_deref(),
                Some("provider is unavailable")
            );
        });
    }

    #[test]
    fn health_maps_200_to_ready() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/health"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "status": "ok",
                    "version": "0.1.0",
                    "provider": "local",
                    "model": "large-v3",
                    "device": "cpu",
                    "model_state": "unloaded",
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let health = service.health().await.expect("health should succeed");
            assert!(health.ready);
        });
    }

    #[test]
    fn health_maps_connection_refusal_to_unavailable_naming_base_url() {
        run(async {
            // Reserve a loopback port, then release it immediately so
            // nothing is listening there — a reliable way to force a
            // connection refusal without a flaky server-shutdown race.
            let listener =
                std::net::TcpListener::bind("127.0.0.1:0").expect("bind a free loopback port");
            let port = listener.local_addr().expect("local addr").port();
            drop(listener);
            let base_url = format!("http://127.0.0.1:{port}");

            let service = HttpTranscriptionService::new(&base_url, None)
                .expect("loopback base url must be accepted");
            let err = service.health().await.expect_err("health should fail");
            match err {
                ServiceError::Unavailable { detail } => {
                    assert!(
                        detail.contains(&base_url),
                        "detail {detail:?} must name the base url {base_url:?}"
                    );
                }
                other => panic!("expected Unavailable, got {other:?}"),
            }
        });
    }

    #[test]
    fn a_401_response_maps_to_an_auth_error() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .respond_with(ResponseTemplate::new(401).set_body_json(serde_json::json!({
                    "detail": "unauthorized"
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("wrong".to_string()))
                .expect("loopback base url must be accepted");
            let err = service
                .submit(request())
                .await
                .expect_err("submit should fail on 401");
            match err {
                ServiceError::Auth { message } => assert_eq!(message, "unauthorized"),
                other => panic!("expected Auth, got {other:?}"),
            }
        });
    }

    #[test]
    fn a_5xx_response_maps_to_a_distinct_http_error() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .respond_with(ResponseTemplate::new(500).set_body_json(serde_json::json!({
                    "error_kind": "internal",
                    "error_message": "internal error",
                    "provider_status": serde_json::Value::Null,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .submit(request())
                .await
                .expect_err("submit should fail on 500");
            match err {
                ServiceError::Http { status, message } => {
                    assert_eq!(status, 500);
                    assert_eq!(message, "internal error");
                }
                other => panic!("expected Http, got {other:?}"),
            }
        });
    }

    #[test]
    fn a_malformed_body_maps_to_a_decode_error_without_panicking() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/bad-body"))
                .respond_with(ResponseTemplate::new(200).set_body_string("not json at all"))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .status("bad-body")
                .await
                .expect_err("status should fail on malformed body");
            assert!(matches!(err, ServiceError::Decode { .. }));
        });
    }

    #[test]
    fn constructing_a_client_with_a_non_loopback_base_url_is_rejected() {
        assert!(HttpTranscriptionService::new("http://10.0.0.5:8000", None).is_err());
        assert!(HttpTranscriptionService::new("https://example.com", None).is_err());
    }

    #[test]
    fn constructing_a_client_with_a_loopback_base_url_is_accepted() {
        assert!(HttpTranscriptionService::new("http://127.0.0.1:8756", None).is_ok());
        assert!(HttpTranscriptionService::new("http://localhost:8756", None).is_ok());
    }

    #[test]
    fn status_completes_well_under_the_poll_interval_with_a_bounded_timeout() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/slow"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({
                            "job_id": "slow",
                            "status": "running",
                            "progress": 0.1,
                        }))
                        .set_delay(Duration::from_secs(5)),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::with_timeouts(
                &server.uri(),
                None,
                Duration::from_millis(200),
                Duration::from_millis(200),
            )
            .expect("loopback base url must be accepted");

            let start = Instant::now();
            let err = service
                .status("slow")
                .await
                .expect_err("status should time out");
            let elapsed = start.elapsed();
            assert!(
                elapsed < Duration::from_millis(1500),
                "status() must complete well under the 1.5s poll interval, took {elapsed:?}"
            );
            assert!(matches!(err, ServiceError::Unavailable { .. }));
        });
    }

    #[test]
    fn status_is_bounded_even_on_a_client_built_with_the_production_default_timeout() {
        // E11: `HttpTranscriptionService::new()` (production path) builds
        // its client with `DEFAULT_REQUEST_TIMEOUT` (10s) -- before this
        // fix, a slow-but-reachable service could leave the UI showing
        // stale job status for up to 10s. `status()` must cap itself to
        // `STATUS_REQUEST_TIMEOUT` regardless of the client's own default.
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/slow"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({
                            "job_id": "slow",
                            "status": "running",
                            "progress": 0.1,
                        }))
                        .set_delay(Duration::from_secs(3)),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");

            let start = Instant::now();
            let err = service
                .status("slow")
                .await
                .expect_err("status should time out well before the 3s mock delay elapses");
            let elapsed = start.elapsed();

            assert!(
                elapsed < Duration::from_millis(1500),
                "status() must stay under NFR-4's 2s staleness bound even on the production \
                 default client, took {elapsed:?}"
            );
            assert!(matches!(err, ServiceError::Unavailable { .. }));
        });
    }

    // -- model download (T13, FR-12, FR-17) -------------------------------

    #[test]
    fn health_reports_model_present_from_the_wire_field() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/health"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "status": "ok",
                    "model_present": true,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let health = service.health().await.expect("health should succeed");
            assert!(health.model_present);
        });
    }

    #[test]
    fn health_defaults_model_present_to_false_when_the_field_is_absent() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/health"))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!({"status": "ok"})),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let health = service.health().await.expect("health should succeed");
            assert!(!health.model_present);
        });
    }

    #[test]
    fn health_decodes_cuda_runtime_present_from_the_wire_field_and_defaults_to_none() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/health"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "status": "ok",
                    "cuda_runtime_present": false,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let health = service.health().await.expect("health should succeed");
            assert_eq!(health.cuda_runtime_present, Some(false));
        });
    }

    #[test]
    fn health_defaults_cuda_runtime_present_to_none_when_the_field_is_absent() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/health"))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!({"status": "ok"})),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let health = service.health().await.expect("health should succeed");
            assert_eq!(health.cuda_runtime_present, None);
        });
    }

    #[test]
    fn model_download_status_get_maps_the_wire_shape() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/model/download"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "state": "downloading",
                    "downloaded_bytes": 512,
                    "total_bytes": 1024,
                    "percent": 50.0,
                    "error_kind": serde_json::Value::Null,
                    "error_message": serde_json::Value::Null,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let status = service
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(status.state, ModelDownloadState::Downloading);
            assert_eq!(status.downloaded_bytes, 512);
            assert_eq!(status.total_bytes, 1024);
            assert_eq!(status.percent, 50.0);
            assert_eq!(status.cuda_warning, None);
        });
    }

    #[test]
    fn model_download_status_get_decodes_a_cuda_warning_when_present() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/model/download"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "state": "complete",
                    "downloaded_bytes": 1024,
                    "total_bytes": 1024,
                    "percent": 100.0,
                    "cuda_warning": "digest mismatch for nvidia_cublas_cu12.whl",
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let status = service
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(
                status.cuda_warning.as_deref(),
                Some("digest mismatch for nvidia_cublas_cu12.whl")
            );
        });
    }

    #[test]
    fn start_model_download_posts_and_sends_the_bearer_token() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/model/download"))
                .and(header("Authorization", "Bearer secret-token"))
                .respond_with(ResponseTemplate::new(202).set_body_json(serde_json::json!({
                    "state": "downloading",
                    "downloaded_bytes": 0,
                    "total_bytes": 1024,
                    "percent": 0.0,
                })))
                .expect(1)
                .mount(&server)
                .await;

            let service =
                HttpTranscriptionService::new(&server.uri(), Some("secret-token".to_string()))
                    .expect("loopback base url must be accepted");
            let status = service
                .start_model_download()
                .await
                .expect("start_model_download should succeed");
            assert_eq!(status.state, ModelDownloadState::Downloading);
        });
    }

    #[test]
    fn cancel_model_download_deletes_and_maps_cancelled() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("DELETE"))
                .and(path("/v1/model/download"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "state": "cancelled",
                    "downloaded_bytes": 400,
                    "total_bytes": 1024,
                    "percent": 39.0625,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let status = service
                .cancel_model_download()
                .await
                .expect("cancel_model_download should succeed");
            assert_eq!(status.state, ModelDownloadState::Cancelled);
        });
    }

    #[test]
    fn model_download_status_a_401_response_maps_to_an_auth_error() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/model/download"))
                .respond_with(ResponseTemplate::new(401).set_body_json(serde_json::json!({
                    "detail": "unauthorized"
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("wrong".to_string()))
                .expect("loopback base url must be accepted");
            let err = service
                .model_download_status()
                .await
                .expect_err("model_download_status should fail on 401");
            assert!(matches!(err, ServiceError::Auth { .. }));
        });
    }

    #[test]
    fn model_download_status_an_unrecognised_wire_state_maps_to_a_decode_error() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/model/download"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "state": "bogus",
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "percent": 0.0,
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .model_download_status()
                .await
                .expect_err("an unrecognised wire state must not be silently accepted");
            assert!(matches!(err, ServiceError::Decode { .. }));
        });
    }
}
