# Root Makefile -- QA fanout across the three-payload monorepo (Rust in
# crates/vault + apps/desktop/src-tauri, TypeScript in apps/desktop, Python in
# services/transcription) plus the release build entry points (FR-2, FR-6).
#
# NOTE (R6): `make` is not installed on a fresh clone until scripts/bootstrap.ps1
# has been run once (T2). Every recipe below is one command per line -- run any
# of them directly, in the order shown in the comment above each target, to get
# the same effect without make.
#
# GNU Make aborts a recipe on the first command that exits non-zero, which is
# exactly FR-2's fail-fast requirement -- so recipes here never use `&&` chains
# or a `-` prefix to suppress a failure.

.PHONY: format lint type test installer bootstrap

# Direct equivalents (run in order, from the repo root):
#   cargo fmt --all
#   npm --prefix apps/desktop run format
#   uv run --directory services/transcription ruff format .
format:
	cargo fmt --all
	npm --prefix apps/desktop run format
	uv run --directory services/transcription ruff format .

# Direct equivalents (run in order, from the repo root):
#   cargo clippy --workspace --all-targets -- -D warnings
#   npm --prefix apps/desktop run lint
#   uv run --directory services/transcription ruff check .
#   uv run scripts/sync_version.py --check
#   uv run scripts/verify_locks.py --check
lint:
	cargo clippy --workspace --all-targets -- -D warnings
	npm --prefix apps/desktop run lint
	uv run --directory services/transcription ruff check .
	uv run scripts/sync_version.py --check
	uv run scripts/verify_locks.py --check

# Direct equivalents (run in order, from the repo root):
#   cargo check --workspace
#   npm --prefix apps/desktop run type
#   uv run --directory services/transcription mypy src
type:
	cargo check --workspace
	npm --prefix apps/desktop run type
	uv run --directory services/transcription mypy src

# Direct equivalents (run in order, from the repo root):
#   cargo test --workspace
#   npm --prefix apps/desktop run test
#   uv run --directory services/transcription pytest -q
#   uv run --with pytest -- pytest scripts/tests -q
test:
	cargo test --workspace
	npm --prefix apps/desktop run test
	uv run --directory services/transcription pytest -q
	uv run --with pytest -- pytest scripts/tests -q

# Direct equivalent (built by a later wave -- this target resolves under
# `make -n` regardless of whether scripts/build_installer.py exists yet):
#   uv run scripts/build_installer.py
installer:
	uv run scripts/build_installer.py

# Direct equivalent (built by a later wave -- this target resolves under
# `make -n` regardless of whether scripts/bootstrap.ps1 exists yet):
#   powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
bootstrap:
	powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
