#requires -Version 5.1
<#
.SYNOPSIS
    Build the ready-to-use Remo installer from the current source,
    using the original project's compiler, driver package, and VB-CABLE zip.
#>
$ErrorActionPreference = "Stop"
$RC003 = Split-Path -Parent $PSScriptRoot
$Repo = (Resolve-Path (Join-Path $RC003 "..\..\..")).Path
$Origin = "D:\Claude Code\codex\projects\remote-mic-windows\apps\windows\rc003"
$Python = Join-Path $Origin ".venv-build-312\Scripts\python.exe"
$Iscc = Join-Path $Origin ".tools\inno-setup\ISCC.exe"
$Daemon = Join-Path $Repo "apps\windows\remo-rust\target\release\remo-daemon.exe"

if (-not (Test-Path -LiteralPath $Python)) { throw "Python build environment not found: $Python" }
if (-not (Test-Path -LiteralPath $Iscc)) { throw "Inno Setup compiler not found: $Iscc" }

$third = Join-Path $RC003 "build\third_party"
New-Item -ItemType Directory -Path (Join-Path $third "winuhid") -Force | Out-Null
& robocopy (Join-Path $Origin "build\third_party\winuhid") (Join-Path $third "winuhid") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy winuhid failed: $LASTEXITCODE" }
Copy-Item -LiteralPath (Join-Path $Origin "build\third_party\VBCABLE_Driver_Pack45.zip") -Destination (Join-Path $third "VBCABLE_Driver_Pack45.zip") -Force
$spec = Join-Path $RC003 "build\RemoteMicRC003.spec"
if (-not (Test-Path -LiteralPath $spec)) {
    Copy-Item -LiteralPath (Join-Path $Origin "build\RemoteMicRC003.spec") -Destination $spec -Force
}

Write-Host "-- rust daemon --"
$env:CARGO_TARGET_DIR = Join-Path $Repo "apps\windows\remo-rust\target"
Get-Process remo-daemon -ErrorAction SilentlyContinue | Stop-Process -Force
Push-Location (Join-Path $Repo "apps\windows\remo-rust")
try {
    & cargo build --offline --release --bin remo-daemon
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
} finally {
    Pop-Location
}

Write-Host "-- pyinstaller --"
Push-Location $RC003
try {
    & $Python -m PyInstaller (Join-Path "build" "RemoteMicRC003.spec") --distpath dist --workpath build\pyinstaller-work --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
}

$dist = Join-Path $RC003 "dist\RemoteMicRC003"
if (-not (Test-Path -LiteralPath (Join-Path $dist "RemoteMicRC003.exe"))) {
    throw "frozen app was not produced"
}
Copy-Item -LiteralPath $Daemon -Destination (Join-Path $dist "remo-daemon.exe") -Force
Copy-Item -LiteralPath (Join-Path $third "winuhid\WinUHid.dll") -Destination (Join-Path $dist "WinUHid.dll") -Force

Write-Host "-- installer --"
& $Iscc (Join-Path $RC003 "installer\RemoReadySetup.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

Start-Process -FilePath $Daemon -ArgumentList "--backend","ble","--execute" -WindowStyle Hidden
Write-Host "installer ready"
Get-ChildItem (Join-Path $RC003 "dist\installer\RemoSetup-*.exe") | Select-Object FullName, Length
