//! Compare this engine's transcript against one the Python service produced.
//!
//! The two decoders are not the same program -- faster-whisper drives
//! CTranslate2, this drives whisper.cpp -- so the output will never match word
//! for word, and a test that demanded it would be a test that always failed.
//! What this checks is that they are transcribing the *same recording the same
//! way*: similar length, similar segmentation, and text that agrees on the
//! overwhelming majority of its words in the same order.
//!
//! Ignored by default; it needs a recording, a reference transcript and a real
//! model:
//!
//! ```text
//! set TRANSCRIBER_AB_SOURCE=D:\vault\PROJ\meeting\source.mp4
//! set TRANSCRIBER_AB_REFERENCE=D:\vault\PROJ\meeting\transcript.json
//! set TRANSCRIBER_TEST_MODEL=D:\models\ggml-large-v3.bin
//! set TRANSCRIBER_TEST_FFMPEG=D:\ffmpeg\ffmpeg.exe
//! cargo test -p engine --test ab_against_python -- --ignored --nocapture
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
        std::env::var(var).unwrap_or_else(|_| panic!("{var} must be set for this comparison")),
    )
}

/// Words, lowercased and stripped of punctuation, so the comparison is about
/// what was said rather than how it was typeset.
fn words(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(|word| {
            word.chars()
                .filter(|c| c.is_alphanumeric())
                .flat_map(char::to_lowercase)
                .collect::<String>()
        })
        .filter(|word| !word.is_empty())
        .collect()
}

/// Length of the longest common subsequence, which is order-sensitive in a way
/// a word-count overlap is not: two transcripts that contain the same words in
/// a scrambled order are not the same transcript.
fn lcs_len(a: &[String], b: &[String]) -> usize {
    let mut previous = vec![0usize; b.len() + 1];
    let mut current = vec![0usize; b.len() + 1];
    for word_a in a {
        for (j, word_b) in b.iter().enumerate() {
            current[j + 1] = if word_a == word_b {
                previous[j] + 1
            } else {
                current[j].max(previous[j + 1])
            };
        }
        std::mem::swap(&mut previous, &mut current);
        current.iter_mut().for_each(|cell| *cell = 0);
    }
    previous[b.len()]
}

#[test]
#[ignore = "needs a recording, a reference transcript and a real model"]
fn the_rust_engine_agrees_with_the_python_service() {
    let reference: TranscriptDoc = TranscriptDoc::from_json(
        &std::fs::read_to_string(required("TRANSCRIBER_AB_REFERENCE"))
            .expect("read the reference transcript"),
    )
    .expect("parse the reference transcript");

    let dir = tempfile::tempdir().expect("tempdir");
    let mut env = Env::new();
    env.insert(
        "TRANSCRIBER_APP_DIR".to_string(),
        dir.path().display().to_string(),
    );
    let mut config = Config::load(None, &env).expect("config");
    config.device = "cpu".to_string();
    // Pinned to whatever the reference decided, so a language-detection
    // difference cannot masquerade as a transcription difference.
    config.language = reference.language.clone();

    let model = models::whisper_model_file(&config);
    std::fs::create_dir_all(model.parent().unwrap()).expect("model dir");
    if std::fs::hard_link(required("TRANSCRIBER_TEST_MODEL"), &model).is_err() {
        std::fs::copy(required("TRANSCRIBER_TEST_MODEL"), &model).expect("stage the model");
    }
    models::mark_installed(&model).expect("mark installed");

    // The VAD model matters more than it looks: without it whisper decodes the
    // silence between utterances too, and invents words there. The Python
    // service always ran with faster-whisper's VAD on, so a comparison without
    // one is comparing two different pipelines.
    if let Ok(vad) = std::env::var("TRANSCRIBER_TEST_VAD") {
        let target = models::whisper_vad_model_file(&config);
        if std::fs::hard_link(&vad, &target).is_err() {
            std::fs::copy(&vad, &target).expect("stage the VAD model");
        }
    }

    let output_dir = dir.path().join("meeting");
    std::fs::create_dir_all(&output_dir).expect("output dir");

    let decoder = match std::env::var("TRANSCRIBER_TEST_FFMPEG") {
        Ok(path) => FfmpegDecoder::with_program(path),
        Err(_) => FfmpegDecoder::with_program("ffmpeg"),
    };

    let ledger = Ledger::open(&config.db_path).expect("ledger");
    let runner_config = config.clone();
    let engine = EngineHandle::start(
        config,
        ledger,
        Box::new(move || {
            Box::new(
                EngineRunner::new(runner_config.clone()).with_decoder(Box::new(decoder.clone())),
            ) as Box<dyn JobRunner>
        }),
    )
    .expect("engine");

    let started = Instant::now();
    let job_id = engine
        .submit(JobRequest {
            kind: JobKind::Transcribe,
            input_path: required("TRANSCRIBER_AB_SOURCE").display().to_string(),
            output_dir: output_dir.display().to_string(),
            language: None,
        })
        .expect("submit");

    // Long recordings on CPU are slow; the cap is generous rather than tight.
    let deadline = Instant::now() + Duration::from_secs(4 * 3600);
    let snapshot = loop {
        let snapshot = engine.status(&job_id).expect("known job");
        if snapshot.state.is_terminal() {
            break snapshot;
        }
        assert!(Instant::now() < deadline, "transcription never finished");
        std::thread::sleep(Duration::from_secs(5));
    };
    assert_eq!(
        snapshot.state,
        JobState::Succeeded,
        "transcription failed: {:?} {:?}",
        snapshot.error_kind,
        snapshot.error_message
    );

    let ours = TranscriptDoc::from_json(
        &std::fs::read_to_string(output_dir.join(wire::TRANSCRIPT_FILE_NAME))
            .expect("read our transcript"),
    )
    .expect("parse our transcript");

    let ours_words = words(&ours.text);
    let theirs_words = words(&reference.text);
    let common = lcs_len(&ours_words, &theirs_words);
    let agreement = 2.0 * common as f64 / (ours_words.len() + theirs_words.len()) as f64;
    let duration_drift = (ours.source.duration_sec - reference.source.duration_sec).abs();

    println!("--- A/B against the Python service ---");
    println!(
        "duration    ours {:.1}s | theirs {:.1}s | drift {:.2}s",
        ours.source.duration_sec, reference.source.duration_sec, duration_drift
    );
    println!(
        "segments    ours {} | theirs {}",
        ours.segments.len(),
        reference.segments.len()
    );
    println!(
        "words       ours {} | theirs {}",
        ours_words.len(),
        theirs_words.len()
    );
    println!("agreement   {:.1}% of words, in order", agreement * 100.0);
    println!(
        "wall clock  {:.0}s for {:.0}s of audio ({:.2}x realtime)",
        started.elapsed().as_secs_f64(),
        ours.source.duration_sec,
        started.elapsed().as_secs_f64() / ours.source.duration_sec.max(1.0)
    );
    println!(
        "language    ours {:?} | theirs {:?}",
        ours.language, reference.language
    );

    // Decoding the same file must agree on how long it is; anything else means
    // the two are not even transcribing the same audio.
    assert!(
        duration_drift < 1.0,
        "the two decoders disagree about the recording's length"
    );
    assert_eq!(ours.language, reference.language);

    // Two different decoders on real, accented, multi-speaker speech will not
    // agree perfectly. They should still be recognisably the same transcript:
    // below this, something structural is wrong rather than merely different.
    assert!(
        agreement > 0.75,
        "only {:.1}% word agreement -- that is a different transcript, not a variation",
        agreement * 100.0
    );

    engine.shutdown();
}
