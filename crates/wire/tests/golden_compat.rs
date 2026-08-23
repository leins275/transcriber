//! Byte-compatibility with the documents the Python service already wrote.
//!
//! The fixtures under `tests/golden/` were produced by running the *Python*
//! writers (`transcription.transcript`, `transcription.artifacts`) — the same
//! code that wrote every transcript and artifact currently sitting in a user's
//! vault. Comparing against them is what makes this a compatibility test
//! rather than a restatement of my reading of `schema.py`.
//!
//! Regenerate (while the Python service still exists) with:
//!
//! ```text
//! uv run --directory services/transcription python <generator> crates/wire/tests/golden
//! ```
//!
//! A failure here means a real vault would get documents that differ from its
//! existing ones — never "fix" it by editing the fixture.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};
use wire::artifacts;
use wire::transcript::{
    DiarizationInfo, DiarizationStatus, ProviderInfo, Segment, Source, Stats, TranscriptDoc, Word,
    SCHEMA_VERSION,
};

fn golden(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("golden")
        .join(name);
    fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("missing golden fixture {}: {e}", path.display()))
}

/// The same document `gen_golden.py` builds, field for field.
fn doc(with_extras: bool) -> TranscriptDoc {
    let segment = Segment {
        id: 0,
        start: 0.0,
        end: 12.5,
        text: "Привет, это тест.".to_string(),
        avg_logprob: Some(-0.25),
        no_speech_prob: Some(0.01),
        compression_ratio: Some(1.5),
        words: with_extras.then(|| {
            vec![
                Word {
                    word: "Привет,".to_string(),
                    start: 0.0,
                    end: 1.25,
                    probability: Some(0.98),
                },
                Word {
                    word: "это".to_string(),
                    start: 1.25,
                    end: 1.5,
                    probability: Some(0.9),
                },
            ]
        }),
        speaker: with_extras.then(|| "Speaker 1".to_string()),
    };

    TranscriptDoc {
        schema_version: SCHEMA_VERSION,
        created_at: "2026-08-23T18:00:00+00:00".to_string(),
        source: Source {
            path: "C:\\vault\\ELS\\260812 - Demo\\source.mp4".to_string(),
            filename: "source.mp4".to_string(),
            duration_sec: 12.5,
        },
        provider: ProviderInfo {
            name: "local".to_string(),
            model: "large-v3".to_string(),
            device: "cuda".to_string(),
            compute_type: "float16".to_string(),
        },
        language: Some("ru".to_string()),
        language_probability: Some(0.98),
        text: "Привет, это тест.".to_string(),
        segments: vec![segment],
        stats: Stats {
            elapsed_sec: 3.0,
            realtime_factor: 4.0,
            cost_usd: None,
            currency: None,
        },
        diarization: with_extras.then(|| DiarizationInfo {
            status: DiarizationStatus::Succeeded,
            model: "pyannote/speaker-diarization-3.1".to_string(),
            device: Some("cuda".to_string()),
            speaker_count: Some(2),
            error_kind: None,
            error_message: None,
        }),
    }
}

#[test]
fn minimal_transcript_matches_python_byte_for_byte() {
    assert_eq!(
        doc(false).to_json().unwrap(),
        golden("transcript_minimal.json")
    );
}

#[test]
fn full_transcript_with_words_and_diarization_matches_python() {
    assert_eq!(doc(true).to_json().unwrap(), golden("transcript_full.json"));
}

#[test]
fn python_transcripts_parse_back_into_the_same_document() {
    // The other direction: every document already on disk must load.
    assert_eq!(
        TranscriptDoc::from_json(&golden("transcript_minimal.json")).unwrap(),
        doc(false)
    );
    assert_eq!(
        TranscriptDoc::from_json(&golden("transcript_full.json")).unwrap(),
        doc(true)
    );
}

#[test]
fn front_matter_matches_python_byte_for_byte() {
    let mut meta = Map::new();
    meta.insert("kind".into(), json!("action_item"));
    meta.insert("meeting".into(), json!("260812 - Demo"));
    meta.insert("timestamps".into(), json!([1.5, 2.0]));
    meta.insert("owner".into(), Value::Null);
    meta.insert("done".into(), json!(false));
    meta.insert("confidence".into(), json!(0.8));
    meta.insert("title".into(), json!("Отчёт"));

    // The fixture is a file, so it carries the platform line endings the
    // Python writer produced; the renderer itself returns `\n`-joined text and
    // `atomic::write_text` does the translation.
    let expected = golden("front_matter.md").replace("\r\n", "\n");
    assert_eq!(artifacts::render_front_matter(&meta).unwrap(), expected);
}

#[test]
fn slugs_match_python_for_every_recorded_title() {
    let expected: BTreeMap<String, String> =
        serde_json::from_str(&golden("slugs.json")).expect("slugs.json parses");
    assert!(!expected.is_empty());

    for (title, want) in expected {
        assert_eq!(
            artifacts::slugify(&title, "item"),
            want,
            "slug for {title:?}"
        );
    }
}

#[test]
fn write_item_reproduces_pythons_file_bytes() {
    let root = tempfile::tempdir().expect("tempdir");
    let mut meta = Map::new();
    meta.insert("kind".into(), json!("action_item"));
    meta.insert("meeting".into(), json!("260812 - Demo"));

    let md = artifacts::write_item(
        root.path(),
        "Fix the Thing / Now",
        &meta,
        "  Do it soon.  ",
        &[("screenshot-0.png".to_string(), b"\x89PNG".to_vec())],
    )
    .expect("write_item");

    // Folder name, file name and content all come from the same slug.
    let dir_name = golden("item_dir_name.txt");
    assert_eq!(md.parent().unwrap().file_name().unwrap(), dir_name.as_str());
    assert_eq!(md.file_name().unwrap(), format!("{dir_name}.md").as_str());

    // Compared as bytes: the CRLF the Python writer's text mode produced is
    // part of what this must reproduce.
    let python_bytes = fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/golden/items")
            .join(&dir_name)
            .join(format!("{dir_name}.md")),
    )
    .expect("python item.md");
    assert_eq!(fs::read(&md).unwrap(), python_bytes);
}

#[test]
fn python_written_items_read_back_through_list_items() {
    let items = artifacts::list_items(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/golden/items"),
    );
    assert_eq!(items.len(), 1, "expected the one generated item");

    let item = &items[0];
    assert_eq!(item.meta.get("kind"), Some(&json!("action_item")));
    assert_eq!(item.meta.get("meeting"), Some(&json!("260812 - Demo")));
    assert_eq!(item.screenshot_names, vec!["screenshot-0.png"]);
    assert!(
        item.body.starts_with("# Fix the Thing / Now"),
        "body was {:?}",
        item.body
    );
}
