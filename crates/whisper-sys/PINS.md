# Engine source pins

Two trees must agree on **one** ggml, because ggml structs cross a DLL
boundary between them. Bumping either means checking the other.

| Component | Pin | ggml version |
|---|---|---|
| `llama-cpp-sys-2` (crates.io) | `0.1.154` — vendors llama.cpp `bed81ad` | **0.18.0** |
| `vendor/whisper.cpp` (this crate) | `306c88f4d1286aec1bf96e544632897886af5501` (tag `v1.9.2`) | **0.18.1** |

One patch release apart. That closeness is the point of the pin, and it is not
automatic: whisper.cpp `v1.9.3` carries ggml 0.20.2, two minor releases ahead
of what the crate vendors. It happens to *compile* against 0.18.0, which is
exactly the trap — a silent struct-layout difference would surface as garbage
at runtime, not as a build error. Pick the whisper.cpp release whose ggml
matches the crate's, never the newest one that builds.

Useful ggml versions by whisper.cpp release: `v1.9.0`/`v1.9.1` → 0.15.1,
`v1.9.2` → 0.18.1, `v1.9.3` → 0.20.2.

## Why there is only one ggml here

`llama-cpp-sys-2` with the `dynamic-backends` feature builds ggml itself as
shared libraries (`GGML_BACKEND_DL=ON`, `GGML_CPU_ALL_VARIANTS=ON`) and exports
the resulting CMake package directory as `DEP_LLAMA_GGML_CMAKE_DIR`. This
crate depends on it solely to receive that export, then builds whisper.cpp with
`WHISPER_USE_SYSTEM_GGML=ON` against it, so `whisper.dll` and `llama.dll` both
*import* the same `ggml-base.dll` instead of each embedding a private copy.

Two copies cannot coexist: statically linking both produces duplicate ggml
symbols, and shipping two same-named `ggml-base.dll` files does not help
either, because the Windows loader resolves imports by basename against
already-loaded modules.

That is why whisper.cpp's own `ggml/` tree is **not** vendored below — it would
never be compiled. It is also 22 MB of the 40 MB checkout.

`GGML_BACKEND_API_VERSION` is the runtime backstop: a backend DLL built
against a different ggml refuses to register rather than crashing.

## Known upstream gap the build works around

ggml's exported `ggml-config.cmake` attaches `INTERFACE_INCLUDE_DIRECTORIES`
only inside its `if (NOT GGML_BACKEND_DL)` branch. With dynamic backends — the
configuration this whole design rests on — `find_package(ggml)` therefore
yields targets that link correctly but carry no header path, and whisper.cpp
fails on `#include "ggml.h"`. `build.rs` locates `include/ggml.h` by walking up
from the exported CMake directory and passes it as an explicit `-I` to both the
C/C++ compiler and bindgen's clang. Revisit if a future ggml sets the property
unconditionally.

## What is vendored

`vendor/whisper.cpp/` holds only what the build touches with system ggml,
tests and examples off: `CMakeLists.txt`, `cmake/`, `include/`, `src/`,
`bindings/javascript/package-tmpl.json` (an unconditional `configure_file`
target in the top-level CMakeLists), plus `LICENSE` and `AUTHORS`. That is
~660 KB — small enough to vendor outright, which keeps the exact source
visible in-tree and CI free of submodule handling.

To refresh, re-copy those paths from a clean checkout at the new tag and update
the table above.
