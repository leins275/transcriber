; installer/installer_hooks.nsh
;
; Tauri 2 NSIS installer hooks for Transcriber (FR-8, FR-14, FR-16, FR-18).
;
; Referenced from apps/desktop/src-tauri/tauri.conf.json's
; bundle.windows.nsis.installerHooks as "../../../installer/installer_hooks.nsh"
; (T6). Tauri's generated NSIS template `!include`s this file and, at the
; corresponding point in the generated installer/uninstaller, invokes
; whichever of the four macros below it finds defined:
;
;   NSIS_HOOK_PREINSTALL    -- before files are copied into $INSTDIR
;   NSIS_HOOK_POSTINSTALL   -- after files are copied, install still running
;   NSIS_HOOK_PREUNINSTALL  -- before the core uninstall Section removes files
;   NSIS_HOOK_POSTUNINSTALL -- after the core uninstall Section has run
;
; See installer/README.md for the full contract, the vault-safety
; invariant, and exactly what T14 must still prove empirically (this file
; cannot be compiled here -- `makensis` is not installed on this machine,
; per spec.md's Detected stack table -- so nothing below has been run
; through the real NSIS compiler; only its text shape has been checked).

!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "WordFunc.nsh"

!ifndef TRANSCRIBER_IDENTIFIER
  !define TRANSCRIBER_IDENTIFIER "com.transcriber.desktop"
!endif

; ---------------------------------------------------------------------------
; Shared helper -- write config.json under
; $APPDATA\${TRANSCRIBER_IDENTIFIER}\config.json for the given vault root.
; Matches F3's schema v1 exactly (apps/desktop/src-tauri/src/config.rs):
;   {
;     "schema_version": 1,
;     "meetings_root": "...",
;     "service": { "base_url": null },
;     "model": { "id": null, "path": null }
;   }
; This is a full overwrite, not a merge: /VAULT= is a developer/silent-mode
; convenience (FR-18) for repeated reinstalls, not a general config editor,
; and a fresh install has no prior config.json to merge with. If a later
; wave needs a real JSON merge here, that is new NSIS logic, not something
; this file's static tests currently require.
!macro TranscriberWriteVaultConfig VaultPath
  ; JSON requires a backslash to be escaped as \\ -- ${VaultPath} is a
  ; Windows path with single backslashes (e.g. C:\Meetings), so it must be
  ; doubled before being embedded in a JSON string literal below. Found
  ; empirically (T14): writing it unescaped produced a config.json that
  ; neither Python's json.loads nor PowerShell's ConvertFrom-Json (nor, by
  ; the same rule, serde_json on the app side) could parse.
  ${WordReplace} "${VaultPath}" "\" "\\" "+" $9
  CreateDirectory "$APPDATA\${TRANSCRIBER_IDENTIFIER}"
  FileOpen $8 "$APPDATA\${TRANSCRIBER_IDENTIFIER}\config.json" w
  FileWrite $8 '{$\r$\n'
  FileWrite $8 '  "schema_version": 1,$\r$\n'
  FileWrite $8 '  "meetings_root": "$9",$\r$\n'
  FileWrite $8 '  "service": { "base_url": null },$\r$\n'
  FileWrite $8 '  "model": { "id": null, "path": null }$\r$\n'
  FileWrite $8 '}$\r$\n'
  FileClose $8
!macroend

; ---------------------------------------------------------------------------
; NSIS_HOOK_PREINSTALL
;
; Reserved. Model/log/data preservation across an upgrade is handled below,
; in NSIS_HOOK_PREUNINSTALL / NSIS_HOOK_POSTUNINSTALL, which Tauri's
; template runs against the *old* version's uninstaller before this new
; version's files are copied in. Residual risk for T14 to confirm
; empirically: that the new install's own file-copy step does not clear
; $INSTDIR before NSIS_HOOK_POSTINSTALL restores/creates the three
; subfolders (R1).
!macro NSIS_HOOK_PREINSTALL
!macroend

; ---------------------------------------------------------------------------
; NSIS_HOOK_POSTINSTALL
;
; FR-8: create the three empty, user-writable subfolders. CreateDirectory
; is a no-op when the target already exists, so this is also what makes an
; upgrade non-destructive to an already-populated models\ (FR-16) -- it
; never recreates or clears the directory, only ensures it exists.
!macro NSIS_HOOK_POSTINSTALL
  CreateDirectory "$INSTDIR\models"
  CreateDirectory "$INSTDIR\logs"
  CreateDirectory "$INSTDIR\data"

  ; FR-18: silent-mode vault selection, e.g.
  ;   setup.exe /S /D=C:\Users\me\AppData\Local\Programs\Transcriber /VAULT=D:\Meetings
  ; NSIS's own /D= is parsed by NSIS itself before any hook runs (it must be
  ; the last argument, unquoted) and is already reflected in $INSTDIR here --
  ; nothing for this file to do for /D=.
  ${GetParameters} $6
  ClearErrors
  ${GetOptions} $6 "/VAULT=" $7
  IfErrors transcriber_postinstall_no_vault transcriber_postinstall_vault

  transcriber_postinstall_vault:
    StrCmp $7 "" transcriber_postinstall_no_vault 0
    ; FR-10's validation, re-applied here so silent mode cannot bypass it:
    ; reject a vault path that is empty, not creatable, or inside the
    ; application folder.
    ${StrLoc} $R0 "$7" "$INSTDIR" ">"
    StrCmp $R0 "" transcriber_postinstall_vault_not_inside transcriber_postinstall_vault_rejected

    transcriber_postinstall_vault_not_inside:
      CreateDirectory "$7"
      IfFileExists "$7\*.*" 0 transcriber_postinstall_vault_rejected
      !insertmacro TranscriberWriteVaultConfig "$7"
      Goto transcriber_postinstall_no_vault

    transcriber_postinstall_vault_rejected:
      MessageBox MB_OK|MB_ICONEXCLAMATION \
        "The /VAULT path is invalid (missing, not writable, or inside the application folder) and was ignored:$\r$\n$7"

  transcriber_postinstall_no_vault:
!macroend

; ---------------------------------------------------------------------------
; NSIS_HOOK_PREUNINSTALL
;
; R1 mitigation: whether Tauri's generated uninstall Section recursively
; removes $INSTDIR, or only deletes the files it explicitly installed, is
; not something that can be verified without a real `tauri build` plus an
; install/uninstall cycle (makensis is not installed here -- T14 owns that
; proof). Defensively relocate the three user-writable subfolders out of
; $INSTDIR *before* the core uninstall Section runs, so neither behaviour
; can destroy their contents, then restore or discard them in
; NSIS_HOOK_POSTUNINSTALL depending on the branch decided here.
;
; $R7 records the outcome for NSIS_HOOK_POSTUNINSTALL:
;   "0" = keep the model (default; upgrade, or a real uninstall where the
;         user chose to keep it)
;   "1" = a real uninstall where the user explicitly opted to delete it
!macro NSIS_HOOK_PREUNINSTALL
  StrCpy $R7 "0"

  IfSilent transcriber_preuninstall_silent transcriber_preuninstall_interactive

  transcriber_preuninstall_silent:
    ; A silent uninstall is the automatic "replace the old version" step
    ; that Tauri's NSIS template runs before installing an upgrade (the
    ; standard NSIS convention for a chained/automated uninstall is to
    ; invoke it with /S). No user is present to ask, so always preserve --
    ; FR-16's "no re-download, no re-picking the folder".
    Goto transcriber_preuninstall_relocate

  transcriber_preuninstall_interactive:
    ; A genuine, user-initiated uninstall (Control Panel, or the
    ; installer's own "Uninstall" entry point). FR-14: present an explicit
    ; choice about the multi-gigabyte model directory; default to keep so
    ; nothing is ever silently orphaned with no way to find it.
    ; E14: the CUDA runtime (E3, ~1.4 GB) shares this same keep/delete choice
    ; ($R7) as the model -- name both directories here, and in the
    ; keep-branch confirmation below, so "Choose No" never leaves a
    ; multi-gigabyte directory on disk with no mention of it at all.
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
      "Also delete the downloaded transcription model and GPU runtime files (about 4.4 GB total) at$\r$\n$\r$\n$INSTDIR\models$\r$\n$INSTDIR\runtime$\r$\n$\r$\nChoose No to keep them on disk." \
      IDYES transcriber_preuninstall_delete_model
    Goto transcriber_preuninstall_relocate

    transcriber_preuninstall_delete_model:
      StrCpy $R7 "1"

  transcriber_preuninstall_relocate:
    ; Move (not copy) the three subfolders out of $INSTDIR and into this
    ; app's own %APPDATA% folder, so the core uninstall Section -- whatever
    ; it does to $INSTDIR -- cannot touch their contents.
    IfFileExists "$INSTDIR\models" 0 transcriber_preuninstall_relocate_logs
      CreateDirectory "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp"
      Rename "$INSTDIR\models" "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\models"

  transcriber_preuninstall_relocate_logs:
    IfFileExists "$INSTDIR\logs" 0 transcriber_preuninstall_relocate_data
      CreateDirectory "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp"
      Rename "$INSTDIR\logs" "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\logs"

  transcriber_preuninstall_relocate_data:
    IfFileExists "$INSTDIR\data" 0 transcriber_preuninstall_relocate_runtime
      CreateDirectory "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp"
      Rename "$INSTDIR\data" "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\data"

  transcriber_preuninstall_relocate_runtime:
    ; E3: the first-run CUDA runtime download (windows-installer-build's
    ; Defect 1 fix, cuda_runtime.py) extracts roughly 1.4 GB of wheels into
    ; $INSTDIR\runtime -- an upgrade that lost this would force a silent
    ; second multi-gigabyte download on next launch, exactly the FR-16
    ; regression ("no re-download") this whole hook exists to prevent.
    ; Relocated here unconditionally, the same as logs\/data\; its eventual
    ; fate (restore vs. discard) is decided in NSIS_HOOK_POSTUNINSTALL,
    ; tied to the same keep/delete choice as models\ ($R7) -- both are
    ; large, re-fetchable payloads, so a real uninstall that opts to
    ; discard the model discards the runtime with it rather than silently
    ; orphaning ~1.4 GB with no prompt of its own.
    IfFileExists "$INSTDIR\runtime" 0 transcriber_preuninstall_done
      CreateDirectory "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp"
      Rename "$INSTDIR\runtime" "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\runtime"

  transcriber_preuninstall_done:
!macroend

; ---------------------------------------------------------------------------
; NSIS_HOOK_POSTUNINSTALL
;
; Restores (or, on explicit opt-in, permanently discards) the subfolders
; NSIS_HOOK_PREUNINSTALL relocated. logs\ and data\ are never the
; multi-gigabyte concern FR-14 asks about, so they are always restored
; unconditionally; only models\ branches on $R7.
!macro NSIS_HOOK_POSTUNINSTALL
  IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\logs" 0 transcriber_postuninstall_data
    CreateDirectory "$INSTDIR"
    Rename "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\logs" "$INSTDIR\logs"

  transcriber_postuninstall_data:
  IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\data" 0 transcriber_postuninstall_models
    CreateDirectory "$INSTDIR"
    Rename "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\data" "$INSTDIR\data"

  transcriber_postuninstall_models:
  ${If} $R7 == "1"
    ; Real uninstall, user opted in: discard the relocated model for good.
    IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\models" 0 transcriber_postuninstall_runtime
      RMDir /r "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\models"
  ${Else}
    ; Upgrade, or a real uninstall where the user chose to keep it: put it
    ; back. $INSTDIR itself may have just been removed by the core
    ; uninstall Section, so recreate it first.
    IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\models" 0 transcriber_postuninstall_runtime
      CreateDirectory "$INSTDIR"
      Rename "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\models" "$INSTDIR\models"
      IfSilent transcriber_postuninstall_runtime 0
        ; E14: name both directories -- the CUDA runtime (E3) is restored by
        ; the same $R7 == "0" branch just below, tied to this same choice, so
        ; a user who answered "keep" must be told about both, not just the
        ; model.
        MessageBox MB_OK|MB_ICONINFORMATION \
          "The downloaded model and GPU runtime files were kept at$\r$\n$\r$\n$INSTDIR\models$\r$\n$INSTDIR\runtime$\r$\n$\r$\nDelete these folders by hand if you no longer need them."
  ${EndIf}

  transcriber_postuninstall_runtime:
  ; E3: the CUDA runtime (~1.4 GB, `$INSTDIR\runtime`) follows the same
  ; keep/delete decision as models\ ($R7) -- see NSIS_HOOK_PREUNINSTALL's
  ; relocate comment for the rationale.
  ${If} $R7 == "1"
    IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\runtime" 0 transcriber_postuninstall_done
      RMDir /r "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\runtime"
  ${Else}
    IfFileExists "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\runtime" 0 transcriber_postuninstall_done
      CreateDirectory "$INSTDIR"
      Rename "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp\runtime" "$INSTDIR\runtime"
  ${EndIf}

  transcriber_postuninstall_done:
  RMDir "$APPDATA\${TRANSCRIBER_IDENTIFIER}\_uninstall_tmp"
!macroend
