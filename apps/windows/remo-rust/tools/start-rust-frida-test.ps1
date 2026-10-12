param(
    [int]$TargetPid = 0,
    [int]$Port = 30684
)

$ErrorActionPreference = "Stop"
$artifactRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\" )).Path
$probe = (Resolve-Path $PSScriptRoot).Path
$workspaceRoot = $null
while ($probe) {
    if (Test-Path (Join-Path $probe "projects\remote-mic-windows")) { $workspaceRoot = $probe; break }
    $parent = Split-Path $probe -Parent
    if ($parent -eq $probe) { break }
    $probe = $parent
}
if (-not $workspaceRoot) { throw "Workspace root containing projects/remote-mic-windows was not found." }
$repoRoot = Join-Path $workspaceRoot "projects\remote-mic-windows"
$sourceRustRoot = Join-Path $repoRoot "apps\windows\remo-rust"
$rustRoot = if (Test-Path (Join-Path $artifactRoot "remo-daemon.exe")) { $artifactRoot } else { $sourceRustRoot }
$daemon = Join-Path $rustRoot "remo-daemon.exe"
if (-not (Test-Path -LiteralPath $daemon)) { $daemon = Join-Path $sourceRustRoot "target\release\remo-daemon.exe"; $rustRoot = $sourceRustRoot }
$python = Join-Path $repoRoot "apps\windows\rc003\.venv-build-312\Scripts\python.exe"
$pythonSrc = Join-Path $repoRoot "apps\windows\rc003\src"

if (-not (Test-Path -LiteralPath $daemon)) { throw "Release daemon not found: $daemon. Run cargo build --release --workspace first." }
if (-not (Test-Path -LiteralPath $python)) { throw "Existing Frida bootstrap Python not found: $python" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this test script from an elevated PowerShell. The bootstrap injector requires SeDebugPrivilege."
}

$env:PYTHONPATH = $pythonSrc
$env:REMOTE_MIC_RC003_HID_TAP_PORT = [string]$Port
if ($TargetPid -le 0) {
    $TargetPid = [int]((& $python -c "from ovb_rc003.frida_hid_tap_runtime import find_rc003_hidogatt_host_pid; value=find_rc003_hidogatt_host_pid(); print(value or '')").Trim())
}
if ($TargetPid -le 0) { throw "No unique RC003 WUDFHost was found. Connect and wake the remote first." }

$logDir = Join-Path $env:LOCALAPPDATA "Remo\rust\frida-test"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:REMO_LOG_DIR = $logDir
$daemonProcess = $null
try {
    $daemonProcess = Start-Process -FilePath $daemon -ArgumentList @("--backend", "frida", "--frida-pid", $TargetPid, "--frida-port", $Port) -WorkingDirectory $rustRoot -NoNewWindow -PassThru
    Start-Sleep -Milliseconds 300
    Write-Host "Rust Frida listener started. WUDFHost PID=$TargetPid port=$Port"
    Write-Host "Preparing the pinned Gadget with the existing verified bootstrap injector..."
    & $python -m ovb_rc003.frida_hid_tap_injector --pid $TargetPid
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap injection failed with exit code $LASTEXITCODE" }
    Write-Host "Gadget injected. Press the physical RC003 buttons and watch the Rust daemon JSON events. Ctrl+C stops the test."
    while (-not $daemonProcess.HasExited) { Start-Sleep -Milliseconds 250 }
    if ($daemonProcess.ExitCode -ne 0) { throw "Rust daemon exited with code $($daemonProcess.ExitCode)" }
}
finally {
    if ($daemonProcess -and -not $daemonProcess.HasExited) { Stop-Process -Id $daemonProcess.Id -Force -ErrorAction SilentlyContinue }
}
