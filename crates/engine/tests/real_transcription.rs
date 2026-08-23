//! End-to-end transcription against real models.
//!
//! Ignored by default: these need a whisper model on disk and an `ffmpeg` that
//! can be executed, neither of which belongs in a unit-test run. They are the
//! tests that catch what fakes cannot -- that the parameters actually reach
//! whisper.cpp, that its segment table maps onto the transcript's shape, and
//! that the file lands where the desktop app looks for it.
//!
//! Run them with the two paths supplied:
//!
//! ```text
//! set TRANSCRIBER_TEST_MODEL=D:\models\ggml-tiny.bin
//! set TRANSCRIBER_TEST_AUDIO=D:\samples\jfk.wav
//! set TRANSCRIBER_TEST_FFMPEG=D:\ffmpeg\ffmpeg.exe
//! cargo test -p engine --test real_transcription -- --ignored --nocapture
//! ```

use std::path::PathBuf;
use std::time::{Duration, Instant};

use engine::config::{Config, Env};
use engine::jobs::{EngineHandle, JobKind, JobRequest, JobRunner, JobState};
use engine::ledger::Ledger;
use engine::media::ffmpeg::FfmpegDecoder;
use engine::models;
use engine::runner::EngineRunner;
use wire::transcript::TranscriptDoc;

fn required(var: &str) -> PathBuf {
    PathBuf::from(
        std::env::var(var)
            .unwrap_or_else(|_| panic!("{var} must point at a real file for this test")),
    )
}

/// Stage a real model into a throwaway app folder, in the layout the engine
/// expects to find it in.
fn app_dir_with_model(dir: &std::path::Path) -> Config {
    let mut env = Env::new();
    env.insert("TRANSCRIBER_APP_DIR".to_string(), dir.display().to_string());
    let config = Config::load(None, &env).expect("config");

    let target = models::whisper_model_file(&config);
    std::fs::create_dir_all(target.parent().unwrap()).expect("model dir");
    // Hard-link where possible: copying a multi-gigabyte model per test run is
    // minutes of disk for no benefit.
    if std::fs::hard_link(required("TRANSCRIBER_TEST_MODEL"), &target).is_err() {
        std::fs::copy(required("TRANSCRIBER_TEST_MODEL"), &target).expect("stage the model");
    }
    models::mark_installed(&target).expect("mark installed");
    config
}

fn decoder() -> FfmpegDecoder {
    match std::env::var("TRANSCRIBER_TEST_FFMPEG") {
        Ok(path) => FfmpegDecoder::with_program(path),
        Err(_) => FfmpegDecoder::with_program("ffmpeg"),
    }
}

#[test]
#[ignore = "needs a real whisper model and ffmpeg"]
fn a_real_recording_becomes_a_transcript_on_disk() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut config = app_dir_with_model(dir.path());
    // The canary is English and the tiny model guesses badly when left to
    // detect; the point of this test is the pipeline, not language ID.
    config.language = Some("en".to_string());
    config.device = "cpu".to_string();

    let output_dir = dir.path().join("meeting");
    std::fs::create_dir_all(&output_dir).expect("output dir");

    let ledger = Ledger::open(&config.db_path).expect("ledger");
    let runner_config = config.clone();
    let engine = EngineHandle::start(
        config,
        ledger,
        Box::new(move || {
            Box::new(EngineRunner::new(runner_config.clone()).with_decoder(Box::new(decoder())))
                as Box<dyn JobRunner>
        }),
    )
    .expect("engine");

    let job_id = engine
        .submit(JobRequest {
            kind: JobKind::Transcribe,
            input_path: required("TRANSCRIBER_TEST_AUDIO").display().to_string(),
            output_dir: output_dir.display().to_string(),
            language: None,
        })
        .expect("submit");

    let deadline = Instant::now() + Duration::from_secs(600);
    let snapshot = loop {
        let snapshot = engine.status(&job_id).expect("known job");
        if snapshot.state.is_terminal() {
            break snapshot;
        }
        assert!(Instant::now() < deadline, "transcription never finished");
        std::thread::sleep(Duration::from_millis(200));
    };

    assert_eq!(
        snapshot.state,
        JobState::Succeeded,
        "transcription failed: {:?} {:?}",
        snapshot.error_kind,
        snapshot.error_message
    );

    let path = output_dir.join(wire::TRANSCRIPT_FILE_NAME);
    let text = std::fs::read_to_string(&path).expect("transcript.json exists");
    let doc = TranscriptDoc::from_json(&text).expect("transcript parses");

    println!("transcript: {}", doc.text.trim());
    println!("segments:   {}", doc.segments.len());
    println!("duration:   {:.2}s", doc.source.duration_sec);
    println!("elapsed:    {:.2}s", doc.stats.elapsed_sec);

    // The canary's actual words, so a pipeline that silently transcribes
    // silence cannot pass.
    let spoken = doc.text.to_lowercase();
    assert!(
        spoken.contains("ask not what your country"),
        "unexpected transcript: {}",
        doc.text
    );

    assert!(!doc.segments.is_empty());
    assert_eq!(doc.provider.name, "local");
    assert_eq!(doc.language.as_deref(), Some("en"));
    assert!(doc.source.duration_sec > 5.0);

    // Ids number the transcript that was written, after every pass.
    assert_eq!(
        doc.segments.iter().map(|s| s.id).collect::<Vec<_>>(),
        (0..doc.segments.len() as i64).collect::<Vec<_>>()
    );

    // Word timestamps are what re-segmentation and the diarization vote both
    // need; their absence would quietly degrade both.
    let words: usize = doc
        .segments
        .iter()
        .map(|s| s.words.as_ref().map(Vec::len).unwrap_or(0))
        .sum();
    assert!(words > 0, "expected word timestamps");

    for segment in &doc.segments {
        assert!(segment.end >= segment.start, "{segment:?}");
        assert!(segment.avg_logprob.is_some(), "{segment:?}");
        assert!(segment.no_speech_prob.is_some(), "{segment:?}");
        assert!(segment.compression_ratio.is_some(), "{segment:?}");
    }

    let rows = engine.list_ledger_jobs(None).expect("ledger");
    let row = rows.iter().find(|r| r.job_id == job_id).expect("row");
    assert_eq!(row.status, "succeeded");
    assert_eq!(row.segment_count, Some(doc.segments.len() as i64));
    assert!(row.realtime_factor.is_some());

    engine.shutdown();
}

#[test]
#[ignore = "needs a real whisper model and ffmpeg"]
fn a_missing_model_is_reported_as_a_model_problem() {
    // The same pipeline with nothing staged: the failure the UI turns into a
    // download offer, rather than a decode error about the recording.
    let dir = tempfile::tempdir().expect("tempdir");
    let mut env = Env::new();
    env.insert(
        "TRANSCRIBER_APP_DIR".to_string(),
        dir.path().display().to_string(),
    );
    let config = Config::load(None, &env).expect("config");

    let ledger = Ledger::open(&config.db_path).expect("ledger");
    let runner_config = config.clone();
    let engine = EngineHandle::start(
        config,
        ledger,
        Box::new(move || {
            Box::new(EngineRunner::new(runner_config.clone()).with_decoder(Box::new(decoder())))
                as Box<dyn JobRunner>
        }),
    )
    .expect("engine");

    let job_id = engine
        .submit(JobRequest {
            kind: JobKind::Transcribe,
            input_path: required("TRANSCRIBER_TEST_AUDIO").display().to_string(),
            output_dir: dir.path().display().to_string(),
            language: None,
        })
        .expect("submit");

    let deadline = Instant::now() + Duration::from_secs(120);
    let snapshot = loop {
        let snapshot = engine.status(&job_id).expect("known job");
        if snapshot.state.is_terminal() {
            break snapshot;
        }
        assert!(Instant::now() < deadline, "job never finished");
        std::thread::sleep(Duration::from_millis(50));
    };

    assert_eq!(snapshot.state, JobState::Failed);
    assert_eq!(snapshot.error_kind, Some(wire::ErrorKind::ModelLoad));
    engine.shutdown();
}
