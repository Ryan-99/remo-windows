#requires -Version 5.1
<#+
.SYNOPSIS
    Compiles and stages the unsigned RC003 Windows release artifacts.

.DESCRIPTION
    Requires an existing dist\RemoteMicRC003 PyInstaller one-dir build.
    Compiles the Inno Setup source, creates a versioned portable ZIP, and
    writes a verified SHA256SUMS.txt beside both artifacts in dist\release.
#>

[CmdletBinding()]
param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Assert-NativeSuccess {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function Resolve-Iscc {
    param([string]$RequestedPath)

    $candidates = @()
    if ($RequestedPath) {
        $candidates += $RequestedPath
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    }
    $candidates += (Join-Path $RC003Root ".tools\inno-setup\ISCC.exe")

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "ISCC.exe was not found. Install Inno Setup 6 or pass -IsccPath explicitly."
}

function Copy-RequiredFile {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "required release metadata file not found: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

Push-Location $RC003Root
try {
    $builtDir = Join-Path $RC003Root "dist\RemoteMicRC003"
    $builtExe = Join-Path $builtDir "RemoteMicRC003.exe"
    if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
        throw "expected PyInstaller build not found: $builtExe"
    }

    $issPath = Join-Path $RC003Root "installer\RemoteMicRC003Setup.iss"
    $issText = Get-Content -LiteralPath $issPath -Raw
    $versionMatch = [regex]::Match($issText, '#define AppVersion "([^"]+)"')
    if (-not $versionMatch.Success) {
        throw "could not determine AppVersion from $issPath"
    }
    $version = $versionMatch.Groups[1].Value
    if ($version -notmatch '^[0-9A-Za-z][0-9A-Za-z.-]*$') {
        throw "AppVersion contains unsafe path characters: $version"
    }

    $resolvedIscc = Resolve-Iscc -RequestedPath $IsccPath
    Write-Host "-- compile Inno Setup installer --"
    & $resolvedIscc $issPath
    Assert-NativeSuccess "ISCC.exe"

    $installerDir = Join-Path $RC003Root "dist\installer"
    $installerName = "RemoteMicRC003Setup-$version-unsigned.exe"
    $installerPath = Join-Path $installerDir $installerName
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "compiled installer not found: $installerPath"
    }

    Write-Host "-- create versioned portable ZIP --"
    $portableDir = Join-Path $RC003Root "dist\portable"
    $stagingName = "RemoteMicRC003-$version"
    $stagingDir = Join-Path $portableDir $stagingName
    $zipName = "RemoteMicRC003-$version-portable-unsigned.zip"
    $zipPath = Join-Path $portableDir $zipName

    if (Test-Path -LiteralPath $stagingDir) {
        Remove-Item -LiteralPath $stagingDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
    Copy-Item -Path (Join-Path $builtDir "*") -Destination $stagingDir -Recurse -Force

    $repoRoot = (Resolve-Path (Join-Path $RC003Root "..\..\..")).Path
    Copy-RequiredFile (Join-Path $repoRoot "LICENSE.md") (Join-Path $stagingDir "LICENSE.txt")
    Copy-RequiredFile (Join-Path $repoRoot "COPYRIGHT.md") (Join-Path $stagingDir "COPYRIGHT.txt")
    Copy-RequiredFile (Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") (Join-Path $stagingDir "THIRD_PARTY_NOTICES.md")
    Copy-RequiredFile (Join-Path $RC003Root "ATTRIBUTION.md") (Join-Path $stagingDir "ATTRIBUTION.md")
    Copy-RequiredFile (Join-Path $RC003Root "installer\readme-rc003.txt") (Join-Path $stagingDir "README.txt")
    Copy-RequiredFile (Join-Path $repoRoot "docs\rc003-hardware-acceptance.md") (Join-Path $stagingDir "HARDWARE_ACCEPTANCE.md")

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $stagingDir -DestinationPath $zipPath -CompressionLevel Optimal -ErrorAction Stop

    Write-Host "-- stage and verify release directory --"
    $releaseDir = Join-Path $RC003Root "dist\release"
    if (Test-Path -LiteralPath $releaseDir) {
        Remove-Item -LiteralPath $releaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

    $releaseZipPath = Join-Path $releaseDir $zipName
    $releaseInstallerPath = Join-Path $releaseDir $installerName
    $manifestPath = Join-Path $releaseDir "SHA256SUMS.txt"
    Copy-Item -LiteralPath $zipPath -Destination $releaseZipPath -Force
    Copy-Item -LiteralPath $installerPath -Destination $releaseInstallerPath -Force

    $hashes = @(
        Get-FileHash -LiteralPath $releaseInstallerPath -Algorithm SHA256
        Get-FileHash -LiteralPath $releaseZipPath -Algorithm SHA256
    ) | Sort-Object { Split-Path $_.Path -Leaf }
    $manifestLines = $hashes | ForEach-Object {
        "{0}  {1}" -f $_.Hash.ToLowerInvariant(), (Split-Path $_.Path -Leaf)
    }
    Set-Content -LiteralPath $manifestPath -Value $manifestLines -Encoding ascii

    $releaseFiles = @(Get-ChildItem -LiteralPath $releaseDir -File)
    $expectedNames = @($installerName, $zipName, "SHA256SUMS.txt") | Sort-Object
    $actualNames = @($releaseFiles.Name) | Sort-Object
    if ($releaseFiles.Count -ne 3 -or (Compare-Object $expectedNames $actualNames)) {
        throw "release directory does not contain the exact expected three files"
    }
    $readLines = @(Get-Content -LiteralPath $manifestPath)
    if ($readLines.Count -ne 2) {
        throw "SHA256SUMS.txt must contain exactly two lines"
    }
    foreach ($line in $readLines) {
        if ($line -notmatch '^(?<hash>[0-9a-f]{64})  (?<name>\S.*)$') {
            throw "malformed SHA256SUMS.txt line: $line"
        }
        $listedPath = Join-Path $releaseDir $Matches['name']
        if (-not (Test-Path -LiteralPath $listedPath -PathType Leaf)) {
            throw "SHA256SUMS.txt references a missing file: $($Matches['name'])"
        }
        $actualHash = (Get-FileHash -LiteralPath $listedPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $Matches['hash']) {
            throw "SHA256 mismatch for $($Matches['name'])"
        }
    }

    Write-Host "release preflight passed: $releaseDir"
} finally {
    Pop-Location
}
