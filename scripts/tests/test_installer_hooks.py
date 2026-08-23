"""Static contract tests for installer/installer_hooks.nsh (T7).

NSIS cannot be unit-tested here (no `makensis` on this machine -- see
spec.md's Detected stack table). These tests are the plan's "static
contract assertions over the .nsh": they parse the hook file as text and
assert the macros, guard structure and safety invariants the plan
requires. The *behavioural* proof (double-install, uninstall vault hash,
silent /VAULT= run) is this task's Done-when, executed by hand, and T14's
job to repeat end-to-end against a real build.

Repo root is derived relative to this file (parents[2]: scripts/tests/ ->
scripts/ -> root), per plan.md's rule that no shared conftest.py exists
across scripts/tests/.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_FILE = REPO_ROOT / "installer" / "installer_hooks.nsh"
README_FILE = REPO_ROOT / "installer" / "README.md"
TAURI_CONF = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json"

HOOK_MACROS = (
    "NSIS_HOOK_PREINSTALL",
    "NSIS_HOOK_POSTINSTALL",
    "NSIS_HOOK_PREUNINSTALL",
    "NSIS_HOOK_POSTUNINSTALL",
)


def _read_hooks() -> str:
    assert HOOKS_FILE.is_file(), f"expected hooks file to exist: {HOOKS_FILE}"
    return HOOKS_FILE.read_text(encoding="utf-8")


def _macro_body(text: str, name: str) -> str:
    """Extract the body text of `!macro <name> ... !macroend`."""
    pattern = re.compile(
        r"!macro\s+" + re.escape(name) + r"\b(.*?)!macroend",
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)
    assert match, f"expected !macro {name} ... !macroend block in {HOOKS_FILE}"
    return match.group(1)


def test_all_four_tauri_hook_macros_are_defined() -> None:
    text = _read_hooks()
    for name in HOOK_MACROS:
        _macro_body(text, name)  # asserts internally


def test_preinstall_stops_only_processes_running_from_instdir_pyenv() -> None:
    """Field report (v0.2.1 -> v0.3.0 auto-update): the update install failed
    with "Error opening file for writing: ...\\pyenv\\python\\DLLs\\_asyncio.pyd"
    because the app's bundled Python sidecar was still running while the new
    installer overwrote $INSTDIR\\pyenv (the updater plugin exits the app
    process on a path that skips its RunEvent::Exit sidecar cleanup).
    NSIS_HOOK_PREINSTALL must terminate anything still executing out of
    $INSTDIR\\pyenv before file copy -- and must stay filtered to that path,
    never a machine-wide kill of every python.exe."""
    body = _macro_body(_read_hooks(), "NSIS_HOOK_PREINSTALL")
    kill_lines = [line for line in body.splitlines() if "Stop-Process" in line]
    assert kill_lines, (
        "expected NSIS_HOOK_PREINSTALL to Stop-Process the orphaned sidecar "
        "before files are copied into $INSTDIR"
    )
    for line in kill_lines:
        assert "$INSTDIR\\pyenv" in line, (
            "the kill must be filtered to processes running from the bundled "
            f"interpreter's own tree, never machine-wide: {line!r}"
        )
    assert "taskkill" not in body.lower(), (
        "no taskkill by image name here -- that would reach python.exe "
        "processes that are not ours"
    )
    assert re.search(r"^\s*Sleep\s+\d+", body, re.MULTILINE), (
        "expected a settle delay after the kill so the OS releases the file "
        "handles before the installer starts overwriting pyenv\\"
    )


def test_postinstall_creates_the_three_app_folder_subdirectories() -> None:
    body = _macro_body(_read_hooks(), "NSIS_HOOK_POSTINSTALL")
    for sub in ("models", "logs", "data"):
        assert re.search(
            rf'CreateDirectory\s+"\$INSTDIR\\{sub}"', body, re.IGNORECASE
        ), f"expected CreateDirectory of $INSTDIR\\{sub} in NSIS_HOOK_POSTINSTALL"


def test_preuninstall_and_postuninstall_macros_exist_and_are_nonempty() -> None:
    text = _read_hooks()
    pre = _macro_body(text, "NSIS_HOOK_PREUNINSTALL").strip()
    post = _macro_body(text, "NSIS_HOOK_POSTUNINSTALL").strip()
    assert pre, "NSIS_HOOK_PREUNINSTALL must not be an empty macro"
    assert post, "NSIS_HOOK_POSTUNINSTALL must not be an empty macro"


def test_no_delete_or_rmdir_line_touches_the_vault_or_an_unrooted_path() -> None:
    text = _read_hooks()

    delete_line_pattern = re.compile(
        r'^\s*(?:Delete|RMDir)(?:\s*/r)?\s+"([^"]+)"', re.MULTILINE | re.IGNORECASE
    )
    delete_lines = delete_line_pattern.findall(text)
    assert delete_lines, "expected at least one Delete/RMDir line to inspect"

    allowed_roots = ("$INSTDIR", "$APPDATA")
    for arg in delete_lines:
        assert "vault" not in arg.lower() and "meetings_root" not in arg.lower(), (
            f"Delete/RMDir must never reference the vault, got {arg!r}"
        )
        assert arg.startswith(allowed_roots), (
            f"Delete/RMDir argument {arg!r} is not rooted at $INSTDIR or $APPDATA "
            "-- FR-14 requires every delete path to stay inside the app folder "
            "or the app's own %APPDATA% folder, never the vault"
        )


def test_uninstall_presents_an_explicit_model_choice_defaulting_to_keep() -> None:
    body = _macro_body(_read_hooks(), "NSIS_HOOK_PREUNINSTALL")
    assert "MB_YESNO" in body, "expected an explicit yes/no choice about the model directory"
    assert "MB_DEFBUTTON2" in body, "expected the default button to be No (keep the model)"
    assert re.search(r"model", body, re.IGNORECASE)


def test_silent_uninstall_branch_never_shows_a_prompt_and_always_keeps() -> None:
    body = _macro_body(_read_hooks(), "NSIS_HOOK_PREUNINSTALL")
    assert "IfSilent" in body, (
        "expected the pre-uninstall hook to branch on IfSilent to distinguish "
        "an automatic upgrade replace (silent) from a genuine user uninstall"
    )
    silent_match = re.search(
        r"transcriber_preuninstall_silent:(.*?)(?=\n\s*transcriber_preuninstall_\w+:|\Z)",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    assert silent_match, "expected a transcriber_preuninstall_silent: label"
    silent_block = silent_match.group(1)
    assert "MessageBox" not in silent_block, (
        "the silent (upgrade) branch must never prompt -- no user is present to answer"
    )


def test_upgrade_preserves_model_by_relocating_out_of_instdir() -> None:
    pre = _macro_body(_read_hooks(), "NSIS_HOOK_PREUNINSTALL")
    post = _macro_body(_read_hooks(), "NSIS_HOOK_POSTUNINSTALL")
    assert re.search(r'Rename\s+"\$INSTDIR\\models"', pre, re.IGNORECASE), (
        "expected pre-uninstall to relocate $INSTDIR\\models before the core "
        "uninstall Section can delete it (R1 mitigation)"
    )
    assert re.search(r"models", post, re.IGNORECASE), (
        "expected post-uninstall to restore (or, on explicit opt-in, discard) "
        "the relocated model directory"
    )


def test_upgrade_preserves_cuda_runtime_by_relocating_out_of_instdir() -> None:
    """E3: the first-run CUDA runtime download (cuda_runtime.py) extracts
    roughly 1.4 GB into $INSTDIR\\runtime -- an upgrade that discarded this
    the way models\\/logs\\/data\\ were already protected would force a
    silent second multi-gigabyte download on next launch (FR-16's own "no
    re-download" regressing for a payload this fix pass added after that
    guarantee was first proven)."""
    pre = _macro_body(_read_hooks(), "NSIS_HOOK_PREUNINSTALL")
    post = _macro_body(_read_hooks(), "NSIS_HOOK_POSTUNINSTALL")
    assert re.search(r'Rename\s+"\$INSTDIR\\runtime"', pre, re.IGNORECASE), (
        "expected pre-uninstall to relocate $INSTDIR\\runtime before the core "
        "uninstall Section can delete it, the same as models\\/logs\\/data\\ "
        "(R1 mitigation)"
    )
    assert re.search(r"runtime", post, re.IGNORECASE), (
        "expected post-uninstall to restore (or, on explicit opt-in, discard) "
        "the relocated CUDA runtime directory"
    )


def test_uninstall_choice_prompt_names_both_the_model_and_runtime_directories() -> None:
    """E14: runtime\\ (E3, ~1.4 GB) ties its fate to the same $R7 keep/delete
    choice as models\\ -- the initial prompt must name both directories, not
    just the model, so the operator knows what "Yes" actually deletes."""
    body = _macro_body(_read_hooks(), "NSIS_HOOK_PREUNINSTALL")
    assert re.search(r"\$INSTDIR\\models", body)
    assert re.search(r"\$INSTDIR\\runtime", body)


def test_uninstall_keep_branch_message_names_both_directories() -> None:
    """E14: a user who answers "No, keep the model" must be told about both
    $INSTDIR\\models and $INSTDIR\\runtime -- the keep-branch confirmation
    previously named only the model, leaving the ~1.4 GB runtime directory
    restored and unmentioned."""
    post = _macro_body(_read_hooks(), "NSIS_HOOK_POSTUNINSTALL")
    keep_message = re.search(
        r'MessageBox\s+MB_OK\|MB_ICONINFORMATION\s+\\\s*\n\s*"([^"]*)"', post
    )
    assert keep_message, "expected the keep-branch confirmation MessageBox"
    message = keep_message.group(1)
    assert "$INSTDIR\\models" in message
    assert "$INSTDIR\\runtime" in message


def test_postuninstall_clears_the_remembered_install_location_registry_key() -> None:
    """Field report (Bug 1): Tauri's generated template writes the last
    successful install's $INSTDIR to
    HKCU\\Software\\<manufacturer>\\<productName> on every install and reads
    it back to pre-fill the *next* install's default directory, but only
    clears it on uninstall when the interactive "delete app data" checkbox
    was checked -- a silent uninstall (the default for repeated dev/
    verification installs, FR-18) leaves it in place. A throwaway
    verification install to a nonstandard directory then silently redirects
    every later install, including a real one, into that same directory.
    NSIS_HOOK_POSTUNINSTALL must clear this key unconditionally so no
    uninstall -- silent or interactive -- can leave that residue behind.
    """
    text = _read_hooks()
    post = _macro_body(text, "NSIS_HOOK_POSTUNINSTALL")
    assert re.search(
        r'DeleteRegKey\s+HKCU\s+"Software\\\$\{TRANSCRIBER_MANUFACTURER\}\\\$\{TRANSCRIBER_PRODUCTNAME\}"',
        post,
    ), (
        "expected NSIS_HOOK_POSTUNINSTALL to unconditionally DeleteRegKey the "
        "remembered install-location key "
        "(HKCU\\Software\\${TRANSCRIBER_MANUFACTURER}\\${TRANSCRIBER_PRODUCTNAME})"
    )
    assert '!define TRANSCRIBER_MANUFACTURER "Transcriber"' in text
    assert '!define TRANSCRIBER_PRODUCTNAME "Transcriber"' in text


def test_silent_mode_parses_vault_option() -> None:
    body = _macro_body(_read_hooks(), "NSIS_HOOK_POSTINSTALL")
    assert "${GetOptions}" in body
    assert '"/VAULT="' in body
    text = _read_hooks()
    assert "/D=" in text, "expected the file to document NSIS's native /D= handling"


def test_vault_config_write_matches_f3_schema() -> None:
    text = _read_hooks()
    assert '"schema_version": 1' in text
    assert '"meetings_root"' in text


def test_vault_config_write_escapes_backslashes_before_the_json_write() -> None:
    # Found empirically (T14, real silent /VAULT= install): NSIS path
    # variables like $7/${VaultPath} hold single backslashes
    # ("C:\Meetings"), and writing that directly into a JSON string literal
    # produces invalid JSON ("\M" is not a legal JSON escape) -- confirmed
    # both by PowerShell's ConvertFrom-Json and Python's json.loads
    # rejecting the real written config.json. The macro must run the vault
    # path through a backslash-doubling step (e.g. WordFunc.nsh's
    # ${WordReplace}) before the `FileWrite` line that embeds it, and the
    # embedded line itself must reference the escaped result, not the raw
    # parameter.
    text = _read_hooks()
    body = _macro_body(text, "TranscriberWriteVaultConfig")

    meetings_root_lines = [
        line for line in body.splitlines() if "meetings_root" in line and "FileWrite" in line
    ]
    assert meetings_root_lines, "expected the meetings_root FileWrite line in TranscriberWriteVaultConfig"
    for line in meetings_root_lines:
        assert "${VaultPath}" not in line, (
            "the meetings_root FileWrite line must not embed the raw, "
            f"unescaped vault path parameter directly: {line!r}"
        )

    assert re.search(r"WordReplace|StrRep", text), (
        "expected a backslash-doubling helper (WordFunc.nsh's ${WordReplace} "
        "or StrFunc.nsh's ${StrRep}) to be used somewhere in the file"
    )


def test_readme_documents_the_hook_contract() -> None:
    assert README_FILE.is_file(), f"expected {README_FILE} to exist"
    readme = README_FILE.read_text(encoding="utf-8")
    for phrase in ("NSIS_HOOK_POSTINSTALL", "/VAULT=", "never", "model"):
        assert phrase in readme, f"expected README to mention {phrase!r}"


def test_relative_path_from_tauri_conf_resolves_to_this_hooks_file() -> None:
    # T6 wires apps/desktop/src-tauri/tauri.conf.json's
    # bundle.windows.nsis.installerHooks at "../../../installer/installer_hooks.nsh".
    # This checks the path arithmetic independently of whether T6 has landed yet.
    resolved = (TAURI_CONF.parent / "../../../installer/installer_hooks.nsh").resolve()
    assert resolved == HOOKS_FILE.resolve(), (
        f"../../../installer/installer_hooks.nsh from {TAURI_CONF.parent} "
        f"resolves to {resolved}, expected {HOOKS_FILE.resolve()}"
    )
