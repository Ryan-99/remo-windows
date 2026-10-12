[CmdletBinding(SupportsShouldProcess)]
param([switch]$DisableTestSigning)

$ErrorActionPreference = 'Stop'
$published = Microsoft.PowerShell.Management\Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
    Where-Object { $_.InfName -like 'oem*.inf' -and $_.DeviceName -like '*Remo RC003*' }
if ($published) {
    foreach ($item in $published) {
        if ($PSCmdlet.ShouldProcess($item.InfName, 'remove Remo RC003 filter package')) {
            & pnputil.exe /delete-driver $item.InfName /uninstall /force
            if ($LASTEXITCODE -ne 0) { throw "pnputil failed: $LASTEXITCODE" }
        }
    }
} else {
    Write-Host 'No published Remo RC003 package found.'
}
if ($DisableTestSigning) {
    if ($PSCmdlet.ShouldProcess('current Windows boot entry', 'disable TESTSIGNING')) {
        & bcdedit /set "{current}" testsigning off
        if ($LASTEXITCODE -ne 0) { throw "bcdedit failed: $LASTEXITCODE" }
        Write-Warning 'TESTSIGNING disable requested; reboot Windows to apply.'
    }
}
