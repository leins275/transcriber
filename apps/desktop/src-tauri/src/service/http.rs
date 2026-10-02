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
//! - `GET /v1/jobs/{id}` — response includes `status`, `progress`
//!   (nullable: `null` whenever the running phase has no linear signal),
//!   `phase` (the sub-step's display label, absent on a service older than
//!   F3), `error_kind`, `error_message` among other fields this seam
//!   ignores.
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
    ChatEvent, ChatRequest, DiarizationStatus, IndexMeeting, IndexStatus, JobStatus,
    LedgerCompression, LedgerJob, LlmCatalogModel, LlmModelsStatus, LlmSubmitRequest,
    ModelDownloadState, ModelDownloadStatus, SearchHit, SearchQuery, ServiceError, ServiceHealth,
    SubmitRequest, TranscriptionService,
};

/// Default per-request timeout, applied to `submit()`/`health()` (a longer
/// bound is fine for those — E11's fix only tightens `status()`, below).
const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Default TCP connect timeout.
const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
/// Upper bound on one whole chat stream. LOAD-BEARING per-request override:
/// reqwest's client-wide `timeout` covers the *entire body read*, so the
/// default 10s would kill every streamed answer mid-generation.
const CHAT_STREAM_TIMEOUT: Duration = Duration::from_secs(30 * 60);
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

    /// One call against a download slot (`GET`/`POST`/`DELETE` on a
    /// `/v1/<slot>/download` route): every slot answers the same
    /// `ModelDownloadResponse` body.
    async fn download_slot(
        &self,
        builder: reqwest::RequestBuilder,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let response = self
            .authorize(builder)
            .send()
            .await
            .map_err(|err| self.unavailable(err))?;
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

/// `POST /v1/jobs` request body — F2's `JobCreate` keys this app sets.
#[derive(Serialize)]
struct SubmitBody<'a> {
    audio_path: &'a str,
    output_dir: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    language: Option<&'a str>,
    /// FR-1/FR-5: present only when an original file name is actually known.
    /// Skipped entirely otherwise, so a submission without one posts a body
    /// byte-identical to the pre-feature one rather than an empty `meeting`
    /// object F2 would then persist as a meaningless `meeting_json`.
    #[serde(skip_serializing_if = "Option::is_none")]
    meeting: Option<SubmitMeeting<'a>>,
    /// FR-2/FR-7: present only for a meeting under a strict project roster.
    /// Skipped otherwise, so a submission without bounds posts a body
    /// byte-identical to the pre-feature one rather than a `null` F2 would
    /// have to interpret.
    #[serde(skip_serializing_if = "Option::is_none")]
    max_speakers: Option<u32>,
    /// FR-7: travels independently of `max_speakers` -- omitted alone, it
    /// leaves the service's own configured threshold in force.
    #[serde(skip_serializing_if = "Option::is_none")]
    speaker_match_threshold: Option<f64>,
}

/// The `meeting` member of F2's `JobCreate` (`schema.py`), which the service
/// stores verbatim in the ledger's `meeting_json` column.
#[derive(Serialize)]
struct SubmitMeeting<'a> {
    original_file_name: &'a str,
}

/// `POST /v1/jobs` request body for a derived (LLM) job -- F2's `JobCreate`
/// with `job_type` + `input_path` instead of `audio_path`.
#[derive(Serialize)]
struct LlmSubmitBody<'a> {
    job_type: &'a str,
    input_path: &'a str,
    output_dir: &'a str,
    /// FR-2/FR-7: set only on a `diarize` submission under a strict roster;
    /// `summarize` / `export` bodies stay exactly their three pre-feature
    /// keys.
    #[serde(skip_serializing_if = "Option::is_none")]
    max_speakers: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    speaker_match_threshold: Option<f64>,
}

/// `POST /v1/jobs` `202` response body.
#[derive(Deserialize)]
struct SubmitResponse {
    job_id: String,
}

/// `GET /v1/jobs/{id}/result` of a `suggest_title` job: its manifest.
#[derive(Deserialize)]
struct SuggestedTitleResponse {
    title: String,
}

/// `POST /v1/search` request body (F2's `SearchRequest`).
#[derive(Serialize)]
struct SearchBody<'a> {
    query: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    project: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    top_k: Option<u32>,
}

/// One hit of `POST /v1/search`'s response (F2's `SearchResultModel`).
#[derive(Deserialize)]
struct SearchHitBody {
    kind: String,
    project: String,
    meeting_dir: String,
    meeting_title: String,
    snippet: String,
    score: f64,
    #[serde(default)]
    start_sec: Option<f64>,
    #[serde(default)]
    timestamp: Option<String>,
}

impl From<SearchHitBody> for SearchHit {
    fn from(body: SearchHitBody) -> Self {
        SearchHit {
            kind: body.kind,
            project: body.project,
            meeting_dir: body.meeting_dir,
            meeting_title: body.meeting_title,
            snippet: body.snippet,
            score: body.score,
            start_sec: body.start_sec,
            timestamp: body.timestamp,
        }
    }
}

/// `POST /v1/search` response body.
#[derive(Deserialize)]
struct SearchResponseBody {
    results: Vec<SearchHitBody>,
}

/// `GET /v1/index/status` response body (F2's `IndexStatusResponse`).
#[derive(Deserialize)]
struct IndexStatusBody {
    project: String,
    #[serde(default)]
    updated_at: Option<i64>,
    indexing: bool,
    #[serde(default)]
    progress: Option<f64>,
    indexed_count: u64,
    total_count: u64,
    meetings: Vec<IndexMeetingBody>,
}

#[derive(Deserialize)]
struct IndexMeetingBody {
    name: String,
    meeting_dir: String,
    state: String,
    chunks: u64,
}

/// `GET /v1/people` response body.
#[derive(Deserialize)]
struct PeopleResponseBody {
    people: Vec<super::Person>,
}

/// `PUT /v1/people` request body. An omitted field is left alone by the
/// service, so `None` must not travel as `null`.
#[derive(Serialize)]
struct PersonUpdateBody<'a> {
    name: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    new_name: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    aliases: Option<&'a [String]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bio: Option<&'a str>,
}

/// `DELETE /v1/people` response body.
#[derive(Deserialize)]
struct PersonDeletedBody {
    deleted: bool,
}

/// `POST /v1/chat` request body (F2's `ChatRequest`).
#[derive(Serialize)]
struct ChatBody<'a> {
    messages: Vec<ChatMessageBody<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    project: Option<&'a str>,
}

#[derive(Serialize)]
struct ChatMessageBody<'a> {
    role: &'a str,
    content: &'a str,
}

/// Splits every *complete* `\n\n`-terminated SSE block off the front of
/// `buffer` and parses it into [`ChatEvent`]s; an unterminated tail block
/// stays in the buffer for the next network chunk. Pure, so the chunk-split
/// and CRLF edge cases are unit-testable without HTTP.
fn drain_sse_events(buffer: &mut String) -> Vec<ChatEvent> {
    let mut events = Vec::new();
    loop {
        // A block ends at a blank line -- which is `\r\n\r\n` under CRLF
        // framing (where `\n\n` never literally occurs).
        let lf = buffer.find("\n\n");
        let crlf = buffer.find("\r\n\r\n");
        let (boundary, terminator_len) = match (lf, crlf) {
            (Some(l), Some(c)) if c < l => (c, 4),
            (Some(l), _) => (l, 2),
            (None, Some(c)) => (c, 4),
            (None, None) => break,
        };
        let block: String = buffer.drain(..boundary + terminator_len).collect();
        let mut event_name: Option<&str> = None;
        let mut data: Option<&str> = None;
        for line in block.lines() {
            let line = line.strip_suffix('\r').unwrap_or(line);
            if let Some(rest) = line.strip_prefix("event: ") {
                event_name = Some(rest);
            } else if let Some(rest) = line.strip_prefix("data: ") {
                data = Some(rest);
            }
        }
        let (Some(name), Some(data)) = (event_name, data) else {
            continue;
        };
        let parsed: serde_json::Value = match serde_json::from_str(data) {
            Ok(value) => value,
            Err(err) => {
                events.push(ChatEvent::Error {
                    message: format!("malformed chat event payload: {err}"),
                });
                continue;
            }
        };
        match name {
            "delta" => {
                if let Some(text) = parsed.get("text").and_then(|value| value.as_str()) {
                    events.push(ChatEvent::Delta {
                        text: text.to_string(),
                    });
                }
            }
            "sources" => {
                let sources = parsed
                    .get("sources")
                    .cloned()
                    .and_then(|value| serde_json::from_value::<Vec<SearchHitBody>>(value).ok())
                    .unwrap_or_default()
                    .into_iter()
                    .map(SearchHit::from)
                    .collect();
                events.push(ChatEvent::Sources { sources });
            }
            "done" => {
                let finish_reason = parsed
                    .get("finish_reason")
                    .and_then(|value| value.as_str())
                    .unwrap_or("stop")
                    .to_string();
                events.push(ChatEvent::Done { finish_reason });
            }
            "error" => {
                let message = parsed
                    .get("error_message")
                    .and_then(|value| value.as_str())
                    .unwrap_or("chat failed")
                    .to_string();
                events.push(ChatEvent::Error { message });
            }
            // Unknown event names are forward-compatibility, not errors.
            _ => {}
        }
    }
    events
}

/// `GET /v1/jobs/{id}` response body, reduced to the fields this seam uses.
///
/// Both `progress` and `phase` default on absence *and* accept `null`:
/// F2 answers `progress: null` for a phase with no linear signal, and a
/// build of F2 older than F3 sends no `phase` key at all. Neither is a
/// decode failure -- a poll that cannot read a label must still read the
/// job's state.
#[derive(Deserialize)]
struct StatusResponse {
    status: String,
    #[serde(default)]
    progress: Option<f64>,
    #[serde(default)]
    phase: Option<String>,
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
    /// Same convention; `None` also means "no NVIDIA GPU on this host".
    #[serde(default)]
    llm_gpu_build_present: Option<bool>,
    /// `None` when the field is absent (a build of F2 older than hybrid
    /// search), same convention as above.
    #[serde(default)]
    embedding_model_present: Option<bool>,
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

/// `GET /v1/diarization/status` body (`schema.py::DiarizationStatus`).
#[derive(Debug, Deserialize)]
struct DiarizationStatusResponse {
    runtime_present: bool,
    model_present: bool,
    token_present: bool,
    enabled: bool,
    gpu_present: bool,
    #[serde(default)]
    runtime_total_bytes: u64,
}

const DIARIZATION_RUNTIME_SLOT: &str = "/v1/diarization-runtime/download";
const DIARIZATION_MODEL_SLOT: &str = "/v1/diarization-model/download";

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

/// `GET /v1/llm-models` response body: the curated catalog listing.
#[derive(Deserialize)]
struct LlmModelsResponse {
    active: String,
    models: Vec<LlmCatalogModelResponse>,
}

/// One row of `GET /v1/llm-models`.
#[derive(Deserialize)]
struct LlmCatalogModelResponse {
    id: String,
    label: String,
    file: String,
    #[serde(default)]
    size_bytes: Option<u64>,
    catalog: bool,
    present: bool,
    active: bool,
    download: ModelDownloadResponse,
}

impl LlmModelsResponse {
    fn into_status(self) -> Result<LlmModelsStatus, ServiceError> {
        let models = self
            .models
            .into_iter()
            .map(|row| {
                Ok(LlmCatalogModel {
                    id: row.id,
                    label: row.label,
                    file: row.file,
                    size_bytes: row.size_bytes,
                    catalog: row.catalog,
                    present: row.present,
                    active: row.active,
                    download: row.download.into_status()?,
                })
            })
            .collect::<Result<Vec<_>, ServiceError>>()?;
        Ok(LlmModelsStatus {
            active: self.active,
            models,
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
    job_type: Option<String>,
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
    /// The ledger's `meeting_json` column: a TEXT column holding whatever
    /// `json.dumps` wrote, so it arrives as a JSON *string*, not an object.
    /// Absent on every pre-feature row (FR-6), hence the default.
    #[serde(default)]
    meeting_json: Option<String>,
    /// The ledger's `result_json` column: a derived job's result manifest,
    /// a JSON *string* like `meeting_json`.
    #[serde(default)]
    result_json: Option<String>,
}

/// A `compress` row's result manifest as the sizes the service log shows.
///
/// Total like [`original_file_name_from`]: another job type's manifest, an
/// unfinished row or a shape this side does not know is "nothing to show",
/// never a failed listing.
fn compression_from(
    job_type: Option<&str>,
    result_json: Option<&str>,
) -> Option<LedgerCompression> {
    if job_type != Some("compress") {
        return None;
    }
    serde_json::from_str(result_json?).ok()
}

/// Pull the original file name out of a raw `meeting_json` value (FR-2).
///
/// Deliberately total: anything that is not a JSON object carrying a
/// non-empty string under `original_file_name` -- unparseable text, a
/// different shape, a number, an empty name -- is simply "no recorded name"
/// (NFR-2). A ledger listing must never fail over one odd row's metadata,
/// because the panel has a perfectly good fallback (FR-3).
fn original_file_name_from(meeting_json: Option<&str>) -> Option<String> {
    let parsed: serde_json::Value = serde_json::from_str(meeting_json?).ok()?;
    let name = parsed.get("original_file_name")?.as_str()?;
    if name.is_empty() {
        return None;
    }
    Some(name.to_string())
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
            compression: compression_from(row.job_type.as_deref(), row.result_json.as_deref()),
            job_type: row.job_type,
            original_file_name: original_file_name_from(row.meeting_json.as_deref()),
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
            llm_gpu_build_present: parsed.llm_gpu_build_present,
            embedding_model_present: parsed.embedding_model_present,
        })
    }

    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
        let body = SubmitBody {
            audio_path: &req.audio_path,
            output_dir: &req.output_dir,
            language: req.language.as_deref(),
            meeting: req
                .original_file_name
                .as_deref()
                .map(|original_file_name| SubmitMeeting { original_file_name }),
            max_speakers: req.max_speakers,
            speaker_match_threshold: req.speaker_match_threshold,
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
            parsed.phase,
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
            max_speakers: req.max_speakers,
            speaker_match_threshold: req.speaker_match_threshold,
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

    async fn suggested_title(&self, job_id: &str) -> Result<String, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint(&format!("/v1/jobs/{job_id}/result"))),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: SuggestedTitleResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(parsed.title)
    }

    async fn submit_index(&self) -> Result<String, ServiceError> {
        let body = serde_json::json!({ "job_type": "index" });
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

    async fn search(&self, query: SearchQuery) -> Result<Vec<SearchHit>, ServiceError> {
        let body = SearchBody {
            query: &query.query,
            project: query.project.as_deref(),
            top_k: query.top_k,
        };
        let request = self.authorize(self.client.post(self.endpoint("/v1/search")).json(&body));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: SearchResponseBody =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(parsed.results.into_iter().map(SearchHit::from).collect())
    }

    async fn index_status(&self, project: &str) -> Result<IndexStatus, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint("/v1/index/status"))
                .query(&[("project", project)]),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: IndexStatusBody =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(IndexStatus {
            project: parsed.project,
            updated_at: parsed.updated_at,
            indexing: parsed.indexing,
            progress: parsed.progress,
            indexed_count: parsed.indexed_count,
            total_count: parsed.total_count,
            meetings: parsed
                .meetings
                .into_iter()
                .map(|meeting| IndexMeeting {
                    name: meeting.name,
                    meeting_dir: meeting.meeting_dir,
                    state: meeting.state,
                    chunks: meeting.chunks,
                })
                .collect(),
        })
    }

    async fn voice_status(&self, project: &str) -> Result<super::VoiceStatus, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint("/v1/voices/status"))
                .query(&[("project", project)]),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })
    }

    async fn list_people(&self) -> Result<Vec<super::Person>, ServiceError> {
        let request = self.authorize(self.client.get(self.endpoint("/v1/people")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: PeopleResponseBody =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(parsed.people)
    }

    async fn person_detail(&self, name: &str) -> Result<super::PersonDetail, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint("/v1/people/detail"))
                .query(&[("name", name)]),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })
    }

    async fn save_person(
        &self,
        update: super::PersonUpdate,
    ) -> Result<super::PersonRecord, ServiceError> {
        let body = PersonUpdateBody {
            name: &update.name,
            new_name: update.new_name.as_deref(),
            aliases: update.aliases.as_deref(),
            bio: update.bio.as_deref(),
        };
        let request = self.authorize(self.client.put(self.endpoint("/v1/people")).json(&body));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        response.json().await.map_err(|err| ServiceError::Decode {
            message: err.to_string(),
        })
    }

    async fn delete_person(&self, name: &str) -> Result<bool, ServiceError> {
        let request = self.authorize(
            self.client
                .delete(self.endpoint("/v1/people"))
                .query(&[("name", name)]),
        );
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let parsed: PersonDeletedBody =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(parsed.deleted)
    }

    async fn chat_stream(
        &self,
        req: ChatRequest,
        on_event: Box<dyn Fn(ChatEvent) + Send + Sync>,
        mut cancel: tokio::sync::oneshot::Receiver<()>,
    ) -> Result<(), ServiceError> {
        use futures_util::StreamExt;

        let body = ChatBody {
            messages: req
                .messages
                .iter()
                .map(|message| ChatMessageBody {
                    role: &message.role,
                    content: &message.content,
                })
                .collect(),
            project: req.project.as_deref(),
        };
        let request = self
            .authorize(self.client.post(self.endpoint("/v1/chat")).json(&body))
            // Overrides the client-wide 10s total-read timeout, which would
            // otherwise kill every stream mid-generation (see the const).
            .timeout(CHAT_STREAM_TIMEOUT);
        let response = request.send().await.map_err(|err| self.unavailable(err))?;

        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }

        let mut stream = response.bytes_stream();
        let mut buffer = String::new();
        loop {
            tokio::select! {
                // Cancellation outranks a ready network chunk, so a turn
                // superseded before its first read forwards nothing.
                biased;
                // Fired *or dropped*: either way the caller is done with us.
                _ = &mut cancel => return Ok(()),
                chunk = stream.next() => match chunk {
                    None => return Ok(()),
                    Some(Err(err)) => {
                        on_event(ChatEvent::Error {
                            message: err.to_string(),
                        });
                        return Ok(());
                    }
                    Some(Ok(bytes)) => {
                        buffer.push_str(&String::from_utf8_lossy(&bytes));
                        for event in drain_sse_events(&mut buffer) {
                            let terminal =
                                matches!(event, ChatEvent::Done { .. } | ChatEvent::Error { .. });
                            on_event(event);
                            if terminal {
                                return Ok(());
                            }
                        }
                    }
                }
            }
        }
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

    async fn embedding_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(
            self.client
                .get(self.endpoint("/v1/embedding-model/download")),
        );
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

    async fn start_embedding_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(
            self.client
                .post(self.endpoint("/v1/embedding-model/download")),
        );
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

    async fn cancel_embedding_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let request = self.authorize(
            self.client
                .delete(self.endpoint("/v1/embedding-model/download")),
        );
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

    async fn diarization_status(&self) -> Result<DiarizationStatus, ServiceError> {
        let request = self.authorize(self.client.get(self.endpoint("/v1/diarization/status")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: DiarizationStatusResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        Ok(DiarizationStatus {
            runtime_present: parsed.runtime_present,
            model_present: parsed.model_present,
            token_present: parsed.token_present,
            enabled: parsed.enabled,
            gpu_present: parsed.gpu_present,
            runtime_total_bytes: parsed.runtime_total_bytes,
        })
    }

    async fn diarization_runtime_download_status(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.get(self.endpoint(DIARIZATION_RUNTIME_SLOT)))
            .await
    }

    async fn start_diarization_runtime_download(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.post(self.endpoint(DIARIZATION_RUNTIME_SLOT)))
            .await
    }

    async fn cancel_diarization_runtime_download(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.delete(self.endpoint(DIARIZATION_RUNTIME_SLOT)))
            .await
    }

    async fn diarization_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.get(self.endpoint(DIARIZATION_MODEL_SLOT)))
            .await
    }

    async fn start_diarization_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.post(self.endpoint(DIARIZATION_MODEL_SLOT)))
            .await
    }

    async fn cancel_diarization_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        self.download_slot(self.client.delete(self.endpoint(DIARIZATION_MODEL_SLOT)))
            .await
    }

    async fn llm_models(&self) -> Result<LlmModelsStatus, ServiceError> {
        let request = self.authorize(self.client.get(self.endpoint("/v1/llm-models")));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: LlmModelsResponse =
            response.json().await.map_err(|err| ServiceError::Decode {
                message: err.to_string(),
            })?;
        parsed.into_status()
    }

    async fn start_llm_model_download_for(
        &self,
        model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let path = format!("/v1/llm-models/{model_id}/download");
        let request = self.authorize(self.client.post(self.endpoint(&path)));
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

    async fn cancel_llm_model_download_for(
        &self,
        model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let path = format!("/v1/llm-models/{model_id}/download");
        let request = self.authorize(self.client.delete(self.endpoint(&path)));
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

    async fn delete_llm_model(&self, model_id: &str) -> Result<LlmModelsStatus, ServiceError> {
        let path = format!("/v1/llm-models/{model_id}");
        let request = self.authorize(self.client.delete(self.endpoint(&path)));
        let response = request.send().await.map_err(|err| self.unavailable(err))?;
        if !response.status().is_success() {
            return Err(service_error_from_response(response).await);
        }
        let parsed: LlmModelsResponse =
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
        JobState, LedgerCompression, ModelDownloadState, ServiceError, SubmitRequest,
        TranscriptionService,
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
            original_file_name: None,
            max_speakers: None,
            speaker_match_threshold: None,
        }
    }

    #[test]
    fn submit_posts_exact_body_keys_and_returns_job_id_from_202() {
        run(async {
            let server = MockServer::start().await;
            // FR-1: an ingest-originated job carries the dropped recording's
            // original file name in F2's existing `meeting` object, alongside
            // the paths -- `body_json` is an *exact* match, so this also pins
            // that nothing else was added to the wire body.
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(body_json(serde_json::json!({
                    "audio_path": "C:\\Meetings\\ELS\\260812\\source.mp4",
                    "output_dir": "C:\\Meetings\\ELS\\260812",
                    "meeting": {"original_file_name": "ELS - 260812 - Security issue.mp4"},
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
                .submit(SubmitRequest {
                    original_file_name: Some("ELS - 260812 - Security issue.mp4".to_string()),
                    ..request()
                })
                .await
                .expect("submit should succeed");
            assert_eq!(job_id, "job-1");
        });
    }

    #[test]
    fn submit_omits_the_meeting_key_entirely_when_no_original_file_name_is_known() {
        // FR-5 at the wire level: a retranscribe of an already-filed
        // recording has no original name on disk, so the body must be
        // byte-identical to the pre-feature one -- no `meeting` key at all,
        // never `source.<ext>` passed off as an "original file name".
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
    fn submit_carries_the_selected_language_on_the_wire() {
        // FR-5: an operator-chosen override must actually reach F2's
        // `JobCreate.language` -- the field this app hardcoded to `None`
        // until the recording page grew a language control.
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(body_json(serde_json::json!({
                    "audio_path": "C:\\Meetings\\ELS\\260812\\source.mp4",
                    "output_dir": "C:\\Meetings\\ELS\\260812",
                    "language": "en",
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
            service
                .submit(SubmitRequest {
                    language: Some("en".to_string()),
                    ..request()
                })
                .await
                .expect("submit should succeed");
        });
    }

    #[test]
    fn submit_omits_the_language_key_entirely_when_the_choice_is_auto() {
        // FR-5's second half: Auto is the *absence* of the field, not an
        // empty string or a null -- F2 must be free to run its own
        // constrained detection.
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

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            service
                .submit(request())
                .await
                .expect("submit should succeed");

            let requests = server
                .received_requests()
                .await
                .expect("mock server records requests");
            let body: serde_json::Value =
                serde_json::from_slice(&requests[0].body).expect("body is json");
            assert!(
                body.get("language").is_none(),
                "an Auto submission must omit `language` entirely, got {body}"
            );
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
                assert_eq!(status.progress, Some(0.5));
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
            assert_eq!(status.progress, Some(0.75));
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

    // -- the chat SSE parser (pure) ------------------------------------

    #[test]
    fn sse_events_parse_across_arbitrary_chunk_splits() {
        use super::super::ChatEvent;
        use super::drain_sse_events;

        let whole = "event: sources\ndata: {\"sources\": []}\n\n\
                     event: delta\ndata: {\"text\": \"Hel\"}\n\n\
                     event: delta\ndata: {\"text\": \"lo\"}\n\n\
                     event: done\ndata: {\"finish_reason\": \"stop\"}\n\n";

        // Feed it one byte at a time -- every possible mid-event split.
        let mut buffer = String::new();
        let mut events = Vec::new();
        for ch in whole.chars() {
            buffer.push(ch);
            events.extend(drain_sse_events(&mut buffer));
        }

        assert_eq!(events.len(), 4);
        assert!(matches!(events[0], ChatEvent::Sources { .. }));
        assert_eq!(
            events[1],
            ChatEvent::Delta {
                text: "Hel".to_string()
            }
        );
        assert_eq!(
            events[3],
            ChatEvent::Done {
                finish_reason: "stop".to_string()
            }
        );
        assert!(buffer.is_empty());
    }

    #[test]
    fn sse_parser_tolerates_crlf_unknown_events_and_malformed_payloads() {
        use super::super::ChatEvent;
        use super::drain_sse_events;

        let mut buffer = "event: future-thing\r\ndata: {\"x\": 1}\r\n\r\n\
                          event: delta\r\ndata: {\"text\": \"ok\"}\r\n\r\n\
                          event: delta\ndata: {not json\n\n"
            .to_string();

        let events = drain_sse_events(&mut buffer);

        // Unknown event dropped, CRLF delta parsed, malformed data becomes
        // an Error event rather than a silent swallow.
        assert_eq!(events.len(), 2);
        assert_eq!(
            events[0],
            ChatEvent::Delta {
                text: "ok".to_string()
            }
        );
        assert!(matches!(events[1], ChatEvent::Error { .. }));
    }

    #[test]
    fn chat_stream_posts_the_conversation_and_forwards_parsed_events() {
        run(async {
            let server = MockServer::start().await;
            let body = "event: delta\ndata: {\"text\": \"hi\"}\n\n\
                        event: done\ndata: {\"finish_reason\": \"stop\"}\n\n";
            Mock::given(method("POST"))
                .and(path("/v1/chat"))
                .and(body_json(serde_json::json!({
                    "messages": [{"role": "user", "content": "вопрос"}]
                })))
                .respond_with(
                    ResponseTemplate::new(200)
                        .insert_header("content-type", "text/event-stream")
                        .set_body_string(body),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
            let sink = received.clone();
            let (_cancel_tx, cancel_rx) = tokio::sync::oneshot::channel::<()>();

            service
                .chat_stream(
                    super::super::ChatRequest {
                        messages: vec![super::super::ChatMessage {
                            role: "user".to_string(),
                            content: "вопрос".to_string(),
                        }],
                        project: None,
                    },
                    Box::new(move |event| sink.lock().unwrap().push(event)),
                    cancel_rx,
                )
                .await
                .expect("chat stream should succeed");

            let events = received.lock().unwrap().clone();
            assert_eq!(events.len(), 2);
            assert!(matches!(events[1], super::super::ChatEvent::Done { .. }));
        });
    }

    #[test]
    fn dropping_the_cancel_sender_ends_the_stream_before_any_event() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/chat"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .insert_header("content-type", "text/event-stream")
                        .set_body_string("event: delta\ndata: {\"text\": \"a\"}\n\n"),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel::<()>();
            drop(cancel_tx); // superseded before it even started

            let received = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
            let sink = received.clone();
            service
                .chat_stream(
                    super::super::ChatRequest {
                        messages: vec![super::super::ChatMessage {
                            role: "user".to_string(),
                            content: "q".to_string(),
                        }],
                        project: None,
                    },
                    Box::new(move |event| sink.lock().unwrap().push(event)),
                    cancel_rx,
                )
                .await
                .expect("a cancelled stream is not an error");

            // The biased select saw the (already fired) cancellation before
            // it ever read the body: nothing was forwarded.
            assert!(received.lock().unwrap().is_empty());
        });
    }

    #[test]
    fn voice_status_asks_for_the_project_and_decodes_the_memory() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/voices/status"))
                .and(wiremock::matchers::query_param("project", "ACME"))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "project": "ACME",
                    "roster_only": true,
                    "updated_at": 1_790_000_000,
                    "rescanned": ["260901 - Planning"],
                    "rescanned_elsewhere": 3,
                    "voices": [
                        {
                            "name": "Anna",
                            "samples": 2,
                            "here": 1,
                            "other_projects": ["OTHER"],
                            "speech_sec": 310.5,
                            "set_aside": 1
                        }
                    ],
                    "meetings": [
                        {
                            "name": "260901 - Planning",
                            "state": "named",
                            "scanned_at": 1_790_000_000,
                            "voices": [
                                {
                                    "label": "Speaker 1",
                                    "name": "Anna",
                                    "speech_sec": 120.0,
                                    "quality": "conflict",
                                    "conflicts_with": "Boris"
                                }
                            ]
                        },
                        {"name": "260830 - Empty", "state": "no_transcript", "voices": []}
                    ]
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let status = service
                .voice_status("ACME")
                .await
                .expect("voice_status should succeed");

            assert_eq!(status.project, "ACME");
            assert_eq!(status.rescanned, vec!["260901 - Planning".to_string()]);
            assert!(status.roster_only);
            assert_eq!(status.rescanned_elsewhere, 3);
            assert_eq!(status.voices[0].samples, 2);
            assert_eq!(status.voices[0].here, 1);
            assert_eq!(status.voices[0].other_projects, vec!["OTHER".to_string()]);
            assert_eq!(status.voices[0].set_aside, 1);
            assert_eq!(status.meetings.len(), 2);
            let sample = &status.meetings[0].voices[0];
            assert_eq!(sample.quality, "conflict");
            assert_eq!(sample.conflicts_with.as_deref(), Some("Boris"));
            assert_eq!(status.meetings[1].scanned_at, None);
        });
    }

    #[test]
    fn list_people_decodes_the_speakers_database() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/people"))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "people": [
                        {
                            "name": "Nikita",
                            "aliases": ["Никита"],
                            "bio": "",
                            "registered": true,
                            "projects": ["ELS", "GIS"],
                            "meetings": 34,
                            "labelled_segments": 5123,
                            "hand_segments": 4000,
                            "speech_sec": 12345.0,
                            "voice_samples": 26,
                            "voice_set_aside": 5
                        },
                        {"name": "Anna", "registered": false}
                    ]
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let people = service
                .list_people()
                .await
                .expect("list_people should succeed");

            assert_eq!(people.len(), 2);
            assert_eq!(people[0].name, "Nikita");
            assert_eq!(people[0].aliases, vec!["Никита".to_string()]);
            assert!(people[0].registered);
            assert_eq!(
                people[0].projects,
                vec!["ELS".to_string(), "GIS".to_string()]
            );
            assert_eq!(people[0].meetings, 34);
            assert_eq!(people[0].hand_segments, 4000);
            assert_eq!(people[0].voice_samples, 26);
            assert_eq!(people[0].voice_set_aside, 5);
            // A sparse row still decodes: the counts default to nothing.
            assert!(!people[1].registered);
            assert_eq!(people[1].meetings, 0);
        });
    }

    #[test]
    fn person_detail_asks_by_name_and_decodes_meetings_and_segments() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/people/detail"))
                .and(wiremock::matchers::query_param("name", "Никита"))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "name": "Nikita",
                    "aliases": ["Никита"],
                    "bio": "Lead",
                    "registered": true,
                    "projects": [
                        {"project": "GIS", "meetings": 7, "speech_sec": 3100.0, "in_roster": true}
                    ],
                    "voice": {"samples": 26, "set_aside": 5, "speech_sec": 12300.0},
                    "meetings": [
                        {
                            "project": "GIS",
                            "meeting": "260903 - 360 photos & flows",
                            "meeting_dir": "GIS/260903 - 360 photos & flows",
                            "labelled_segments": 132,
                            "hand_segments": 120,
                            "speech_sec": 640.0,
                            "voice_quality": "ok",
                            "segments": [
                                {"id": 12, "start": 63.2, "end": 70.1, "text": "hello"}
                            ],
                            "segments_truncated": true
                        },
                        {
                            "project": "GIS",
                            "meeting": "260801 - Kickoff",
                            "meeting_dir": "GIS/260801 - Kickoff",
                            "labelled_segments": 3,
                            "hand_segments": 3,
                            "speech_sec": 12.0,
                            "voice_quality": null,
                            "segments": [],
                            "segments_truncated": false
                        }
                    ]
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let detail = service
                .person_detail("Никита")
                .await
                .expect("person_detail should succeed");

            assert_eq!(detail.name, "Nikita");
            assert_eq!(detail.bio, "Lead");
            assert!(detail.projects[0].in_roster);
            assert_eq!(detail.voice.samples, 26);
            assert_eq!(detail.voice.set_aside, 5);
            let first = &detail.meetings[0];
            assert_eq!(first.meeting_dir, "GIS/260903 - 360 photos & flows");
            assert_eq!(first.voice_quality.as_deref(), Some("ok"));
            assert_eq!(first.segments[0].id, 12);
            assert_eq!(first.segments[0].text, "hello");
            assert!(first.segments_truncated);
            assert_eq!(detail.meetings[1].voice_quality, None);
        });
    }

    #[test]
    fn person_detail_for_an_unknown_name_is_the_services_404() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/people/detail"))
                .respond_with(ResponseTemplate::new(404).set_body_json(serde_json::json!({
                    "error_kind": "not_found",
                    "error_message": "no such person: Ghost"
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .person_detail("Ghost")
                .await
                .expect_err("an unknown name must fail");

            assert_eq!(
                err,
                ServiceError::Http {
                    status: 404,
                    message: "no such person: Ghost".to_string()
                }
            );
        });
    }

    #[test]
    fn save_person_puts_only_the_fields_that_are_set() {
        run(async {
            let server = MockServer::start().await;
            // A bio-only save: `new_name` and `aliases` must be absent, not
            // null -- an omitted field is what the service leaves alone.
            Mock::given(method("PUT"))
                .and(path("/v1/people"))
                .and(body_json(
                    serde_json::json!({ "name": "Nikita", "bio": "Lead" }),
                ))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "name": "Nikita",
                    "aliases": ["Никита"],
                    "bio": "Lead",
                    "registered": true
                })))
                .mount(&server)
                .await;
            Mock::given(method("PUT"))
                .and(path("/v1/people"))
                .and(body_json(serde_json::json!({
                    "name": "Nikita",
                    "new_name": "Nikita L",
                    "aliases": ["Никита"]
                })))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "name": "Nikita L",
                    "aliases": ["Никита", "Nikita"],
                    "bio": "Lead",
                    "registered": true
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let saved = service
                .save_person(super::super::PersonUpdate {
                    name: "Nikita".to_string(),
                    bio: Some("Lead".to_string()),
                    ..Default::default()
                })
                .await
                .expect("save_person should succeed");
            assert_eq!(saved.bio, "Lead");
            assert!(saved.registered);

            let renamed = service
                .save_person(super::super::PersonUpdate {
                    name: "Nikita".to_string(),
                    new_name: Some("Nikita L".to_string()),
                    aliases: Some(vec!["Никита".to_string()]),
                    bio: None,
                })
                .await
                .expect("save_person should succeed");
            assert_eq!(renamed.name, "Nikita L");
            assert_eq!(
                renamed.aliases,
                vec!["Никита".to_string(), "Nikita".to_string()]
            );
        });
    }

    #[test]
    fn delete_person_asks_by_name_and_decodes_the_flag() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("DELETE"))
                .and(path("/v1/people"))
                .and(wiremock::matchers::query_param("name", "Nikita"))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({ "deleted": true })),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let deleted = service
                .delete_person("Nikita")
                .await
                .expect("delete_person should succeed");

            assert!(deleted);
        });
    }

    #[test]
    fn submit_index_posts_exactly_the_bare_job_type_and_decodes_the_id() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(body_json(serde_json::json!({ "job_type": "index" })))
                .and(header("Authorization", "Bearer token-1"))
                .respond_with(ResponseTemplate::new(202).set_body_json(serde_json::json!({
                    "job_id": "idx-1"
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("token-1".to_string()))
                .expect("loopback base url must be accepted");
            let job_id = service
                .submit_index()
                .await
                .expect("submit_index should succeed");

            assert_eq!(job_id, "idx-1");
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
    fn list_ledger_jobs_reads_the_original_file_name_out_of_meeting_json() {
        run(async {
            let server = MockServer::start().await;
            // FR-2: `meeting_json` is a TEXT column holding `json.dumps(...)`,
            // so it crosses the wire as a JSON *string*, not an object. The
            // parse happens exactly once, here, so the panel stays
            // presentational.
            Mock::given(method("GET"))
                .and(path("/v1/jobs"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                    "job_id": "job-1",
                    "status": "succeeded",
                    "source_path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                    "meeting_json": "{\"original_file_name\": \"ELS - 260812 - Security issue.mp4\"}",
                }])))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let rows = service
                .list_ledger_jobs(50)
                .await
                .expect("list_ledger_jobs should succeed");
            assert_eq!(
                rows[0].original_file_name.as_deref(),
                Some("ELS - 260812 - Security issue.mp4")
            );
        });
    }

    #[test]
    fn list_ledger_jobs_reads_a_compress_rows_sizes_out_of_result_json() {
        run(async {
            let server = MockServer::start().await;
            // `result_json` is a TEXT column like `meeting_json`: the manifest
            // crosses the wire as a JSON *string*.
            let manifest = serde_json::json!({
                "replaced": true,
                "path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                "encoder": "hevc_nvenc",
                "audio": "copy",
                "before_bytes": 1_347_900_000_u64,
                "after_bytes": 161_700_000_u64,
                "warning": null,
            })
            .to_string();
            Mock::given(method("GET"))
                .and(path("/v1/jobs"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
                    {
                        "job_id": "compressed",
                        "status": "succeeded",
                        "job_type": "compress",
                        "result_json": manifest,
                    },
                    // Another job type's manifest is not a compression.
                    {
                        "job_id": "exported",
                        "status": "succeeded",
                        "job_type": "export",
                        "result_json": "{\"artifacts\": []}",
                    },
                    {"job_id": "running", "status": "running", "job_type": "compress"},
                    {
                        "job_id": "odd",
                        "status": "succeeded",
                        "job_type": "compress",
                        "result_json": "not json",
                    },
                ])))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let rows = service
                .list_ledger_jobs(50)
                .await
                .expect("list_ledger_jobs should succeed");
            assert_eq!(rows[0].job_type.as_deref(), Some("compress"));
            assert_eq!(
                rows[0].compression,
                Some(LedgerCompression {
                    replaced: true,
                    encoder: Some("hevc_nvenc".to_string()),
                    before_bytes: 1_347_900_000,
                    after_bytes: 161_700_000,
                    warning: None,
                })
            );
            assert_eq!(rows[1].compression, None);
            assert_eq!(rows[2].compression, None);
            assert_eq!(rows[3].compression, None);
        });
    }

    #[test]
    fn list_ledger_jobs_a_row_without_a_meeting_json_key_still_decodes() {
        run(async {
            let server = MockServer::start().await;
            // FR-6/NFR-1: every pre-feature ledger row, and any build of F2
            // older than the column, omits the key entirely. That is absence,
            // not a decode failure.
            Mock::given(method("GET"))
                .and(path("/v1/jobs"))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                        "job_id": "job-1",
                        "status": "queued",
                        "source_path": "C:\\Meetings\\ELS\\260812 - Security issue\\source.mp4",
                    }])),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let rows = service
                .list_ledger_jobs(50)
                .await
                .expect("a row without meeting_json must decode, not fail");
            assert_eq!(rows[0].job_id, "job-1");
            assert_eq!(rows[0].original_file_name, None);
        });
    }

    #[test]
    fn list_ledger_jobs_a_malformed_meeting_json_yields_no_name_rather_than_an_error() {
        run(async {
            let server = MockServer::start().await;
            // NFR-2: not JSON, right JSON but no key, a non-string name, an
            // empty name, and JSON that is not even an object. None of these
            // may break the read path -- each row falls back to no recorded
            // name and renders via FR-3.
            Mock::given(method("GET"))
                .and(path("/v1/jobs"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
                    {"job_id": "not-json", "status": "succeeded", "meeting_json": "not json"},
                    {"job_id": "empty-object", "status": "succeeded", "meeting_json": "{}"},
                    {
                        "job_id": "non-string-name",
                        "status": "succeeded",
                        "meeting_json": "{\"original_file_name\": 42}",
                    },
                    {
                        "job_id": "empty-name",
                        "status": "succeeded",
                        "meeting_json": "{\"original_file_name\": \"\"}",
                    },
                    {"job_id": "json-but-not-an-object", "status": "succeeded", "meeting_json": "[1,2]"},
                    {"job_id": "null-column", "status": "succeeded", "meeting_json": null},
                ])))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let rows = service
                .list_ledger_jobs(50)
                .await
                .expect("a malformed meeting_json must never fail the whole listing");
            assert_eq!(rows.len(), 6);
            for row in rows {
                assert_eq!(
                    row.original_file_name, None,
                    "row {:?} must have no recorded original name",
                    row.job_id
                );
            }
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

    #[test]
    fn submit_llm_posts_a_suggest_title_job_with_exactly_its_three_keys() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("POST"))
                .and(path("/v1/jobs"))
                .and(body_json(serde_json::json!({
                    "job_type": "suggest_title",
                    "input_path": "C:\\Meetings\\ELS\\260812 - Sync",
                    "output_dir": "C:\\Meetings\\ELS\\260812 - Sync",
                })))
                .respond_with(
                    ResponseTemplate::new(202)
                        .set_body_json(serde_json::json!({"job_id": "job-7"})),
                )
                .expect(1)
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let job_id = service
                .submit_llm(super::super::LlmSubmitRequest {
                    kind: super::super::LlmJobKind::SuggestTitle,
                    input_path: "C:\\Meetings\\ELS\\260812 - Sync".to_string(),
                    output_dir: "C:\\Meetings\\ELS\\260812 - Sync".to_string(),
                    max_speakers: None,
                    speaker_match_threshold: None,
                })
                .await
                .expect("submit should succeed");
            assert_eq!(job_id, "job-7");
        });
    }

    #[test]
    fn suggested_title_reads_the_title_out_of_the_jobs_result() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/job-7/result"))
                .and(header("authorization", "Bearer secret"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({"title": "Обзор бюджета"})),
                )
                .expect(1)
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), Some("secret".to_string()))
                .expect("loopback base url must be accepted");
            let title = service
                .suggested_title("job-7")
                .await
                .expect("the result must decode");
            assert_eq!(title, "Обзор бюджета");
        });
    }

    #[test]
    fn suggested_title_maps_a_missing_result_to_an_http_error_with_the_service_message() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/job-7/result"))
                .respond_with(ResponseTemplate::new(404).set_body_json(serde_json::json!({
                    "error_kind": "invalid_request",
                    "error_message": "result not available for job job-7",
                })))
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .suggested_title("job-7")
                .await
                .expect_err("a 404 must not be read as a title");
            assert_eq!(
                err,
                ServiceError::Http {
                    status: 404,
                    message: "result not available for job job-7".to_string(),
                }
            );
        });
    }

    #[test]
    fn suggested_title_refuses_a_result_that_is_not_a_title_manifest() {
        run(async {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/v1/jobs/job-7/result"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({"artifacts": ["summary.md"]})),
                )
                .mount(&server)
                .await;

            let service = HttpTranscriptionService::new(&server.uri(), None)
                .expect("loopback base url must be accepted");
            let err = service
                .suggested_title("job-7")
                .await
                .expect_err("another job's manifest is not a title");
            assert!(matches!(err, ServiceError::Decode { .. }));
        });
    }
}
