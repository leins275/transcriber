"""Tests for `scripts/build_installer.py` (T8, FR-6, FR-15, NFR-1, R4).

The real pipeline shells out to `npm`, `cargo` (via Tauri) and `uv`, and
produces gigabytes of output over many minutes -- none of that runs here.
Instead:

  * the stage list and its per-stage exit codes are asserted structurally
    and via monkeypatched failure injection (FR-6 "exits non-zero if any
    payload fails", one distinct code per payload);
  * `--dry-run` is exercised for real, but every stage function it would
    otherwise call is monkeypatched to explode if invoked, proving dry-run
    truly touches nothing (FR-6, NFR-5);
  * `collect()` and `gate_artifact_size()` are pure functions tested
    directly against fixture bytes under `tmp_path`, with no real installer
    or engine payload staging anywhere nearby (FR-15, NFR-1).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_installer  # noqa: E402
import sync_version  # noqa: E402


# --- stage order and exit codes -----------------------------------------


def test_stage_order_matches_the_documented_pipeline() -> None:
    assert [stage.name for stage in build_installer.STAGES] == [
        "version_check",
        "lock_check",
        "engine_payload",
        "tauri_build",
        "collect",
        "gate",
    ]


def test_every_stage_has_a_distinct_nonzero_exit_code() -> None:
    codes = [stage.exit_code for stage in build_installer.STAGES]
    assert all(code != 0 for code in codes)
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("stage", build_installer.STAGES, ids=[s.name for s in build_installer.STAGES])
def test_a_failing_stage_aborts_with_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, stage: build_installer.Stage
) -> None:
    # Every stage before the failing one is a no-op success; the failing
    # one raises; every stage after it must never run.
    called_after_failure: list[str] = []
    saw_failure = False

    for candidate in build_installer.STAGES:
        if candidate.name == stage.name:

            def _raise(ctx: build_installer.BuildContext) -> None:
                raise build_installer.BuildInstallerError("synthetic failure")

            monkeypatch.setattr(build_installer, candidate.func_name, _raise)
        else:

            def _record(ctx: build_installer.BuildContext, _name: str = candidate.name) -> None:
                if _name != stage.name:
                    called_after_failure.append(_name)

            monkeypatch.setattr(build_installer, candidate.func_name, _record)

    ctx = build_installer.BuildContext()
    exit_code = build_installer.run_stages(ctx)

    assert exit_code == stage.exit_code
    # nothing after the failing stage in pipeline order should have run
    failing_index = [s.name for s in build_installer.STAGES].index(stage.name)
    later_stage_names = {s.name for s in build_installer.STAGES[failing_index + 1 :]}
    assert not (set(called_after_failure) & later_stage_names)


def test_all_stages_succeeding_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    for stage in build_installer.STAGES:
        monkeypatch.setattr(build_installer, stage.func_name, lambda ctx: None)
    assert build_installer.run_stages(build_installer.BuildContext()) == 0


# --- dry run -------------------------------------------------------------


def test_dry_run_prints_every_planned_command_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for stage in build_installer.STAGES:

        def _explode(ctx: build_installer.BuildContext) -> None:
            raise AssertionError("dry-run must not execute any real stage")

        monkeypatch.setattr(build_installer, stage.func_name, _explode)

    def _explode_run(cmd, cwd=None):  # noqa: ANN001
        raise AssertionError("dry-run must not invoke any subprocess")

    monkeypatch.setattr(build_installer, "_run", _explode_run)

    dist_dir = tmp_path / "dist"

    exit_code = build_installer.main(["--dry-run", "--dist-dir", str(dist_dir)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "sync_version.py --check" in out
    assert "verify_locks.py --check" in out
    assert "stage_engine_payload.py" in out
    assert "npm --prefix" in out
    assert f"tauri -- {' '.join(build_installer._tauri_build_args())} -- --locked" in out, (
        "E5/FR-4: --locked must reach cargo through the tauri build stage, "
        f"not just npm ci/uv export --frozen, got: {out!r}"
    )
    assert "build-manifest.json" in out
    assert str(build_installer.SIZE_LIMIT_BYTES) in out

    assert not dist_dir.exists()


def test_run_resolves_the_executable_through_path_before_invoking_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows' CreateProcess (which subprocess.run(..., shell=False) uses)
    # does not apply PATHEXT/shell command resolution to a bare command
    # name -- "npm" (a .CMD shim, not a .exe) fails with
    # `FileNotFoundError: [WinError 2]` unless it is resolved to its full
    # path first. Found empirically during T14's first real
    # `uv run scripts/build_installer.py` on the operator's machine: every
    # `npm ...`/`tauri build` invocation in stage_tauri_build failed this
    # way before this fix.
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return r"C:\Fake\npm.CMD" if name == "npm" else name

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(build_installer.shutil, "which", fake_which)
    monkeypatch.setattr(build_installer.subprocess, "run", fake_subprocess_run)

    build_installer._run(["npm", "--version"])

    assert calls == [[r"C:\Fake\npm.CMD", "--version"]]


def test_run_leaves_the_command_unchanged_when_it_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return None

    def fake_subprocess_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(build_installer.shutil, "which", fake_which)
    monkeypatch.setattr(build_installer.subprocess, "run", fake_subprocess_run)

    build_installer._run(["totally-unresolvable-command", "--version"])

    assert calls == [["totally-unresolvable-command", "--version"]]


def test_stage_tauri_build_passes_locked_through_to_the_tauri_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E5/FR-4: release builds must fail loudly on an out-of-date
    `Cargo.lock` rather than silently re-resolving -- verified empirically
    (a fake `cargo` runner substituted via Tauri CLI's own `--runner`,
    invoked both directly and through this exact `npm run` argv shape) that
    reaching cargo requires *two* separate `--` separators: one immediately
    after the `tauri` script name (so `npm` itself never parses `--locked`
    as one of its own CLI flags), and Tauri CLI's own `--` marking the
    start of args forwarded to the runner."""
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None):  # noqa: ANN001
        calls.append(list(cmd))
        return ""

    monkeypatch.setattr(build_installer, "_run", fake_run)
    fake_installer = tmp_path / sync_version.artifact_name("0.0.0")
    fake_installer.write_bytes(b"")
    monkeypatch.setattr(
        build_installer, "find_built_installer", lambda repo_root, version=None, product=None: fake_installer
    )

    ctx = build_installer.BuildContext()
    build_installer.stage_tauri_build(ctx)

    assert calls == [
        ["npm", "--prefix", str(build_installer.APP_DIR), "ci"],
        [
            "npm",
            "--prefix",
            str(build_installer.APP_DIR),
            "run",
            "tauri",
            "--",
            *build_installer._tauri_build_args(),
            "--",
            "--locked",
        ],
    ]
    assert ctx.installer_src == fake_installer


def test_main_with_no_extra_flag_defaults_to_no_cuda(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = build_installer.main(["--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "--extra cuda" not in out


def test_dry_run_is_noninteractive_and_never_prompts(capsys: pytest.CaptureFixture[str]) -> None:
    # A bare dry-run (no monkeypatching needed) must still complete with no
    # interactive prompt of any kind -- exit 0 with only printed lines.
    exit_code = build_installer.main(["--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count("[dry-run]") == len(build_installer.describe_plan(build_installer.BuildContext()))


# --- find_built_installer() ------------------------------------------------


def test_find_built_installer_looks_under_the_workspace_root_target_dir(tmp_path: Path) -> None:
    # `Cargo.toml` at the repo root is a workspace (`members = ["crates/vault",
    # "apps/desktop/src-tauri"]`), so Cargo puts one shared `target/` at the
    # *workspace root*, not a separate `apps/desktop/src-tauri/target/` --
    # confirmed empirically (the real `tauri build` in this task's second
    # real run produced `target/release/bundle/nsis/Transcriber_0.1.0_x64-
    # setup.exe` at the repo root, and `find_built_installer` failed to find
    # it there). Found on the first real end-to-end build this fix pass
    # allowed to reach the NSIS step at all.
    bundle_dir = build_installer.bundle_dir(tmp_path)
    bundle_dir.mkdir(parents=True)
    installer = bundle_dir / sync_version.artifact_name("0.1.0")
    installer.write_bytes(b"fixture-installer-bytes")

    found = build_installer.find_built_installer(tmp_path, version="0.1.0")

    assert found == installer


def test_find_built_installer_ignores_a_previous_versions_leftover(tmp_path: Path) -> None:
    # `target/` is not cleaned between builds, so after a bump the bundle
    # directory holds both installers -- and the older one sorts first. The
    # picker used to take `sorted(glob("*.exe"))[0]` and hand back the stale
    # artifact, which `collect` then copied out under the *new* version's
    # name. Two releases with different version numbers and identical bytes
    # is how this was found.
    bundle_dir = build_installer.bundle_dir(tmp_path)
    bundle_dir.mkdir(parents=True)
    stale = bundle_dir / sync_version.artifact_name("0.1.0")
    stale.write_bytes(b"last release's bytes")
    fresh = bundle_dir / sync_version.artifact_name("0.2.0")
    fresh.write_bytes(b"this release's bytes")

    found = build_installer.find_built_installer(tmp_path, version="0.2.0")

    assert found == fresh
    assert found.read_bytes() == b"this release's bytes"


def test_find_built_installer_refuses_to_substitute_another_version(tmp_path: Path) -> None:
    # A build that did not produce what it was asked for must fail, not fall
    # back to whatever is lying nearby.
    bundle_dir = build_installer.bundle_dir(tmp_path)
    bundle_dir.mkdir(parents=True)
    (bundle_dir / sync_version.artifact_name("0.1.0")).write_bytes(b"stale")

    with pytest.raises(build_installer.BuildInstallerError) as excinfo:
        build_installer.find_built_installer(tmp_path, version="0.2.0")

    message = str(excinfo.value)
    assert sync_version.artifact_name("0.2.0") in message
    # Names what it did find, so the failure is diagnosable at a glance.
    assert sync_version.artifact_name("0.1.0") in message


def test_find_built_installer_reports_an_empty_bundle_directory(tmp_path: Path) -> None:
    bundle_dir = build_installer.bundle_dir(tmp_path)
    bundle_dir.mkdir(parents=True)

    with pytest.raises(build_installer.BuildInstallerError):
        build_installer.find_built_installer(tmp_path, version="0.2.0")


# --- collect() ------------------------------------------------------------


def _fake_payload_manifest() -> dict:
    return {
        "profile": "release",
        "files": ["whisper.dll", "ffmpeg.exe"],
        "total_bytes": 123_456_789,
    }


def test_collect_writes_installer_checksum_and_manifest(tmp_path: Path) -> None:
    installer_src = tmp_path / "src" / "Transcriber_x64-setup.exe"
    installer_src.parent.mkdir(parents=True)
    installer_src.write_bytes(b"fixture-installer-bytes" * 100)

    dist_dir = tmp_path / "dist"

    result = build_installer.collect(
        installer_src=installer_src,
        version="1.2.3",
        git_commit="deadbeef",
        payload_manifest=_fake_payload_manifest(),
        payload_versions={"rust": "1.2.3", "node": "1.2.3"},
        dist_dir=dist_dir,
    )

    expected_installer = dist_dir / sync_version.artifact_name("1.2.3")
    assert result.installer_path == expected_installer
    assert expected_installer.is_file()
    assert expected_installer.read_bytes() == installer_src.read_bytes()

    checksum_path = dist_dir / f"{sync_version.artifact_name('1.2.3')}.sha256"
    assert result.checksum_path == checksum_path
    assert checksum_path.is_file()
    assert build_installer.verify_checksum_file(expected_installer, checksum_path)

    recorded_digest = checksum_path.read_text(encoding="utf-8").split()[0]
    actual_digest = hashlib.sha256(expected_installer.read_bytes()).hexdigest()
    assert recorded_digest == actual_digest == result.sha256

    manifest_path = dist_dir / "build-manifest.json"
    assert result.manifest_path == manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["product_version"] == "1.2.3"
    assert manifest["git_commit"] == "deadbeef"
    assert manifest["payload_versions"] == {"rust": "1.2.3", "node": "1.2.3"}
    assert manifest["artifact"]["sha256"] == actual_digest
    assert manifest["artifact"]["name"] == sync_version.artifact_name("1.2.3")


def test_collect_checksum_fails_verification_if_file_is_tampered(tmp_path: Path) -> None:
    installer_src = tmp_path / "Transcriber_x64-setup.exe"
    installer_src.write_bytes(b"original-bytes")
    dist_dir = tmp_path / "dist"

    result = build_installer.collect(
        installer_src=installer_src,
        version="1.0.0",
        git_commit="abc123",
        payload_manifest=_fake_payload_manifest(),
        payload_versions={},
        dist_dir=dist_dir,
    )

    result.installer_path.write_bytes(b"tampered-bytes-different-length")
    assert not build_installer.verify_checksum_file(result.installer_path, result.checksum_path)


# --- size gate (NFR-1) -----------------------------------------------------


def test_size_gate_raises_when_artifact_exceeds_the_budget(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.exe"
    with open(oversized, "wb") as fh:
        fh.truncate(build_installer.SIZE_LIMIT_BYTES + 1)

    with pytest.raises(build_installer.SizeGateError):
        build_installer.gate_artifact_size(oversized)


def test_size_gate_passes_at_exactly_the_budget(tmp_path: Path) -> None:
    exact = tmp_path / "exact.exe"
    with open(exact, "wb") as fh:
        fh.truncate(build_installer.SIZE_LIMIT_BYTES)

    size = build_installer.gate_artifact_size(exact)
    assert size == build_installer.SIZE_LIMIT_BYTES


def test_size_gate_passes_well_under_the_budget(tmp_path: Path) -> None:
    small = tmp_path / "small.exe"
    small.write_bytes(b"tiny")
    assert build_installer.gate_artifact_size(small) == 4


# --- stage_gate wiring (integration of gate into the pipeline) ------------


def test_stage_gate_raises_build_installer_error_when_oversized(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.exe"
    with open(oversized, "wb") as fh:
        fh.truncate(build_installer.SIZE_LIMIT_BYTES + 1)

    ctx = build_installer.BuildContext()
    ctx.collect_result = build_installer.CollectResult(
        installer_path=oversized,
        checksum_path=tmp_path / "oversized.exe.sha256",
        manifest_path=tmp_path / "build-manifest.json",
        sha256="deadbeef",
    )

    with pytest.raises(build_installer.BuildInstallerError):
        build_installer.stage_gate(ctx)


def test_every_stage_function_named_in_stages_actually_resolves() -> None:
    # The stages are looked up by name off STAGES, and every test that drives
    # the pipeline monkeypatches them -- so a stage whose body references a
    # module that is no longer imported passes every existing test and fails
    # only during a real build. Caught exactly that way: the engine-payload
    # stage lost its import during the Python removal.
    for stage in build_installer.STAGES:
        func = getattr(build_installer, stage.func_name, None)
        assert callable(func), f"{stage.name} has no {stage.func_name}"
        for name in func.__code__.co_names:
            if name.startswith("stage_") or name.endswith("_impl"):
                assert hasattr(build_installer, name) or name in dir(build_installer), (
                    f"{stage.func_name} references {name}, which the module does not define"
                )


def test_side_by_side_renames_the_product_so_it_installs_beside_a_release() -> None:
    # Same install directory, identifier and uninstall entry would make this
    # an upgrade of the released copy rather than a second one.
    release = build_installer.BuildContext()
    test_build = build_installer.BuildContext(side_by_side=True)

    assert release.product_name() != test_build.product_name()
    assert "--config" not in build_installer._tauri_build_args(False)
    assert build_installer.SIDE_BY_SIDE_CONFIG in build_installer._tauri_build_args(True)
    assert Path(build_installer.SIDE_BY_SIDE_CONFIG).is_file()

    overlay = json.loads(
        (
            Path(build_installer.SIDE_BY_SIDE_CONFIG)
        ).read_text(encoding="utf-8")
    )
    base = json.loads(
        (REPO_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    assert overlay["productName"] == test_build.product_name()
    assert overlay["identifier"] != base["identifier"]
    # No signing key is available for a local build, and updater artifacts
    # cannot be produced without one.
    assert overlay["bundle"]["createUpdaterArtifacts"] is False


# -- --no-updater ----------------------------------------------------------
#
# `createUpdaterArtifacts` is on in tauri.conf.json, so the bundler stops with
# "A public key has been found, but no private key" on any machine without the
# release signing key -- after the installer is already on disk, but with a
# non-zero exit, so `stage_collect` never runs. These pin the escape hatch.


def test_no_updater_overlay_file_exists_and_only_disables_updater_artifacts():
    path = Path(build_installer.NO_UPDATER_CONFIG)
    assert path.is_file(), path
    overlay = json.loads(path.read_text(encoding="utf-8"))
    assert overlay["bundle"]["createUpdaterArtifacts"] is False
    # It must not touch identity: this flavour IS the release product, just
    # built without the key. Renaming it here would silently produce an
    # installer that upgrades nothing.
    assert "productName" not in overlay
    assert "identifier" not in overlay


def test_no_updater_passes_the_overlay_to_the_tauri_cli():
    args = build_installer._tauri_build_args(no_updater=True)
    assert build_installer.NO_UPDATER_CONFIG in args
    assert args[args.index(build_installer.NO_UPDATER_CONFIG) - 1] == "--config"


def test_plain_build_still_produces_updater_artifacts():
    assert build_installer.NO_UPDATER_CONFIG not in build_installer._tauri_build_args()


def test_side_by_side_implies_no_updater():
    """The side-by-side overlay disables updater artifacts itself, but the
    context flag must agree -- otherwise a later reader of `ctx.no_updater`
    would conclude the test flavour was signed."""
    args = build_installer._parse_args(["--side-by-side"])
    ctx = build_installer.build_context_from_args(args)
    assert ctx.no_updater is True


def test_no_updater_alone_keeps_the_release_product_name():
    args = build_installer._parse_args(["--no-updater"])
    ctx = build_installer.build_context_from_args(args)
    assert ctx.no_updater is True
    assert ctx.side_by_side is False
    assert ctx.product_name() == sync_version.DEFAULT_PRODUCT
