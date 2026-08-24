"""Tests for the Tauri bundle configuration (T6: FR-7, FR-8, FR-9, FR-13, NFR-4).

Self-contained by design: there is deliberately no scripts/tests/conftest.py
(see plan.md), so this module derives the repo root itself instead of relying
on a shared fixture.

These are config-level checks only. They do not invoke `tauri build` (the
engine payload this config references is staged by
scripts/stage_engine_payload.py, and running a real bundling build here would
require that staging to have already happened).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_TAURI = REPO_ROOT / "apps" / "desktop" / "src-tauri"
CONFIG_PATH = SRC_TAURI / "tauri.conf.json"
CARGO_CONFIG_PATH = SRC_TAURI / ".cargo" / "config.toml"


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_bundle_active_and_single_nsis_target():
    config = _load_config()
    bundle = config["bundle"]
    assert bundle["active"] is True
    assert bundle["targets"] == ["nsis"], "FR-7: exactly one installer executable, NSIS only"


def test_bundle_resources_reference_the_staged_engine_payload():
    """The app folder must contain the engine's own runtime files.

    `scripts/stage_engine_payload.py` assembles them into
    `apps/desktop/src-tauri/resources/engine/`, and this maps that directory
    onto the application folder's root, so `whisper.dll` lands beside the
    executable and `runtime/`/`models/` land where the engine looks for them.

    The source directory must physically exist (even if only as a tracked
    placeholder) because tauri-build's build.rs resolves bundle.resources on
    *every* `cargo build`/`test`/`clippy`, not only during real bundling -- a
    nonexistent path there hard-errors the whole crate compile.
    """
    config = _load_config()
    resources = config["bundle"]["resources"]
    assert resources, "bundle.resources must be declared"
    assert isinstance(resources, dict), "expect a source->target map for fine-grained control"
    assert resources.get("resources/engine/") == "./"

    source = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "resources" / "engine"
    assert source.is_dir(), (
        f"{source} must exist even before a payload is staged, or every "
        "cargo build of the app crate fails"
    )
def test_nsis_install_mode_is_current_user():
    config = _load_config()
    nsis = config["bundle"]["windows"]["nsis"]
    assert nsis["installMode"] == "currentUser", "NFR-4/Q4-A: per-user install, no UAC"


def test_installer_hooks_points_at_installer_directory():
    config = _load_config()
    nsis = config["bundle"]["windows"]["nsis"]
    hooks_rel = nsis["installerHooks"]
    assert hooks_rel == "../../../installer/installer_hooks.nsh"

    resolved = (SRC_TAURI / hooks_rel).resolve()
    expected = (REPO_ROOT / "installer" / "installer_hooks.nsh").resolve()
    assert resolved == expected

    if not resolved.is_file():
        pytest.skip(
            f"{resolved} does not exist yet -- installer/installer_hooks.nsh is T7's file "
            "(T6 and T7 run concurrently in wave 2); the config wiring above is verified, "
            "the physical file is T7's Done-when."
        )


def test_webview_install_mode_is_download_bootstrapper():
    config = _load_config()
    mode = config["bundle"]["windows"]["webviewInstallMode"]
    assert mode["type"] == "downloadBootstrapper", "FR-9: WebView2 detect-and-install, small installer"


def test_nsis_does_not_disable_start_menu_or_desktop_shortcut():
    """FR-13: Tauri's stock NSIS template always creates a Start Menu shortcut
    and always offers the desktop-shortcut checkbox on the installer's finish
    page -- there is no config key to disable either (confirmed against the
    installed @tauri-apps/cli config schema, which has no such toggle). This
    test only guards against a future edit accidentally adding one under an
    unexpected key.
    """
    config = _load_config()
    nsis = config["bundle"]["windows"]["nsis"]
    assert "startMenuFolder" not in nsis or isinstance(nsis["startMenuFolder"], str)
    for disabling_key in ("createDesktopShortcut", "disableDesktopShortcut", "noShortcuts"):
        assert disabling_key not in nsis


def test_nsis_is_non_interactive_language_selection():
    config = _load_config()
    nsis = config["bundle"]["windows"]["nsis"]
    assert nsis["displayLanguageSelector"] is False
    assert nsis.get("languages")


def test_bundle_version_matches_version_txt():
    version = (REPO_ROOT / "version.txt").read_text(encoding="utf-8").strip()
    config = _load_config()
    assert config["version"] == version


def test_product_identity_is_unchanged_from_f3():
    config = _load_config()
    assert config["productName"] == "Transcriber"
    assert config["identifier"] == "com.transcriber.desktop"


def test_cargo_config_sets_static_crt_for_msvc_target():
    assert CARGO_CONFIG_PATH.is_file(), f"expected {CARGO_CONFIG_PATH} to exist"
    text = CARGO_CONFIG_PATH.read_text(encoding="utf-8")
    assert re.search(r"\[target\.x86_64-pc-windows-msvc\]", text)
    assert "target-feature=+crt-static" in text
