//! Builds the vendored whisper.cpp against llama-cpp-sys-2's ggml.
//!
//! The whole job is to make `whisper.dll` *import* the ggml that
//! `llama-cpp-sys-2` already built, rather than compile its own. That is what
//! lets both engines live in one process; see PINS.md for why a second copy
//! cannot.

use std::env;
use std::path::{Path, PathBuf};

fn main() {
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let whisper_src = manifest_dir.join("vendor").join("whisper.cpp");
    let header = whisper_src.join("include").join("whisper.h");

    println!("cargo:rerun-if-changed={}", whisper_src.display());

    // Set by cargo from llama-cpp-sys-2's `cargo:ggml_cmake_dir=` line. Its
    // absence means that crate stopped exporting it (or stopped building ggml
    // as a package), which would silently give us a second ggml -- so it is a
    // hard error, not a fallback.
    let ggml_cmake_dir = env::var("DEP_LLAMA_GGML_CMAKE_DIR").expect(
        "llama-cpp-sys-2 did not export DEP_LLAMA_GGML_CMAKE_DIR; \
         whisper.cpp cannot be built against the shared ggml without it",
    );

    // Work around a gap in ggml's exported CMake package: it attaches
    // INTERFACE_INCLUDE_DIRECTORIES only inside its `NOT GGML_BACKEND_DL`
    // branch, so with dynamic backends -- the configuration this whole design
    // rests on -- `find_package(ggml)` yields targets that link fine but carry
    // no header path, and whisper.cpp fails on `#include "ggml.h"`. Passing it
    // as a compiler flag is the least magical fix available from outside.
    let ggml_include = find_ggml_include(Path::new(&ggml_cmake_dir)).unwrap_or_else(|| {
        panic!("no include/ggml.h found above {ggml_cmake_dir}; ggml's install layout changed")
    });
    let include_flag = format!("-I{}", ggml_include.display());

    let dst = cmake::Config::new(&whisper_src)
        .define("BUILD_SHARED_LIBS", "ON")
        .define("WHISPER_USE_SYSTEM_GGML", "ON")
        .define("WHISPER_BUILD_TESTS", "OFF")
        .define("WHISPER_BUILD_EXAMPLES", "OFF")
        .define("CMAKE_PREFIX_PATH", &ggml_cmake_dir)
        .cflag(&include_flag)
        .cxxflag(&include_flag)
        // The vendored tree has no ggml/ directory, so any option that would
        // reach into it is meaningless here; these keep the whisper side from
        // trying to add its own backends on top of the shared ggml.
        .define("WHISPER_BUILD_SERVER", "OFF")
        .profile("Release")
        .build();

    emit_link_flags(&dst);
    generate_bindings(&header, &ggml_include);
    stage_runtime_dlls(&dst, &ggml_include);
}

/// Copy the engine DLLs next to the binaries cargo builds.
///
/// Cargo does not stage native runtime libraries, so without this a test or
/// `cargo run` binary starts and immediately fails to resolve `whisper.dll`.
/// The shipped app gets its DLLs from the installer instead; this exists so
/// the development loop works at all, and it is deliberately best-effort --
/// a copy that fails (a DLL locked by a running binary, most likely) must not
/// fail the build.
fn stage_runtime_dlls(whisper_dst: &Path, ggml_include: &Path) {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    // OUT_DIR is target/<profile>/build/<pkg>-<hash>/out
    let Some(profile_dir) = out_dir.ancestors().nth(3) else {
        return;
    };
    // Integration tests and unit tests run from deps/, plain binaries from
    // the profile root.
    let targets = [profile_dir.to_path_buf(), profile_dir.join("deps")];

    let sources = [whisper_dst.join("bin"), ggml_include.with_file_name("bin")];
    for source in sources.iter().filter(|d| d.is_dir()) {
        let Ok(entries) = std::fs::read_dir(source) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .extension()
                .is_some_and(|e| e.eq_ignore_ascii_case("dll"))
            {
                for target in &targets {
                    if target.is_dir() {
                        let _ = std::fs::copy(&path, target.join(entry.file_name()));
                    }
                }
            }
        }
    }
}

/// Walk up from ggml's exported CMake directory to the `include/` holding
/// `ggml.h`, wherever the install prefix happens to sit.
fn find_ggml_include(cmake_dir: &Path) -> Option<PathBuf> {
    let mut dir = Some(cmake_dir);
    while let Some(current) = dir {
        let candidate = current.join("include");
        if candidate.join("ggml.h").is_file() {
            return Some(candidate);
        }
        dir = current.parent();
    }
    None
}

/// Tell rustc where the import libraries are and what to link.
///
/// Only `whisper` is named: ggml comes in through whisper's own imports, and
/// naming it here again would be a second, competing view of which ggml this
/// binary uses.
fn emit_link_flags(dst: &Path) {
    for dir in ["lib", "lib64", "bin"] {
        let candidate = dst.join(dir);
        if candidate.is_dir() {
            println!("cargo:rustc-link-search=native={}", candidate.display());
        }
    }
    println!("cargo:rustc-link-lib=dylib=whisper");

    // Dependents (and the packaging step) need to find the DLLs to stage next
    // to the executable; cargo does not copy them.
    println!("cargo:root={}", dst.display());
    println!("cargo:bin_dir={}", dst.join("bin").display());
}

fn generate_bindings(header: &Path, ggml_include: &Path) {
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    let bindings = bindgen::Builder::default()
        .header(header.to_string_lossy())
        // `whisper.h` opens with `#include "ggml.h"`, so bindgen's clang needs
        // the same header path the compiler got.
        .clang_arg(format!("-I{}", ggml_include.display()))
        // whisper.h is the entire surface we bind; pulling in the C runtime's
        // headers would generate thousands of irrelevant items.
        .allowlist_function("whisper_.*")
        .allowlist_type("whisper_.*")
        .allowlist_var("WHISPER_.*")
        // Callback-carrying structs are passed across the FFI boundary by
        // value, so their layout must be exact rather than opaque.
        .derive_default(true)
        .derive_debug(true)
        .prepend_enum_name(false)
        .parse_callbacks(Box::new(bindgen::CargoCallbacks::new()))
        .generate()
        .expect("failed to generate whisper.cpp bindings");

    bindings
        .write_to_file(out_dir.join("bindings.rs"))
        .expect("failed to write whisper.cpp bindings");
}
