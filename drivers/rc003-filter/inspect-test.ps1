$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bcd = & bcdedit /enum '{current}' 2>&1 | Out-String
$testSigning = $bcd -match '(?im)^\s*testsigning\s+Yes'
$devices = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -match 'VID_2717|Dev_VID&012717' }
[pscustomobject]@{
    TestSigning = $testSigning
    Rc003Devices = @($devices | Select-Object Status,Class,FriendlyName,InstanceId)
    PackageSys = Test-Path (Join-Path $root '.build\driver\RemoRc003Filter.sys')
    PackageInf = Test-Path (Join-Path $root '.build\driver\RemoRc003Filter.inf')
    PackageCat = Test-Path (Join-Path $root '.build\driver\remorc003filter.cat')
} | ConvertTo-Json -Depth 4
