//! Fuzz/property test for `vault::parse::classify_filename` (NFR-3).
//!
//! A seeded xorshift64* generator (no external dependency) produces at
//! least 10,000 filenames drawn from an alphabet of ASCII, the `" - "`
//! separator, control characters, `\u{0}`, emoji, RTL marks, and codepoints
//! adjacent to the surrogate range expressed as valid UTF-8, plus lengths
//! up to 32,768. Every call must return `Ok`/`Err` without panicking, and
//! the same seed must reproduce the same run.
//!
//! `ALPHABET` alone cannot spell any of the ten allowed media extensions
//! (it is missing letters like `m`, `p`, `4`, `k`, `v`, `o`, `w`, `e`, `i`,
//! `3`, `f`, `l`, `g`, `s`, `n`), so a name built purely from random
//! characters fails `media::from_file_name` before `classify_filename` ever
//! reaches the separator split, project-code, date or title validation.
//! `random_filename` therefore appends a real allowed extension to most
//! generated names so the sweep actually exercises the parser past the
//! extension gate, and `ten_thousand_random_filenames_never_panic` asserts
//! that a meaningful fraction of cases do so.

use vault::error::VaultError;
use vault::parse::classify_filename;

const SEED: u64 = 0x9E37_79B9_7F4A_7C15;

struct Xorshift64Star(u64);

impl Xorshift64Star {
    fn new(seed: u64) -> Self {
        Xorshift64Star(seed | 1) // must never be zero
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn next_range(&mut self, bound: usize) -> usize {
        if bound == 0 {
            0
        } else {
            (self.next_u64() as usize) % bound
        }
    }
}

const ALPHABET: &[char] = &[
    'A', 'B', 'C', 'a', 'b', 'c', '0', '1', '2', ' ', '-', '.', '_', '(', ')', '/', '\\', ':', '"',
    '<', '>', '|', '?', '*',
];

/// The ten recording extensions `media::from_file_name` accepts (FR-7),
/// duplicated here (rather than imported) so this test genuinely exercises
/// the crate's public parsing entry point end to end, the way F3 would.
const EXTENSIONS: &[&str] = &[
    "mp4", "mkv", "mov", "webm", "avi", "m4a", "mp3", "wav", "flac", "ogg",
];

fn random_char(rng: &mut Xorshift64Star) -> char {
    match rng.next_range(10) {
        0 => '\u{0}',
        1 => '\u{1f}',
        2 => '\u{7f}',
        3 => '\u{1F600}', // emoji
        4 => '\u{200F}',  // RTL mark
        5 => '\u{D7FF}',  // highest codepoint just below the surrogate range
        6 => '\u{E000}',  // lowest codepoint just above the surrogate range
        7 => '\u{FFFD}',  // replacement character
        _ => ALPHABET[rng.next_range(ALPHABET.len())],
    }
}

fn random_filename(rng: &mut Xorshift64Star) -> String {
    // Almost always a short name; rarely (1 in 1000) push toward 32,768
    // chars to cover NFR-3's long-input requirement without making the
    // whole sweep slow.
    let len = if rng.next_range(1000) == 0 {
        rng.next_range(32_768)
    } else {
        rng.next_range(64)
    };

    let mut s = String::new();
    for _ in 0..len {
        if rng.next_range(20) == 0 {
            s.push_str(" - ");
        } else {
            s.push(random_char(rng));
        }
    }

    // Append a real allowed extension to 70% of names so a meaningful
    // fraction of the sweep reaches the parser past the extension gate,
    // rather than every case dying in `media::from_file_name`. The
    // remaining 30% stay purely random, still covering the
    // no-dot / unsupported-extension path.
    if rng.next_range(10) < 7 {
        s.push('.');
        s.push_str(EXTENSIONS[rng.next_range(EXTENSIONS.len())]);
    }

    s
}

#[test]
fn ten_thousand_random_filenames_never_panic() {
    let mut rng = Xorshift64Star::new(SEED);
    let mut reached_parser = 0usize;
    for _ in 0..10_000 {
        let name = random_filename(&mut rng);
        // The point of this test is solely that this call does not panic.
        let result = classify_filename(&name);
        if !matches!(result, Err(VaultError::UnsupportedMediaType { .. })) {
            reached_parser += 1;
        }
    }

    // NFR-3 must prove the parser itself never panics, not merely the
    // extension gate. Assert a meaningful fraction of the 10,000 cases
    // actually got past `media::from_file_name` into the separator split,
    // project-code, date and title validation.
    assert!(
        reached_parser > 5_000,
        "expected a majority of cases to reach the parser past the \
         extension gate, only {reached_parser}/10000 did"
    );
}

#[test]
fn same_seed_reproduces_the_same_run() {
    let mut rng_a = Xorshift64Star::new(SEED);
    let mut rng_b = Xorshift64Star::new(SEED);

    for _ in 0..1_000 {
        let name_a = random_filename(&mut rng_a);
        let name_b = random_filename(&mut rng_b);
        assert_eq!(name_a, name_b);
        assert_eq!(classify_filename(&name_a), classify_filename(&name_b));
    }
}
