<#
.SYNOPSIS
    Developer bootstrap for the Transcriber build (FR-3).

.DESCRIPTION
    Detects every prerequisite this repo's Makefile needs (rust, node, npm,
    uv, make, tauri-cli) plus a diagnostic row for the system `python`
    command (which on many Windows machines is a non-functional Microsoft
    Store stub that must never be silently treated as a real interpreter —
    this project never needs system python; uv manages its own CPython).

    Detection checks the live PATH first, then falls back to each tool's
    well-known install location, because a tool installed by rustup/uv in
    an earlier session may not yet be on the PATH of an already-open shell
    even though it is genuinely installed.

.PARAMETER Check
    Report-only mode: detect and print, never install. Always exits 0,
    even when prerequisites are missing — that is the point of `-Check`,
    it is meant to be run freely and repeatedly.

.PARAMETER Json
    Emit the prerequisite report as a single JSON array on stdout, and
    nothing else. Intended for scripting/tests.

.PARAMETER DryRun
    Like the default (non `-Check`) install-attempt mode, except no
    installer command is actually executed — each missing prerequisite's
    install command is printed instead of run. Still performs the
    "did the attempt satisfy every requirement" gate and its exit code,
    so the gate logic can be verified without mutating the machine.

.NOTES
    Never elevates. If a package manager (winget/scoop) is required and
    absent, the manual command/URL is printed instead of failing opaquely.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Json,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Get-CargoBinDir {
    Join-Path $env:USERPROFILE '.cargo\bin'
}

function Find-ExecutablePath {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string[]]$FallbackPaths = @()
    )
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in $FallbackPaths) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Invoke-VersionProbe {
    param(
        [Parameter(Mandatory)][string]$ExePath,
        [string[]]$ProbeArgs = @('--version')
    )
    try {
        $out = & $ExePath @ProbeArgs 2>$null
        return ($out -join "`n")
    } catch {
        return $null
    }
}

function New-PrereqRow {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]$Present,
        $FoundVersion,
        $InstallCommand
    )
    # $FoundVersion / $InstallCommand are deliberately untyped: a [string]
    # parameter coerces a passed-in $null to '', which would round-trip to
    # JSON as an empty string rather than `null` (FR-3's contract).
    [PSCustomObject][ordered]@{
        name            = $Name
        present         = $Present
        found_version   = $FoundVersion
        install_command = $InstallCommand
    }
}

# ---------------------------------------------------------------------------
# Per-tool detection
# ---------------------------------------------------------------------------

function Test-RustPrereq {
    $cargoBin = Get-CargoBinDir
    $rustcPath = Find-ExecutablePath -Name 'rustc' -FallbackPaths @((Join-Path $cargoBin 'rustc.exe'))
    $installCmd = 'Invoke-WebRequest -Uri https://win.rustup.rs/x86_64 -OutFile "$env:TEMP\rustup-init.exe"; & "$env:TEMP\rustup-init.exe" -y --default-toolchain stable-x86_64-pc-windows-msvc'

    if (-not $rustcPath) {
        return New-PrereqRow -Name 'rust' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }

    $versionOut = Invoke-VersionProbe -ExePath $rustcPath
    $version = $null
    if ($versionOut -and ($versionOut -match 'rustc\s+(\S+)')) { $version = $Matches[1] }

    $hostOut = Invoke-VersionProbe -ExePath $rustcPath -ProbeArgs @('-vV')
    $isMsvc = $false
    if ($hostOut) {
        $hostLine = ($hostOut -split "`n") | Where-Object { $_ -match '^host:\s*(\S+)' }
        if ($hostLine -and $hostLine -match 'msvc') { $isMsvc = $true }
    }

    if (-not $isMsvc) {
        $addTarget = 'rustup toolchain install stable-x86_64-pc-windows-msvc'
        return New-PrereqRow -Name 'rust' -Present $false -FoundVersion $version -InstallCommand $addTarget
    }

    New-PrereqRow -Name 'rust' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-NodePrereq {
    $nodePath = Find-ExecutablePath -Name 'node'
    $installCmd = 'winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements'
    if (-not $nodePath) {
        return New-PrereqRow -Name 'node' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }
    $versionOut = Invoke-VersionProbe -ExePath $nodePath
    $version = $null
    if ($versionOut -and ($versionOut -match 'v?(\d+\.\d+\.\d+)')) { $version = $Matches[1] }
    New-PrereqRow -Name 'node' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-NpmPrereq {
    $npmCmd = Get-Command 'npm' -ErrorAction SilentlyContinue
    $installCmd = 'winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements'
    if (-not $npmCmd) {
        return New-PrereqRow -Name 'npm' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }
    $version = $null
    try {
        $out = & npm --version 2>$null
        if ($out -and ($out -join "`n") -match '(\d+\.\d+\.\d+)') { $version = $Matches[1] }
    } catch { }
    New-PrereqRow -Name 'npm' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-UvPrereq {
    $uvPath = Find-ExecutablePath -Name 'uv' -FallbackPaths @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'))
    $installCmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    if (-not $uvPath) {
        return New-PrereqRow -Name 'uv' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }
    $versionOut = Invoke-VersionProbe -ExePath $uvPath
    $version = $null
    if ($versionOut -and ($versionOut -match 'uv\s+(\S+)')) { $version = $Matches[1] }
    New-PrereqRow -Name 'uv' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-MakePrereq {
    $makePath = Find-ExecutablePath -Name 'make'
    $installCmd = $null
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $installCmd = 'winget install --id ezwinports.make -e --accept-source-agreements --accept-package-agreements'
    } elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
        $installCmd = 'scoop install make'
    } else {
        $installCmd = 'Install GNU Make manually (no winget/scoop found): https://gnuwin32.sourceforge.net/packages/make.htm and add its bin/ to PATH'
    }
    if (-not $makePath) {
        return New-PrereqRow -Name 'make' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }
    $versionOut = Invoke-VersionProbe -ExePath $makePath
    $version = $null
    if ($versionOut -and ($versionOut -match 'Make\s+(\S+)')) { $version = $Matches[1] }
    New-PrereqRow -Name 'make' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-TauriCliPrereq {
    $cargoBin = Get-CargoBinDir
    $tauriPath = Find-ExecutablePath -Name 'cargo-tauri' -FallbackPaths @((Join-Path $cargoBin 'cargo-tauri.exe'))
    $cargoExe = Find-ExecutablePath -Name 'cargo' -FallbackPaths @((Join-Path $cargoBin 'cargo.exe'))
    $installCmd = if ($cargoExe) { "& `"$cargoExe`" install tauri-cli --locked" } else { 'cargo install tauri-cli --locked' }
    if (-not $tauriPath) {
        return New-PrereqRow -Name 'tauri-cli' -Present $false -FoundVersion $null -InstallCommand $installCmd
    }
    $versionOut = Invoke-VersionProbe -ExePath $tauriPath
    $version = $null
    if ($versionOut -and ($versionOut -match '(\d+\.\d+\.\d+)')) { $version = $Matches[1] }
    New-PrereqRow -Name 'tauri-cli' -Present $true -FoundVersion $version -InstallCommand $null
}

function Test-PythonPrereq {
    # This project never needs a system python: uv manages its own CPython
    # (see T4/T5). This row exists purely so bootstrap surfaces the Windows
    # Store's non-functional `python` app-execution alias instead of a
    # confusing downstream failure (FR-3 "does not silently fall back").
    $remedy = 'uv python install 3.12'
    $cmd = Get-Command 'python' -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return New-PrereqRow -Name 'python' -Present $false -FoundVersion $null -InstallCommand $remedy
    }
    if ($cmd.Source -match 'WindowsApps') {
        return New-PrereqRow -Name 'python' -Present 'stub' -FoundVersion $null -InstallCommand $remedy
    }
    $versionOut = $null
    try { $versionOut = (& $cmd.Source --version 2>&1 | Out-String) } catch { }
    if ($versionOut -and ($versionOut -match 'Python\s+(\d+\.\d+\.\d+)')) {
        return New-PrereqRow -Name 'python' -Present $true -FoundVersion $Matches[1] -InstallCommand $null
    }
    New-PrereqRow -Name 'python' -Present 'stub' -FoundVersion $null -InstallCommand $remedy
}

function Get-PrerequisiteReport {
    @(
        Test-RustPrereq
        Test-NodePrereq
        Test-NpmPrereq
        Test-UvPrereq
        Test-MakePrereq
        Test-TauriCliPrereq
        Test-PythonPrereq
    )
}

$script:RequiredNames = @('rust', 'node', 'npm', 'uv', 'make', 'tauri-cli')

function Test-IsSatisfied {
    param($Row)
    $Row.present -eq $true
}

function Write-ReportTable {
    param([Parameter(Mandatory)]$Report)
    $Report |
        Select-Object name, present, found_version, install_command |
        Format-Table -AutoSize |
        Out-String |
        Write-Host
}

function Write-RemainingManualSteps {
    param([Parameter(Mandatory)]$Report)
    $missing = $Report | Where-Object { -not (Test-IsSatisfied $_) -and $_.install_command }
    if (-not $missing) {
        Write-Host 'All required prerequisites are present.'
        return
    }
    Write-Host ''
    Write-Host 'Remaining manual steps:'
    foreach ($row in $missing) {
        Write-Host ("  [{0}] {1}" -f $row.name, $row.install_command)
    }
}

function Invoke-Install {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$InstallCommand)
    switch ($Name) {
        'rust' {
            $installer = Join-Path $env:TEMP 'rustup-init.exe'
            Invoke-WebRequest -Uri 'https://win.rustup.rs/x86_64' -OutFile $installer
            & $installer -y --default-toolchain stable-x86_64-pc-windows-msvc
        }
        'node' {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
            } else {
                Write-Warning "No winget found; install Node manually: $InstallCommand"
            }
        }
        'npm' {
            Write-Warning 'npm ships with Node; re-run after installing Node.'
        }
        'uv' {
            powershell -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        }
        'make' {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                winget install --id ezwinports.make -e --accept-source-agreements --accept-package-agreements
            } elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
                scoop install make
            } else {
                Write-Warning "No package manager found; install GNU Make manually: $InstallCommand"
            }
        }
        'tauri-cli' {
            $cargoBin = Get-CargoBinDir
            $cargoExe = Find-ExecutablePath -Name 'cargo' -FallbackPaths @((Join-Path $cargoBin 'cargo.exe'))
            if ($cargoExe) {
                & $cargoExe install tauri-cli --locked
            } else {
                Write-Warning "cargo not found; install Rust first, then: $InstallCommand"
            }
        }
        default {
            Write-Warning "No automatic installer implemented for '$Name'. Run manually: $InstallCommand"
        }
    }
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if ($Check) {
    $report = Get-PrerequisiteReport
    if ($Json) {
        Write-Output (ConvertTo-Json -InputObject $report -Depth 4)
    } else {
        Write-ReportTable -Report $report
        Write-RemainingManualSteps -Report $report
    }
    exit 0
}

$before = Get-PrerequisiteReport
$toInstall = $before | Where-Object { $script:RequiredNames -contains $_.name -and -not (Test-IsSatisfied $_) }

foreach ($row in $toInstall) {
    if ($DryRun) {
        Write-Host "[dry-run] would install '$($row.name)': $($row.install_command)"
    } else {
        Write-Host "Installing '$($row.name)': $($row.install_command)"
        Invoke-Install -Name $row.name -InstallCommand $row.install_command
    }
}

$after = Get-PrerequisiteReport

if ($Json) {
    Write-Output (ConvertTo-Json -InputObject $after -Depth 4)
} else {
    Write-ReportTable -Report $after
    Write-RemainingManualSteps -Report $after
}

$stillMissing = $after | Where-Object { $script:RequiredNames -contains $_.name -and -not (Test-IsSatisfied $_) }

if ($stillMissing) {
    if (-not $Json) {
        Write-Warning 'Bootstrap incomplete: one or more required prerequisites are still missing.'
    }
    exit 1
}

exit 0
