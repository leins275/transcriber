<#
.SYNOPSIS
Runs the desktop app in dev mode against the folder dev_app_dir.py assembles.

.DESCRIPTION
`make` cannot export environment into a child process, and the engine needs
three variables to find its models (see scripts/dev_app_dir.py). This asks
that script what they should be and sets them for the `tauri dev` it starts,
so the two can never drift apart.

Arguments are passed through to `tauri dev`, e.g.
  scripts\dev.ps1 -- --fake-service
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $TauriArgs
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

$envLines = & uv run --no-project python "$repoRoot\scripts\dev_app_dir.py" --print-env
if ($LASTEXITCODE -ne 0) {
    throw "dev_app_dir.py failed; run it directly to see why"
}

foreach ($line in $envLines) {
    if ($line -match '^([A-Z0-9_]+)=(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
        Write-Host "  $($Matches[1]) = $($Matches[2])" -ForegroundColor DarkGray
    }
}

Write-Host "starting tauri dev..." -ForegroundColor Cyan
& npm --prefix "$repoRoot\apps\desktop" run tauri dev -- @TauriArgs
exit $LASTEXITCODE
