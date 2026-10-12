[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Install,
    [switch]$EnableTestSigning
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pkg = Join-Path $root '.build\driver'
$inf = Join-Path $pkg 'RemoRc003Filter.inf'
$sys = Join-Path $pkg 'RemoRc003Filter.sys'
$cat = Join-Path $pkg 'remorc003filter.cat'

if (!(Test-Path $inf) -or !(Test-Path $sys) -or !(Test-Path $cat)) {
    throw 'Run build-driver.cmd first to create the driver package.'
}

$bcd = & bcdedit /enum "{current}" 2>&1 | Out-String
$testSigning = $bcd -match "(?im)^\s*testsigning\s+Yes"
Write-Host "TestSigning=$testSigning"
Write-Host "Package=$pkg"
Write-Host 'The INF targets only the declared RC003 HID child.'

if ($EnableTestSigning) {
    if (!$PSCmdlet.ShouldProcess('current Windows boot entry', 'enable TESTSIGNING')) { return }
    & bcdedit /set "{current}" testsigning on
    if ($LASTEXITCODE -ne 0) { throw "bcdedit failed: $LASTEXITCODE" }
    Write-Warning 'TESTSIGNING requested; reboot Windows before installing.'
}

if (!$Install) {
    Write-Host 'Check-only mode. No driver was installed. Use -Install to call pnputil.'
    return
}

if (!$testSigning -and !$EnableTestSigning) {
    throw 'TESTSIGNING is not enabled. Reboot first, then use -Install.'
}
if (!$PSCmdlet.ShouldProcess($inf, 'install test driver for RC003')) { return }
& pnputil.exe /add-driver $inf /install
if ($LASTEXITCODE -ne 0) { throw "pnputil failed: $LASTEXITCODE" }
Write-Host 'Driver submitted to PnP. Reconnect RC003 and inspect F13-F20 logs.'
