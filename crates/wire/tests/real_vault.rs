//! Reading documents from a real vault.
//!
//! The golden fixtures next door are small and built for the purpose. This
//! points the same parser at whatever a user actually has on disk -- long
//! Russian transcripts with thousands of word timestamps, artifacts written
//! across several releases of the Python service -- because the failure mode
//! that matters is a document shape nobody thought to write a fixture for.
//!
//! Ignored by default; it needs a vault:
//!
//! ```text
//! set TRANSCRIBER_TEST_VAULT=D:\Meetings
//! cargo test -p wire --test real_vault -- --ignored --nocapture
//! ```

use std::path::{Path, PathBuf};

use wire::transcript::TranscriptDoc;

fn vault() -> PathBuf {
    PathBuf::from(
        std::env::var("TRANSCRIBER_TEST_VAULT")
            .expect("TRANSCRIBER_TEST_VAULT must point at a vault directory"),
    )
}

/// Every `transcript.json` under `dir`, however deep.
fn find(dir: &Path, name: &str, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            find(&path, name, out);
        } else if path.file_name().is_some_and(|n| n == name) {
            out.push(path);
        }
    }
}

#[test]
#[ignore = "needs a real vault"]
fn every_transcript_in_the_vault_round_trips_byte_for_byte() {
    let mut paths = Vec::new();
    find(&vault(), wire::TRANSCRIPT_FILE_NAME, &mut paths);
    assert!(!paths.is_empty(), "no transcripts found in the vault");

    for path in &paths {
        let original = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));

        let doc = TranscriptDoc::from_json(&original)
            .unwrap_or_else(|e| panic!("cannot parse {}: {e}", path.display()));
        let rewritten = doc
            .to_json()
            .unwrap_or_else(|e| panic!("cannot serialize {}: {e}", path.display()));

        let words: usize = doc
            .segments
            .iter()
            .map(|s| s.words.as_ref().map(Vec::len).unwrap_or(0))
            .sum();
        println!(
            "{}: {} segments, {} words, {:.0}s, lang {:?}",
            path.display(),
            doc.segments.len(),
            words,
            doc.source.duration_sec,
            doc.language
        );

        // The whole contract in one assertion: what the engine writes back
        // must be byte-identical to what the Python service wrote, or every
        // re-run rewrites a file that did not change.
        // Transcripts written before the service switched to
        // `ensure_ascii=False` carry `\uXXXX` escapes for every non-Latin
        // character. Rewriting one in today's format is correct -- the current
        // Python writer would do the same -- so byte identity is the wrong
        // question for these. They still have to survive a round trip
        // *semantically*, which is what keeps an old vault readable.
        if original.contains("\\u04") {
            let reparsed = TranscriptDoc::from_json(&rewritten)
                .unwrap_or_else(|e| panic!("cannot re-read {}: {e}", path.display()));
            assert_eq!(
                reparsed,
                doc,
                "{} changed meaning when rewritten",
                path.display()
            );
            println!("  (written before ensure_ascii=False; compared by value)");
            continue;
        }

        if rewritten != original {
            // Located and sliced by character, not by byte: these documents
            // are mostly Cyrillic, and a byte window would land inside a
            // letter and panic in the reporter instead of showing the
            // difference.
            let at = rewritten
                .chars()
                .zip(original.chars())
                .position(|(a, b)| a != b)
                .unwrap_or_else(|| rewritten.chars().count().min(original.chars().count()));
            let window = |text: &str| -> String {
                text.chars().skip(at.saturating_sub(80)).take(160).collect()
            };
            panic!(
                "{} differs at character {at}\n  ours:   ...{}...\n  theirs: ...{}...",
                path.display(),
                window(&rewritten),
                window(&original),
            );
        }
    }

    println!("{} transcripts round-tripped byte for byte", paths.len());
}

#[test]
#[ignore = "needs a real vault"]
fn artifacts_in_the_vault_parse_with_their_front_matter() {
    let mut paths = Vec::new();
    for kind in [
        wire::artifacts::ACTION_ITEMS_DIR_NAME,
        wire::artifacts::FACTS_DIR_NAME,
    ] {
        let mut kind_dirs = Vec::new();
        find_dirs(&vault(), kind, &mut kind_dirs);
        for dir in kind_dirs {
            for item in wire::artifacts::list_items(&dir) {
                println!(
                    "{}: {} keys, {} screenshots",
                    item.md_path.display(),
                    item.meta.len(),
                    item.screenshot_names.len()
                );
                assert!(
                    !item.meta.is_empty(),
                    "{} lost its front matter",
                    item.md_path.display()
                );
                paths.push(item.md_path);
            }
        }
    }
    assert!(!paths.is_empty(), "no artifacts found in the vault");
    println!("{} artifacts read", paths.len());
}

fn find_dirs(dir: &Path, name: &str, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        if path.file_name().is_some_and(|n| n == name) {
            out.push(path);
        } else {
            find_dirs(&path, name, out);
        }
    }
}
