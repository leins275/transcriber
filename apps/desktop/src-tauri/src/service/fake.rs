//! In-memory `TranscriptionService` fake (FR-12, FR-13, FR-14).
//!
//! `FakeService` walks `Queued -> Running(progress) -> Done|Failed` purely
//! from a per-job poll counter — no background task, no timer, no socket —
//! so every other task's tests can drive the whole UI flow deterministically
//! without a running F2 process.

use std::collections::HashMap;
use std::sync::Mutex;

use async_trait::async_trait;
use uuid::Uuid;

use super::{
    ChatEvent, ChatRequest, DiarizationStatus, IndexStatus, JobState, JobStatus, LlmCatalogModel,
    LlmModelsStatus, LlmSubmitRequest, ModelDownloadState, ModelDownloadStatus, SearchHit,
    SearchQuery, ServiceError, ServiceHealth, SubmitRequest, TranscriptionService,
};

/// The simulated curated catalog: `(id, label, file, size_bytes)` -- mirrors
/// F2's `llm_catalog.CATALOG` (deliberately a single model; no switching).
const FAKE_LLM_CATALOG: [(&str, &str, &str, u64); 1] = [(
    "qwen3.5-9b",
    "Qwen3.5 9B",
    "Qwen3.5-9B-Q5_K_M.gguf",
    6_577_841_376,
)];

/// How many `status()` polls a scripted job spends in each phase.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FakeTiming {
    /// Polls spent reporting `Queued` before advancing to `Running`.
    pub queued_polls: u32,
    /// Polls spent reporting `Running` (progress climbs linearly across
    /// these) before advancing to the job's terminal state.
    pub running_polls: u32,
}

impl Default for FakeTiming {
    fn default() -> Self {
        FakeTiming {
            queued_polls: 1,
            running_polls: 3,
        }
    }
}

/// The terminal state a scripted job resolves to.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ScriptedOutcome {
    Succeed,
    Fail(String),
}

struct ScriptedJob {
    outcome: ScriptedOutcome,
    timing: FakeTiming,
    polls: u32,
}

/// In-memory model-download simulation (T13, FR-12, FR-16, FR-17). Each
/// `model_download_status()` poll advances the transfer by one simulated
/// chunk so a caller that polls repeatedly sees monotonically increasing
/// progress, mirroring F2's real behaviour without a background thread or a
/// timer.
struct FakeModelDownload {
    present: bool,
    state: ModelDownloadState,
    downloaded_bytes: u64,
    total_bytes: u64,
    /// How many `model_download_status()` polls a started transfer spends
    /// downloading before landing on its terminal state.
    downloading_polls: u32,
    polls: u32,
    /// Script the next transfer to fail once it reaches its terminal poll,
    /// consumed by exactly one `start_model_download()` (mirrors
    /// `FakeTiming`'s own next-outcome pattern above).
    fail_next: bool,
    /// E13: whether the simulated CUDA runtime is present -- mirrors F2's
    /// `/health`'s `cuda_runtime_present` (this fake never simulates a
    /// GPU-less host, so it is never `None`).
    cuda_runtime_present: bool,
    /// E13: set by [`FakeService::set_cuda_warning`] to simulate a
    /// `SetupDownload` whose CUDA-runtime phase failed and continued into
    /// the model phase anyway (E4) -- carried verbatim on every status until
    /// cleared.
    cuda_warning: Option<String>,
}

impl FakeModelDownload {
    fn present() -> Self {
        FakeModelDownload {
            present: true,
            state: ModelDownloadState::Complete,
            downloaded_bytes: 3_000_000_000,
            total_bytes: 3_000_000_000,
            downloading_polls: 3,
            polls: 0,
            fail_next: false,
            cuda_runtime_present: true,
            cuda_warning: None,
        }
    }

    fn absent() -> Self {
        FakeModelDownload {
            present: false,
            state: ModelDownloadState::Idle,
            downloaded_bytes: 0,
            total_bytes: 3_000_000_000,
            downloading_polls: 3,
            polls: 0,
            fail_next: false,
            cuda_runtime_present: true,
            cuda_warning: None,
        }
    }

    fn start(&mut self) {
        // F2 never starts a second parallel transfer -- a start while one
        // is already running is a no-op that just returns the current
        // status (see `status()` below, called by every trait method).
        if matches!(
            self.state,
            ModelDownloadState::Downloading | ModelDownloadState::Verifying
        ) {
            return;
        }
        self.state = ModelDownloadState::Downloading;
        self.downloaded_bytes = 0;
        self.polls = 0;
    }

    fn cancel(&mut self) {
        if matches!(
            self.state,
            ModelDownloadState::Downloading | ModelDownloadState::Verifying
        ) {
            self.state = ModelDownloadState::Cancelled;
        }
    }

    /// Advances the simulated transfer by one poll (if currently
    /// downloading) and returns the resulting status -- only the `GET`
    /// (`model_download_status`) trait method calls this; `start`/`cancel`
    /// return [`FakeModelDownload::peek`] instead so a `POST` on an
    /// already-running transfer (F2's documented no-op) never itself
    /// advances progress.
    fn advance_and_peek(&mut self) -> ModelDownloadStatus {
        if self.state == ModelDownloadState::Downloading {
            self.polls += 1;
            let capped = self.polls.min(self.downloading_polls);
            self.downloaded_bytes =
                self.total_bytes * u64::from(capped) / u64::from(self.downloading_polls.max(1));
            if self.polls >= self.downloading_polls {
                if self.fail_next {
                    self.fail_next = false;
                    self.state = ModelDownloadState::Error;
                } else {
                    self.state = ModelDownloadState::Complete;
                    self.present = true;
                }
            }
        }
        self.peek()
    }

    /// Reads the current status without mutating anything.
    fn peek(&self) -> ModelDownloadStatus {
        let (error_kind, error_message) = if self.state == ModelDownloadState::Error {
            (
                Some("checksum_mismatch".to_string()),
                Some("simulated fake-service download failure".to_string()),
            )
        } else {
            (None, None)
        };
        let percent = if self.total_bytes > 0 {
            self.downloaded_bytes as f64 / self.total_bytes as f64 * 100.0
        } else {
            0.0
        };
        ModelDownloadStatus {
            state: self.state,
            downloaded_bytes: self.downloaded_bytes,
            total_bytes: self.total_bytes,
            percent,
            error_kind,
            error_message,
            cuda_warning: self.cuda_warning.clone(),
        }
    }
}

struct Inner {
    down: bool,
    timing: FakeTiming,
    next_outcome: ScriptedOutcome,
    jobs: HashMap<String, ScriptedJob>,
    model: FakeModelDownload,
    /// The curated LLM catalog's per-model slots (`FAKE_LLM_CATALOG` order);
    /// each is its own independent simulated transfer. All present by
    /// default so `--fake-service` dev sessions can exercise the LLM job
    /// flow without a download step.
    llm_models: Vec<(String, FakeModelDownload)>,
    /// Which catalog id is active -- the slot the legacy
    /// `llm_model_download_*` trio and `/health.llm_model_present` report.
    llm_active: String,
    /// The search-embedding GGUF's own simulated slot (bge-m3): present by
    /// default, like the LLM slots, so dev sessions search with vectors.
    embedding: FakeModelDownload,
    /// Speaker identification's two simulated slots (the pyannote/torch
    /// runtime and the pinned model snapshots), plus the switch and the
    /// token flag `/v1/diarization/status` reports. All ready by default
    /// so dev sessions can exercise the "Identify speakers" flow.
    diarization_runtime: FakeModelDownload,
    diarization_model: FakeModelDownload,
    diarize_enabled: bool,
    hf_token_present: bool,
    /// Every derived-job submission this fake accepted, for assertions.
    llm_submissions: Vec<LlmSubmitRequest>,
    /// Every transcription submission this fake accepted, for assertions --
    /// the only place a caller can observe what actually went on the wire
    /// (per-job `language`, FR-5), since `submit()` itself only returns an id.
    submissions: Vec<SubmitRequest>,
    /// How many fire-and-forget `submit_index` calls arrived, for assertions.
    index_submissions: usize,
}

impl Inner {
    fn active_llm_slot(&mut self) -> &mut FakeModelDownload {
        let active = self.llm_active.clone();
        self.llm_slot(&active)
            .expect("the active llm id always has a slot")
    }

    fn llm_slot(&mut self, model_id: &str) -> Result<&mut FakeModelDownload, ServiceError> {
        self.llm_models
            .iter_mut()
            .find(|(id, _)| id == model_id)
            .map(|(_, slot)| slot)
            .ok_or_else(|| ServiceError::Http {
                status: 400,
                message: format!("unknown llm model {model_id:?}"),
            })
    }

    /// The catalog listing, advancing any in-flight simulated transfer by
    /// one chunk (the same poll-driven convention as
    /// `FakeModelDownload::advance_and_peek`).
    fn llm_models_status(&mut self, advance: bool) -> LlmModelsStatus {
        let active = self.llm_active.clone();
        let models = FAKE_LLM_CATALOG
            .iter()
            .map(|(id, label, file, size_bytes)| {
                let slot = self.llm_slot(id).expect("catalog ids always have a slot");
                let download = if advance {
                    slot.advance_and_peek()
                } else {
                    slot.peek()
                };
                LlmCatalogModel {
                    id: (*id).to_string(),
                    label: (*label).to_string(),
                    file: (*file).to_string(),
                    size_bytes: Some(*size_bytes),
                    catalog: true,
                    present: slot.present,
                    active: *id == active,
                    download,
                }
            })
            .collect();
        LlmModelsStatus { active, models }
    }
}

/// In-memory fake used by tests and by `--fake-service` dev mode (T11).
pub struct FakeService {
    inner: Mutex<Inner>,
}

impl FakeService {
    /// A healthy fake whose jobs succeed, using [`FakeTiming::default`].
    pub fn new() -> Self {
        Self::with_timing(FakeTiming::default())
    }

    /// A healthy fake whose jobs succeed, on the given timing. The
    /// simulated model starts out present (dev-mode default: behaves like
    /// an already-installed app) -- use [`FakeService::with_model_absent`]
    /// to simulate a fresh install for T13's own tests.
    pub fn with_timing(timing: FakeTiming) -> Self {
        FakeService {
            inner: Mutex::new(Inner {
                down: false,
                timing,
                next_outcome: ScriptedOutcome::Succeed,
                jobs: HashMap::new(),
                model: FakeModelDownload::present(),
                llm_models: FAKE_LLM_CATALOG
                    .iter()
                    .map(|(id, _, _, _)| (id.to_string(), FakeModelDownload::present()))
                    .collect(),
                llm_active: FAKE_LLM_CATALOG[0].0.to_string(),
                embedding: FakeModelDownload::present(),
                diarization_runtime: FakeModelDownload::present(),
                diarization_model: FakeModelDownload::present(),
                diarize_enabled: true,
                hf_token_present: true,
                llm_submissions: Vec::new(),
                submissions: Vec::new(),
                index_submissions: 0,
            }),
        }
    }

    /// A healthy fake on which speaker identification has not been set up
    /// yet: no runtime, no models, no token, switched off -- the state a
    /// fresh install's Settings row starts from.
    pub fn with_diarization_absent() -> Self {
        let fake = Self::new();
        {
            let mut inner = fake.inner.lock().expect("fake service mutex poisoned");
            inner.diarization_runtime = FakeModelDownload::absent();
            inner.diarization_model = FakeModelDownload::absent();
            inner.diarize_enabled = false;
            inner.hf_token_present = false;
        }
        fake
    }

    /// A healthy fake whose simulated model is *not yet* present (T13,
    /// FR-17) -- the first-run "model missing" case.
    pub fn with_model_absent() -> Self {
        let fake = Self::new();
        fake.inner
            .lock()
            .expect("fake service mutex poisoned")
            .model = FakeModelDownload::absent();
        fake
    }

    /// A healthy fake whose simulated *LLM* models are not yet present --
    /// the first-use "download the GGUF" case (every catalog slot absent).
    pub fn with_llm_model_absent() -> Self {
        let fake = Self::new();
        {
            let mut inner = fake.inner.lock().expect("fake service mutex poisoned");
            for (_, slot) in &mut inner.llm_models {
                *slot = FakeModelDownload::absent();
            }
        }
        fake
    }

    /// A healthy fake whose simulated *embedding* model is not yet present
    /// -- the "enable vector search" first-use case.
    pub fn with_embedding_model_absent() -> Self {
        let fake = Self::new();
        fake.inner
            .lock()
            .expect("fake service mutex poisoned")
            .embedding = FakeModelDownload::absent();
        fake
    }

    /// Every transcription submission this fake has accepted, in order.
    pub fn submissions(&self) -> Vec<SubmitRequest> {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .submissions
            .clone()
    }

    /// How many `submit_index` calls this fake has accepted.
    pub fn index_submission_count(&self) -> usize {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .index_submissions
    }

    /// Every derived-job submission this fake has accepted, in order.
    pub fn llm_submissions(&self) -> Vec<LlmSubmitRequest> {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .llm_submissions
            .clone()
    }

    /// Script the *next* started model transfer to fail once it reaches its
    /// terminal poll (mirrors [`FakeService::queue_failure`] for jobs).
    pub fn queue_model_download_failure(&self) {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .model
            .fail_next = true;
    }

    /// Simulate a `SetupDownload` whose CUDA-runtime phase failed and
    /// continued into the model phase anyway (E4, E13): `cuda_runtime_present`
    /// flips to `false` and `message` is carried verbatim on every
    /// subsequent health/status call, until a fresh [`FakeService`] is built.
    pub fn set_cuda_warning(&self, message: impl Into<String>) {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cuda_runtime_present = false;
        inner.model.cuda_warning = Some(message.into());
    }

    /// Simulate the *durable* CUDA-missing case (E13): a fresh sidecar
    /// process after an earlier run's failed CUDA download -- no
    /// `SetupDownload` instance survives a restart, so `cuda_warning` is
    /// `None`, but `/health.cuda_runtime_present` still durably reports the
    /// runtime missing.
    pub fn set_cuda_runtime_missing(&self) {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cuda_runtime_present = false;
    }

    /// Mark the fake up or down. While down, `health()` and `submit()`
    /// return `ServiceError::Unavailable`, but jobs submitted before going
    /// down keep reporting status (FR-13) — `status()` never consults this
    /// flag.
    pub fn set_down(&self, down: bool) {
        self.inner.lock().expect("fake service mutex poisoned").down = down;
    }

    /// Script the *next* `submit()`'s job to fail with `message` verbatim
    /// once it reaches its terminal poll. Consumed by exactly one
    /// `submit()` call; subsequent jobs default back to succeeding.
    pub fn queue_failure(&self, message: impl Into<String>) {
        self.inner
            .lock()
            .expect("fake service mutex poisoned")
            .next_outcome = ScriptedOutcome::Fail(message.into());
    }
}

impl Default for FakeService {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl TranscriptionService for FakeService {
    async fn health(&self) -> Result<ServiceHealth, ServiceError> {
        let inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        Ok(ServiceHealth {
            ready: true,
            detail: None,
            model_present: inner.model.present,
            // The fake never simulates a GPU-less host: `Some(false)`
            // matches its default "no runtime downloaded yet" state, and
            // flips to `Some(true)` once a simulated CUDA phase completes
            // (`FakeModelDownload::advance_and_peek`, T13's own convention
            // for `present`/`model_present`).
            cuda_runtime_present: Some(inner.model.cuda_runtime_present),
            llm_model_present: Some(
                inner
                    .llm_models
                    .iter()
                    .find(|(id, _)| *id == inner.llm_active)
                    .map(|(_, slot)| slot.present)
                    .unwrap_or(false),
            ),
            // The fake behaves like a machine whose GPU build is already
            // fetched -- dev sessions exercise the happy path by default.
            llm_gpu_build_present: Some(true),
            embedding_model_present: Some(inner.embedding.present),
        })
    }

    async fn submit(&self, req: SubmitRequest) -> Result<String, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        inner.submissions.push(req);
        let job_id = Uuid::new_v4().to_string();
        let outcome = std::mem::replace(&mut inner.next_outcome, ScriptedOutcome::Succeed);
        let timing = inner.timing;
        inner.jobs.insert(
            job_id.clone(),
            ScriptedJob {
                outcome,
                timing,
                polls: 0,
            },
        );
        Ok(job_id)
    }

    async fn status(&self, job_id: &str) -> Result<JobStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        let job = inner
            .jobs
            .get_mut(job_id)
            .ok_or_else(|| ServiceError::Http {
                status: 404,
                message: format!("unknown job id {job_id}"),
            })?;

        let poll = job.polls;
        job.polls += 1;

        if poll < job.timing.queued_polls {
            return Ok(JobStatus {
                state: JobState::Queued,
                progress: 0.0,
                error_kind: None,
                error_message: None,
            });
        }

        let running_elapsed = poll - job.timing.queued_polls;
        if running_elapsed < job.timing.running_polls {
            let progress = if job.timing.running_polls == 0 {
                1.0
            } else {
                f64::from(running_elapsed) / f64::from(job.timing.running_polls)
            };
            return Ok(JobStatus {
                state: JobState::Running,
                progress,
                error_kind: None,
                error_message: None,
            });
        }

        match &job.outcome {
            ScriptedOutcome::Succeed => Ok(JobStatus {
                state: JobState::Done,
                progress: 1.0,
                error_kind: None,
                error_message: None,
            }),
            ScriptedOutcome::Fail(message) => Ok(JobStatus {
                state: JobState::Failed,
                progress: 1.0,
                error_kind: None,
                error_message: Some(message.clone()),
            }),
        }
    }

    async fn model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.model.advance_and_peek())
    }

    async fn start_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.start();
        Ok(inner.model.peek())
    }

    async fn cancel_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.model.cancel();
        Ok(inner.model.peek())
    }

    async fn submit_llm(&self, req: LlmSubmitRequest) -> Result<String, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        let job_id = Uuid::new_v4().to_string();
        let outcome = std::mem::replace(&mut inner.next_outcome, ScriptedOutcome::Succeed);
        let timing = inner.timing;
        inner.llm_submissions.push(req);
        inner.jobs.insert(
            job_id.clone(),
            ScriptedJob {
                outcome,
                timing,
                polls: 0,
            },
        );
        Ok(job_id)
    }

    async fn submit_index(&self) -> Result<String, ServiceError> {
        // Accepted, counted and forgotten, like the real service's cheap
        // incremental pass -- enough for `--fake-service` dev sessions to
        // not log errors and for chain tests to observe the trigger.
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        inner.index_submissions += 1;
        Ok("fake-index".to_string())
    }

    async fn search(&self, _query: SearchQuery) -> Result<Vec<SearchHit>, ServiceError> {
        // The fake has no vault to index, and the command layer drops hits
        // it cannot map to a listed meeting anyway -- "no matches" is the
        // honest, error-free answer for `--fake-service` sessions.
        let inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        Ok(Vec::new())
    }

    async fn index_status(&self, project: &str) -> Result<IndexStatus, ServiceError> {
        // An honest empty state: the fake indexes nothing.
        let inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        Ok(IndexStatus {
            project: project.to_string(),
            updated_at: None,
            indexing: false,
            progress: None,
            indexed_count: 0,
            total_count: 0,
            meetings: Vec::new(),
        })
    }

    async fn chat_stream(
        &self,
        req: ChatRequest,
        on_event: Box<dyn Fn(ChatEvent) + Send + Sync>,
        _cancel: tokio::sync::oneshot::Receiver<()>,
    ) -> Result<(), ServiceError> {
        // A scripted two-delta answer echoing the question, so the whole
        // chat UI is drivable under `--fake-service` without any model.
        {
            let inner = self.inner.lock().expect("fake service mutex poisoned");
            if inner.down {
                return Err(ServiceError::Unavailable {
                    detail: "fake service is down".to_string(),
                });
            }
        }
        let question = req
            .messages
            .last()
            .map(|message| message.content.clone())
            .unwrap_or_default();
        on_event(ChatEvent::Sources {
            sources: Vec::new(),
        });
        on_event(ChatEvent::Delta {
            text: "The fake service heard: ".to_string(),
        });
        on_event(ChatEvent::Delta { text: question });
        on_event(ChatEvent::Done {
            finish_reason: "stop".to_string(),
        });
        Ok(())
    }

    async fn llm_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.active_llm_slot().advance_and_peek())
    }

    async fn start_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        let slot = inner.active_llm_slot();
        slot.start();
        Ok(slot.peek())
    }

    async fn cancel_llm_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        let slot = inner.active_llm_slot();
        slot.cancel();
        Ok(slot.peek())
    }

    async fn embedding_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.embedding.advance_and_peek())
    }

    async fn start_embedding_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.embedding.start();
        Ok(inner.embedding.peek())
    }

    async fn cancel_embedding_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.embedding.cancel();
        Ok(inner.embedding.peek())
    }

    async fn diarization_status(&self) -> Result<DiarizationStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if inner.down {
            return Err(ServiceError::Unavailable {
                detail: "fake service is down".to_string(),
            });
        }
        // Peeking advances a started transfer, the way a status poll on
        // the slot itself would -- so a UI polling only this endpoint
        // still sees the simulated fetch land.
        let runtime_present = inner.diarization_runtime.advance_and_peek().state
            == ModelDownloadState::Complete
            || inner.diarization_runtime.present;
        let model_present = inner.diarization_model.advance_and_peek().state
            == ModelDownloadState::Complete
            || inner.diarization_model.present;
        Ok(DiarizationStatus {
            runtime_present,
            model_present,
            token_present: inner.hf_token_present,
            enabled: inner.diarize_enabled,
            gpu_present: true,
            runtime_total_bytes: 2_700_000_000,
        })
    }

    async fn diarization_runtime_download_status(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.diarization_runtime.advance_and_peek())
    }

    async fn start_diarization_runtime_download(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.diarization_runtime.start();
        Ok(inner.diarization_runtime.peek())
    }

    async fn cancel_diarization_runtime_download(
        &self,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.diarization_runtime.cancel();
        Ok(inner.diarization_runtime.peek())
    }

    async fn diarization_model_download_status(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.diarization_model.advance_and_peek())
    }

    async fn start_diarization_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.diarization_model.start();
        Ok(inner.diarization_model.peek())
    }

    async fn cancel_diarization_model_download(&self) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        inner.diarization_model.cancel();
        Ok(inner.diarization_model.peek())
    }

    async fn llm_models(&self) -> Result<LlmModelsStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        Ok(inner.llm_models_status(true))
    }

    async fn start_llm_model_download_for(
        &self,
        model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        // One transfer at a time across the whole catalog, like F2.
        let busy = inner.llm_models.iter().find(|(id, slot)| {
            id != model_id
                && matches!(
                    slot.state,
                    ModelDownloadState::Downloading | ModelDownloadState::Verifying
                )
        });
        if let Some((busy_id, _)) = busy {
            return Err(ServiceError::Http {
                status: 400,
                message: format!("another model ({busy_id:?}) is downloading"),
            });
        }
        let slot = inner.llm_slot(model_id)?;
        slot.start();
        Ok(slot.peek())
    }

    async fn cancel_llm_model_download_for(
        &self,
        model_id: &str,
    ) -> Result<ModelDownloadStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        let slot = inner.llm_slot(model_id)?;
        slot.cancel();
        Ok(slot.peek())
    }

    async fn delete_llm_model(&self, model_id: &str) -> Result<LlmModelsStatus, ServiceError> {
        let mut inner = self.inner.lock().expect("fake service mutex poisoned");
        if model_id == inner.llm_active {
            return Err(ServiceError::Http {
                status: 400,
                message: "cannot delete the active model; select another model first".to_string(),
            });
        }
        let slot = inner.llm_slot(model_id)?;
        if matches!(
            slot.state,
            ModelDownloadState::Downloading | ModelDownloadState::Verifying
        ) {
            return Err(ServiceError::Http {
                status: 400,
                message: "cannot delete a model while it is downloading".to_string(),
            });
        }
        slot.present = false;
        slot.state = ModelDownloadState::Idle;
        slot.downloaded_bytes = 0;
        Ok(inner.llm_models_status(false))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_multi_thread()
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
        }
    }

    #[test]
    fn health_on_healthy_fake_returns_ready() {
        run(async {
            let fake = FakeService::new();
            let health = fake.health().await.expect("health should succeed");
            assert!(health.ready);
        });
    }

    #[test]
    fn submit_then_status_walks_queued_running_done_with_nondecreasing_progress() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 1,
                running_polls: 4,
            });
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            let first = fake.status(&job_id).await.expect("status should succeed");
            assert_eq!(first.state, JobState::Queued);

            let mut last_progress = -1.0;
            let mut saw_running = false;
            let mut terminal = None;
            for _ in 0..10 {
                let snapshot = fake.status(&job_id).await.expect("status should succeed");
                match snapshot.state {
                    JobState::Running => {
                        saw_running = true;
                        assert!(
                            snapshot.progress >= last_progress,
                            "progress must never decrease: {} then {}",
                            last_progress,
                            snapshot.progress
                        );
                        last_progress = snapshot.progress;
                    }
                    JobState::Done => {
                        terminal = Some(snapshot);
                        break;
                    }
                    other => panic!("unexpected state before terminal: {other:?}"),
                }
            }

            assert!(saw_running, "job must pass through Running");
            let terminal = terminal.expect("job must reach Done");
            assert_eq!(terminal.progress, 1.0);
            assert_eq!(terminal.error_message, None);
        });
    }

    #[test]
    fn failing_fake_reports_failed_with_verbatim_provider_message() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 1,
            });
            fake.queue_failure("provider exploded: disk full");
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            // poll through Running, then land on the terminal Failed poll.
            let mut terminal = None;
            for _ in 0..5 {
                let snapshot = fake.status(&job_id).await.expect("status should succeed");
                if snapshot.state == JobState::Failed {
                    terminal = Some(snapshot);
                    break;
                }
            }

            let terminal = terminal.expect("job must reach Failed");
            assert_eq!(
                terminal.error_message.as_deref(),
                Some("provider exploded: disk full")
            );
        });
    }

    #[test]
    fn down_fake_rejects_health_and_submit_but_existing_jobs_still_report_status() {
        run(async {
            let fake = FakeService::with_timing(FakeTiming {
                queued_polls: 0,
                running_polls: 0,
            });
            let job_id = fake.submit(request()).await.expect("submit should succeed");

            fake.set_down(true);

            assert!(matches!(
                fake.health().await,
                Err(ServiceError::Unavailable { .. })
            ));
            assert!(matches!(
                fake.submit(request()).await,
                Err(ServiceError::Unavailable { .. })
            ));

            // The job submitted before going down still reports status.
            let snapshot = fake.status(&job_id).await.expect("status should succeed");
            assert_eq!(snapshot.state, JobState::Done);
        });
    }

    #[test]
    fn job_state_from_wire_collapses_five_states_to_four() {
        assert_eq!(JobState::from_wire("queued"), Some(JobState::Queued));
        assert_eq!(JobState::from_wire("running"), Some(JobState::Running));
        assert_eq!(JobState::from_wire("succeeded"), Some(JobState::Done));
        assert_eq!(JobState::from_wire("failed"), Some(JobState::Failed));
        assert_eq!(JobState::from_wire("cancelled"), Some(JobState::Failed));
        assert_eq!(JobState::from_wire("bogus"), None);
    }

    #[test]
    fn job_status_from_wire_forces_cancelled_message_and_passes_others_through() {
        let succeeded = JobStatus::from_wire("succeeded", 1.0, None, None)
            .expect("succeeded must map to a JobStatus");
        assert_eq!(succeeded.state, JobState::Done);
        assert_eq!(succeeded.error_message, None);

        let cancelled = JobStatus::from_wire("cancelled", 0.4, None, None)
            .expect("cancelled must map to a JobStatus");
        assert_eq!(cancelled.state, JobState::Failed);
        assert_eq!(cancelled.error_message.as_deref(), Some("cancelled"));

        let failed = JobStatus::from_wire(
            "failed",
            0.9,
            Some("provider_unavailable".to_string()),
            Some("provider is unavailable".to_string()),
        )
        .expect("failed must map to a JobStatus");
        assert_eq!(failed.state, JobState::Failed);
        assert_eq!(failed.error_kind.as_deref(), Some("provider_unavailable"));
        assert_eq!(
            failed.error_message.as_deref(),
            Some("provider is unavailable")
        );

        assert_eq!(JobState::from_wire("bogus"), None);
        assert_eq!(JobStatus::from_wire("bogus", 0.0, None, None), None);
    }

    // -- model download simulation (T13, FR-12, FR-16, FR-17) -------------

    #[test]
    fn a_default_fake_reports_the_model_present() {
        run(async {
            let fake = FakeService::new();
            let health = fake.health().await.expect("health should succeed");
            assert!(health.model_present);
        });
    }

    #[test]
    fn with_model_absent_reports_idle_and_not_present_until_started() {
        run(async {
            let fake = FakeService::with_model_absent();
            let health = fake.health().await.expect("health should succeed");
            assert!(!health.model_present);
            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(status.state, ModelDownloadState::Idle);
        });
    }

    #[test]
    fn starting_a_download_walks_downloading_to_complete_with_nondecreasing_bytes_and_flips_model_present(
    ) {
        run(async {
            let fake = FakeService::with_model_absent();
            let first = fake
                .start_model_download()
                .await
                .expect("start_model_download should succeed");
            assert_eq!(first.state, ModelDownloadState::Downloading);

            let mut last_downloaded = -1i64;
            let mut terminal = None;
            for _ in 0..10 {
                let status = fake
                    .model_download_status()
                    .await
                    .expect("model_download_status should succeed");
                assert!(
                    i64::try_from(status.downloaded_bytes).unwrap() >= last_downloaded,
                    "downloaded_bytes must never decrease"
                );
                last_downloaded = i64::try_from(status.downloaded_bytes).unwrap();
                if status.state == ModelDownloadState::Complete {
                    terminal = Some(status);
                    break;
                }
            }

            let terminal = terminal.expect("transfer must reach Complete");
            assert_eq!(terminal.downloaded_bytes, terminal.total_bytes);
            let health = fake.health().await.expect("health should succeed");
            assert!(
                health.model_present,
                "model_present must flip true once the simulated transfer completes"
            );
        });
    }

    #[test]
    fn a_second_start_while_downloading_does_not_restart_the_transfer() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");
            let after_one_poll = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert!(after_one_poll.downloaded_bytes > 0);

            // A second start while already downloading is a no-op -- it
            // must not reset progress back to zero (F2: never a parallel
            // transfer).
            let second = fake
                .start_model_download()
                .await
                .expect("start_model_download should succeed");
            assert_eq!(second.state, ModelDownloadState::Downloading);
            assert!(second.downloaded_bytes >= after_one_poll.downloaded_bytes);
        });
    }

    #[test]
    fn cancel_leaves_a_retryable_cancelled_state() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");
            let cancelled = fake
                .cancel_model_download()
                .await
                .expect("cancel_model_download should succeed");
            assert_eq!(cancelled.state, ModelDownloadState::Cancelled);

            // Retryable: starting again begins a fresh transfer.
            let retried = fake
                .start_model_download()
                .await
                .expect("a cancelled transfer must be retryable");
            assert_eq!(retried.state, ModelDownloadState::Downloading);
        });
    }

    #[test]
    fn queue_model_download_failure_reports_a_verbatim_error_and_never_marks_the_model_present() {
        run(async {
            let fake = FakeService::with_model_absent();
            fake.queue_model_download_failure();
            fake.start_model_download()
                .await
                .expect("start_model_download should succeed");

            let mut terminal = None;
            for _ in 0..10 {
                let status = fake
                    .model_download_status()
                    .await
                    .expect("model_download_status should succeed");
                if status.state == ModelDownloadState::Error {
                    terminal = Some(status);
                    break;
                }
            }

            let terminal = terminal.expect("transfer must reach Error");
            assert!(terminal.error_message.is_some());
            let health = fake.health().await.expect("health should succeed");
            assert!(
                !health.model_present,
                "a failed transfer must never be reported as present"
            );
        });
    }

    #[test]
    fn set_cuda_runtime_missing_flips_cuda_runtime_present_without_a_warning() {
        run(async {
            let fake = FakeService::new();

            fake.set_cuda_runtime_missing();

            let health = fake.health().await.expect("health should succeed");
            assert_eq!(health.cuda_runtime_present, Some(false));
            assert!(health.model_present);

            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(
                status.cuda_warning, None,
                "a fresh process's durable CUDA-missing state carries no in-session warning"
            );
        });
    }

    #[test]
    fn set_cuda_warning_flips_cuda_runtime_present_and_carries_the_message_verbatim() {
        run(async {
            let fake = FakeService::new();
            let health_before = fake.health().await.expect("health should succeed");
            assert_eq!(health_before.cuda_runtime_present, Some(true));

            fake.set_cuda_warning("digest mismatch for nvidia_cublas_cu12.whl");

            let health_after = fake.health().await.expect("health should succeed");
            assert_eq!(health_after.cuda_runtime_present, Some(false));
            assert!(health_after.model_present);

            let status = fake
                .model_download_status()
                .await
                .expect("model_download_status should succeed");
            assert_eq!(
                status.cuda_warning.as_deref(),
                Some("digest mismatch for nvidia_cublas_cu12.whl")
            );
        });
    }
}
