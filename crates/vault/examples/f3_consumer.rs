//! A scratch consumer mirroring F3's drop handler (T11's smoke run).
//!
//! Takes `<vault root>` and `<dropped file>` from argv, calls
//! [`vault::Vault::open`] then [`vault::Vault::ingest`] exactly the way
//! F3's Tauri `#[command]` will, and prints the classification, the
//! meeting-folder path, the source path and the collision outcome to
//! stdout. Any error goes to stderr and the process exits non-zero — the
//! `cli` profile's verification rule that stdout/stderr and exit codes
//! are the contract, not a return value only visible in-process.
//!
//! Usage:
//!
//! ```text
//! cargo run --example f3_consumer -- <vault root> <dropped file>
//! ```

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use vault::{Classification, Vault};

fn main() -> ExitCode {
    let mut args = env::args().skip(1);
    let (root, source) = match (args.next(), args.next()) {
        (Some(root), Some(source)) => (PathBuf::from(root), PathBuf::from(source)),
        _ => {
            eprintln!("usage: f3_consumer <vault root> <dropped file>");
            return ExitCode::FAILURE;
        }
    };

    let vault = match Vault::open(&root) {
        Ok(vault) => vault,
        Err(e) => {
            eprintln!("error: could not open vault at {}: {e}", root.display());
            return ExitCode::FAILURE;
        }
    };

    let ingested = match vault.ingest(&source) {
        Ok(ingested) => ingested,
        Err(e) => {
            eprintln!("error: could not ingest {}: {e}", source.display());
            return ExitCode::FAILURE;
        }
    };

    match ingested.classification {
        Classification::Sorted {
            project,
            date,
            title,
        } => {
            println!("classification: sorted (project={project}, date={date}, title={title:?})");
        }
        Classification::Unsorted { reason } => {
            println!("classification: unsorted (reason={reason})");
        }
    }
    println!("meeting_dir: {}", ingested.meeting_dir.display());
    println!("source_path: {}", ingested.source_path.display());
    println!("collision: {:?}", ingested.collision);

    ExitCode::SUCCESS
}
