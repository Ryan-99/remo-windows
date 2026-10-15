#requires -Version 5.1
<#
.SYNOPSIS
    Builds an unsigned Remo · RC003 candidate on Windows.

    Steps: create/activate a virtual environment, install
    requirements-dev.txt, run the public-boundary scan, run the test suite
    (gated with ``-W error::ResourceWarning``, matching the CI workflow),
    invoke PyInstaller against RemoteMicRC003.spec, then smoke-check
    the built executable with ``--dry-run`` (imports every module, touches
    no GUI/BLE/HID/audio, exits 0) so a broken build is caught here rather
    than only discovered on a real machine.

    Does NOT fetch Frida Gadget, does NOT request elevation, and does NOT
    sign the resulting binary. The optional VB-CABLE helper is downloaded
    only by the explicit, hash-pinned fetch step below.

    Exit-code gating (XRBM-014 review RETRY P2 #3): PowerShell's
    ``$ErrorActionPreference = "Stop"`` only turns PowerShell-cmdlet errors
    into terminating errors - a NATIVE command (venv creation, pip, python,
    PyInstaller, the built .exe itself) can exit non-zero without PowerShell
    treating that as an error at all, letting the script silently continue
    past a real failure. Every native invocation below is followed by an
    explicit ``$LASTEXITCODE`` check via ``Assert-LastExitCode``.

.PARAMETER PythonExecutable
    Python 3.12 interpreter to create the build environment with. The
    script never reuses the legacy .venv directory because it may point to
    Python 3.14, which can crash PySide6 during process shutdown.
#>

param(
    [string]$PythonExecutable = "",
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$RC003Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function Assert-Python312 {
    param([string]$PythonPath)
    $versionText = (& $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if ($LASTEXITCODE -ne 0 -or ($versionText | Select-Object -Last 1) -ne "3.12") {
        throw "Remo Windows build requires Python 3.12; refusing to use $PythonPath (reported $versionText)"
    }
}

Write-Host "== Remo · RC003 candidate build =="

Push-Location $RC003Root
try {
    $buildVenv = Join-Path $RC003Root ".venv-build-312"
    $venvPython = Join-Path $buildVenv "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        Assert-Python312 $venvPython
    } else {
        if ($PythonExecutable) {
            $resolved = Get-Command $PythonExecutable -ErrorAction SilentlyContinue
            if (-not $resolved) { throw "PythonExecutable was not found: $PythonExecutable" }
            Assert-Python312 $resolved.Source
            & $resolved.Source -m venv $buildVenv
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.12 -m venv $buildVenv
        } else {
            $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if (-not $pythonCommand) { throw "Python 3.12 is required; install it or pass -PythonExecutable." }
            Assert-Python312 $pythonCommand.Source
            & $pythonCommand.Source -m venv $buildVenv
        }
        Assert-LastExitCode "python -m venv"
        $venvPython = Join-Path $buildVenv "Scripts\python.exe"
    }
    Assert-Python312 $venvPython

    # The Python bundled with a newly-created venv already includes pip.
    # Do not unconditionally upgrade it here: that turns every otherwise
    # reproducible build into an unnecessary network operation and can hang
    # indefinitely on restricted/offline Windows hosts after merely printing
    # "Requirement already satisfied".  The lock file below is the actual
    # build input and remains the only dependency-install step.
    & $venvPython -m pip --version
    Assert-LastExitCode "pip --version"
    & $venvPython -m pip install --disable-pip-version-check -r requirements-dev.txt
    Assert-LastExitCode "pip install -r requirements-dev.txt"

    Write-Host "-- public boundary scan --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "check-public-boundary.ps1")
    Assert-LastExitCode "check-public-boundary.ps1"

    # XRBM-031 In-scope item 8: fetch + hash-verify the official VB-CABLE
    # base package BEFORE PyInstaller runs, so the frozen build deterministically
    # bundles it (see build/RemoteMicRC003.spec) and end users of the
    # built candidate never need their own network access to install the
    # optional driver. A download failure or hash mismatch here must fail
    # this whole build closed, not silently produce a candidate with no
    # bundled driver helper.
    Write-Host "-- fetch + verify VB-CABLE driver pack --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "fetch-vb-cable.ps1")
    Assert-LastExitCode "fetch-vb-cable.ps1"

    Write-Host "-- test suite --"
    $env:PYTHONPATH = Join-Path $RC003Root "src"
    # The suite contains an isolated Qt shutdown probe that launches the same
    # discovery command in a child process.  Give both levels an explicit
    # headless QPA backend so a developer/packaging shell with no desktop
    # session cannot leave a native Qt window process waiting for a display.
    $env:QT_QPA_PLATFORM = "offscreen"
    $env:QML_DISABLE_DISK_CACHE = "1"
    & $venvPython -u (Join-Path "tools" "run_test_suite.py")
    Assert-LastExitCode "tools/run_test_suite.py"

    Write-Host "-- PyInstaller build (unsigned candidate) --"
    & $venvPython -m PyInstaller (Join-Path "build" "RemoteMicRC003.spec") --distpath dist --workpath build\pyinstaller-work --noconfirm
    Assert-LastExitCode "PyInstaller"

    Write-Host "-- built-artifact dry-run smoke check (no GUI/BLE/HID/audio) --"
    $builtExe = Join-Path "dist" (Join-Path "RemoteMicRC003" "RemoteMicRC003.exe")
    if (-not (Test-Path $builtExe)) {
        throw "expected built executable not found: $builtExe"
    }
    $dryRunProcess = Start-Process -FilePath $builtExe -ArgumentList "--dry-run" -Wait -PassThru
    if ($dryRunProcess.ExitCode -ne 0) {
        throw "$builtExe --dry-run failed with exit code $($dryRunProcess.ExitCode)"
    }

    Write-Host "-- built-artifact Qt native import smoke check --"
    $qtSmokeProcess = Start-Process -FilePath $builtExe -ArgumentList "--qt-smoke" -Wait -PassThru
    if ($qtSmokeProcess.ExitCode -ne 0) {
        throw "$builtExe --qt-smoke failed with exit code $($qtSmokeProcess.ExitCode)"
    }

    Write-Host "-- package installer, portable ZIP, and checksums --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "package-release.ps1") -IsccPath $IsccPath
    Assert-LastExitCode "package-release.ps1"

    Write-Host "== build complete: dist\release\ (unsigned) =="
} finally {
    Pop-Location
}
