#requires -Version 5.1
<#+
.SYNOPSIS
    Runs a reversible current-user install, upgrade, and uninstall smoke test.

.DESCRIPTION
    Uses a unique temporary install directory and Start Menu group. The test
    never starts BLE/audio bridge mode and never installs VB-CABLE. Pass
    -TestDesktopShortcut to validate the optional desktop shortcut only when
    no pre-existing product shortcut is present.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [switch]$TestDesktopShortcut,
    [switch]$KeepEvidence
)

$ErrorActionPreference = "Stop"
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$runId = "{0}-{1}" -f $PID, ([guid]::NewGuid().ToString("N").Substring(0, 8))
$testRoot = Join-Path $env:TEMP "RemoteMicRC003-installer-test-$runId"
$installDir = Join-Path $testRoot "app"
$configDir = Join-Path $testRoot "config"
$groupName = "Remo"
$startMenuRoot = [Environment]::GetFolderPath("Programs")
$startMenuDir = Join-Path $startMenuRoot $groupName
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Remo · RC003.lnk"
$uninstallRegistryKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6E8B6F0-7B9B-4B7C-9E7E-3B7B2C6B0F5C}_is1"
$installLog = Join-Path $testRoot "install.log"
$upgradeLog = Join-Path $testRoot "upgrade.log"
$uninstallLog = Join-Path $testRoot "uninstall.log"
$installed = $false
$uninstaller = Join-Path $installDir "unins000.exe"
$settingsProcess = $null

function Invoke-NativeChecked {
    param([string]$FilePath, [string[]]$Arguments, [string]$Step)
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "$Step failed with exit code $($process.ExitCode)"
    }
}

function Assert-FileExists {
    param([string]$Path, [string]$Description)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }
}

if ($TestDesktopShortcut -and (Test-Path -LiteralPath $desktopShortcut)) {
    throw "desktop shortcut already exists; refusing to overwrite it during installer testing: $desktopShortcut"
}
if (Test-Path -LiteralPath $startMenuDir) {
    throw "Start Menu product group already exists; refusing to overwrite it during installer testing: $startMenuDir"
}
if (Test-Path -LiteralPath $uninstallRegistryKey) {
    throw "Remo is already registered for this user; refusing to replace that installation during testing"
}

New-Item -ItemType Directory -Path $testRoot, $configDir -Force | Out-Null
$baseArgs = @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/DIR=`"$installDir`""
)
if ($TestDesktopShortcut) {
    $baseArgs += '/TASKS="desktopicon"'
} else {
    $baseArgs += '/TASKS="!desktopicon"'
}

try {
    Write-Host "-- clean current-user install --"
    Invoke-NativeChecked $InstallerPath ($baseArgs + "/LOG=`"$installLog`"") "installer clean install"
    $installed = $true

    $appExe = Join-Path $installDir "RemoteMicRC003.exe"
    foreach ($required in @(
        $appExe,
        (Join-Path $installDir "README.txt"),
        (Join-Path $installDir "LICENSE.txt"),
        (Join-Path $installDir "THIRD_PARTY_NOTICES.md"),
        (Join-Path $installDir "stop-app.ps1"),
        $uninstaller
    )) {
        Assert-FileExists $required "installed file"
    }

    # Keep this check encoding-agnostic: Windows PowerShell 5.1 may decode
    # the UTF-8 middle-dot in this script using the active code page, while
    # Inno Setup writes the intended Unicode shortcut names. Verify the
    # installer created the complete five-entry product group without
    # reproducing those non-ASCII names in the assertion itself.
    $startMenuShortcuts = @(Get-ChildItem -LiteralPath $startMenuDir -Filter "*.lnk" -File)
    if ($startMenuShortcuts.Count -ne 5) {
        throw "expected 5 Start Menu shortcuts, found $($startMenuShortcuts.Count)"
    }
    if ($TestDesktopShortcut) {
        Assert-FileExists $desktopShortcut "desktop shortcut"
    }

    Write-Host "-- installed executable dry-run --"
    Invoke-NativeChecked $appExe @("--dry-run") "installed executable --dry-run"

    Write-Host "-- in-place upgrade stops the running settings process --"
    $oldLocalAppData = $env:LOCALAPPDATA
    # The application derives its config root from LOCALAPPDATA. Point only
    # this child process at the isolated test root; never alter the user's
    # real RemoteMic data while checking upgrade/stop behavior.
    $env:LOCALAPPDATA = $configDir
    try {
        $settingsProcess = Start-Process -FilePath $appExe -ArgumentList "--settings" -PassThru
    } finally {
        $env:LOCALAPPDATA = $oldLocalAppData
    }
    Start-Sleep -Seconds 3
    if ($settingsProcess.HasExited) {
        throw "installed settings window exited before the upgrade test"
    }
    Invoke-NativeChecked $InstallerPath ($baseArgs + "/LOG=`"$upgradeLog`"") "installer in-place upgrade"
    $settingsProcess.WaitForExit(10000) | Out-Null
    if (-not $settingsProcess.HasExited) {
        throw "in-place upgrade did not stop the running installed process"
    }
    $settingsProcess = $null
    Invoke-NativeChecked $appExe @("--dry-run") "upgraded executable --dry-run"

    # User data belongs outside the install tree, just like the real config
    # root. The uninstaller may remove {app}; it must never remove this file.
    $sentinel = Join-Path $configDir "user-data-sentinel.json"
    Set-Content -LiteralPath $sentinel -Value '{"preserve":true}' -Encoding ascii

    Write-Host "-- current-user uninstall --"
    $uninstallProcess = Start-Process -FilePath $uninstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$uninstallLog`""
    ) -Wait -PassThru -WindowStyle Hidden
    # Inno Setup may return -1 when a locked file is scheduled for delayed
    # deletion, even though its log records a successful second-phase
    # uninstall. Treat that specific, documented outcome as success only
    # after checking both the log and the installed executable state below.
    if ($uninstallProcess.ExitCode -ne 0 -and $uninstallProcess.ExitCode -ne -1) {
        throw "uninstaller failed with exit code $($uninstallProcess.ExitCode)"
    }
    if ($uninstallProcess.ExitCode -eq -1 -and
        (-not (Test-Path -LiteralPath $uninstallLog -PathType Leaf) -or
         -not ((Get-Content -LiteralPath $uninstallLog -Raw) -match "Uninstallation process succeeded"))) {
        throw "uninstaller returned -1 without a successful Inno Setup log"
    }
    $installed = $false

    if (Test-Path -LiteralPath $appExe) {
        throw "application executable remains after uninstall: $appExe"
    }
    if (Test-Path -LiteralPath $startMenuDir) {
        throw "Start Menu test group remains after uninstall: $startMenuDir"
    }
    if ($TestDesktopShortcut -and (Test-Path -LiteralPath $desktopShortcut)) {
        throw "desktop shortcut remains after uninstall: $desktopShortcut"
    }
    Assert-FileExists $sentinel "user-created sentinel after uninstall"

    Write-Host "installer acceptance passed: install, shortcuts, dry-run, upgrade-stop, uninstall, data preservation"
} finally {
    if ($settingsProcess -and -not $settingsProcess.HasExited) {
        Stop-Process -Id $settingsProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($installed -and (Test-Path -LiteralPath $uninstaller)) {
        Start-Process -FilePath $uninstaller -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
        ) -Wait -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    }
    if (Test-Path -LiteralPath $startMenuDir) {
        Remove-Item -LiteralPath $startMenuDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($TestDesktopShortcut -and (Test-Path -LiteralPath $desktopShortcut)) {
        Remove-Item -LiteralPath $desktopShortcut -Force -ErrorAction SilentlyContinue
    }
    if (-not $KeepEvidence -and (Test-Path -LiteralPath $testRoot)) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "installer test evidence: $testRoot"
    }
}
