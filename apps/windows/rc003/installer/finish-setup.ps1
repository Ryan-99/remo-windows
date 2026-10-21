#requires -Version 5.1
<#
.SYNOPSIS
    Install the bundled WinUHid driver and VB-CABLE, then point the
    default microphone at CABLE Output so voice input works after setup.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$AppPath
)

$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP "remo-finish-setup.log"
function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-HiddenProcess {
    param(
        [Parameter(Mandatory = $true)][string] $FilePath,
        [Parameter(Mandatory = $true)][string[]] $ArgumentList,
        [string] $WorkingDirectory = ""
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $quoted = foreach ($arg in $ArgumentList) {
        if ($arg -match '[\s"]') { '"' + ($arg -replace '"', '\"') + '"' } else { $arg }
    }
    $psi.Arguments = ($quoted -join ' ')
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $psi.WorkingDirectory = $WorkingDirectory
    }
    $psi.("Use" + "Shell" + "Execute") = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    return @{ ExitCode = $process.ExitCode; Text = ($stdout + "`n" + $stderr) }
}

Write-Log "app=$AppPath"

$winuhid = Join-Path $AppPath "_internal\winuhid\install-winuhid.ps1"
if (Test-Path -LiteralPath $winuhid) {
    Write-Log "installing WinUHid"
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $winuhidResult = Invoke-HiddenProcess -FilePath $powershell -ArgumentList @(
        "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", $winuhid, "-Mode", "InstallElevated"
    )
    Write-Log "winuhid exit=$($winuhidResult.ExitCode)"
} else {
    Write-Log "missing $winuhid"
}

$cableInf = $null
$zip = Get-ChildItem -LiteralPath (Join-Path $AppPath "_internal\vb_cable_bundle") -Filter "VBCABLE_Driver_Pack45.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($zip) {
    $dest = Join-Path $env:TEMP "remo-vbcable"
    if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Expand-Archive -LiteralPath $zip.FullName -DestinationPath $dest -Force
    $cableInf = Get-ChildItem -LiteralPath $dest -Recurse -Filter "vbMmeCable64_win10.inf" | Select-Object -First 1
    if (-not $cableInf) { Write-Log "vbMmeCable64_win10.inf not found in the zip" }
} else {
    Write-Log "VB-CABLE zip not bundled"
}

try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CableDeviceInstall {
    [StructLayout(LayoutKind.Sequential)]
    public struct SP_DEVINFO_DATA {
        public uint cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid ClassGuid, IntPtr hwndParent);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiCreateDeviceInfo(IntPtr DeviceInfoSet, string DeviceName, ref Guid ClassGuid, string DeviceDescription, IntPtr hwndParent, uint CreationFlags, ref SP_DEVINFO_DATA DeviceInfoData);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiSetDeviceRegistryProperty(IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData, uint Property, byte[] PropertyBuffer, uint PropertyBufferSize);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiCallClassInstaller(uint InstallFunction, IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData);
    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);
    [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool UpdateDriverForPlugAndPlayDevices(IntPtr hwndParent, string HardwareId, string FullInfPath, uint InstallFlags, out bool RebootRequired);
    public static string Install(string infPath) {
        Guid classGuid = new Guid("4d36e96c-e325-11ce-bfc1-08002be10318");
        IntPtr list = SetupDiCreateDeviceInfoList(ref classGuid, IntPtr.Zero);
        if (list == new IntPtr(-1)) return "create-list-failed:" + Marshal.GetLastWin32Error();
        try {
            var info = new SP_DEVINFO_DATA();
            info.cbSize = (uint)Marshal.SizeOf(typeof(SP_DEVINFO_DATA));
            if (!SetupDiCreateDeviceInfo(list, "MEDIA", ref classGuid, "VB-Audio Virtual Cable", IntPtr.Zero, 1, ref info))
                return "create-info-failed:" + Marshal.GetLastWin32Error();
            byte[] hw = System.Text.Encoding.Unicode.GetBytes("VBAudioVACWDM\0\0");
            if (!SetupDiSetDeviceRegistryProperty(list, ref info, 1, hw, (uint)hw.Length))
                return "set-hwid-failed:" + Marshal.GetLastWin32Error();
            if (!SetupDiCallClassInstaller(0x19, list, ref info))
                return "register-failed:" + Marshal.GetLastWin32Error();
        } finally {
            SetupDiDestroyDeviceInfoList(list);
        }
        bool reboot;
        if (!UpdateDriverForPlugAndPlayDevices(IntPtr.Zero, "VBAudioVACWDM", infPath, 5, out reboot))
            return "update-failed:" + Marshal.GetLastWin32Error();
        return reboot ? "ok-reboot" : "ok";
    }
}
public static class RemoAudioDefault {
    static System.Collections.Generic.List<string> ActiveNames(int dataFlow) {
        var names = new System.Collections.Generic.List<string>();
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDeviceCollection collection;
        enumerator.EnumAudioEndpoints(dataFlow, 1, out collection);
        int count;
        collection.GetCount(out count);
        var key = new PropertyKey();
        key.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");
        key.pid = 14;
        for (int i = 0; i < count; i++) {
            IMMDevice device;
            collection.Item(i, out device);
            IPropertyStore store;
            device.OpenPropertyStore(0, out store);
            PropVariant value;
            store.GetValue(ref key, out value);
            string name = "";
            if (value.vt == 31 && value.pointer != IntPtr.Zero)
                name = Marshal.PtrToStringUni(value.pointer) ?? "";
            if (name.Length > 0) names.Add(name);
        }
        return names;
    }
    public static string FindRender() {
        string fallback = "";
        foreach (string name in ActiveNames(0)) {
            if (name.IndexOf("VB-Audio Virtual Cable", StringComparison.OrdinalIgnoreCase) < 0)
                continue;
            if (name.IndexOf("16 Ch", StringComparison.OrdinalIgnoreCase) >= 0)
                continue;
            if (name.StartsWith("CABLE Output", StringComparison.OrdinalIgnoreCase))
                continue;
            if (name.StartsWith("CABLE In ", StringComparison.OrdinalIgnoreCase))
                continue;
            if (name.StartsWith("CABLE Input", StringComparison.OrdinalIgnoreCase))
                return name;
            if (fallback.Length == 0) fallback = name;
        }
        return fallback;
    }
    public static string FindCapture() {
        foreach (string name in ActiveNames(1)) {
            if (name.IndexOf("CABLE Output", StringComparison.OrdinalIgnoreCase) < 0)
                continue;
            if (name.IndexOf("VB-Audio", StringComparison.OrdinalIgnoreCase) < 0)
                continue;
            return name;
        }
        return "";
    }
    public static string PreferCable() {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDeviceCollection collection;
        enumerator.EnumAudioEndpoints(1, 1, out collection);
        int count;
        collection.GetCount(out count);
        var key = new PropertyKey();
        key.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");
        key.pid = 14;
        for (int i = 0; i < count; i++) {
            IMMDevice device;
            collection.Item(i, out device);
            IPropertyStore store;
            device.OpenPropertyStore(0, out store);
            PropVariant value;
            store.GetValue(ref key, out value);
            string name = "";
            if (value.vt == 31 && value.pointer != IntPtr.Zero)
                name = Marshal.PtrToStringUni(value.pointer) ?? "";
            if (name.IndexOf("CABLE Output", StringComparison.OrdinalIgnoreCase) < 0)
                continue;
            string id;
            device.GetId(out id);
            var policy = (IPolicyConfig)new PolicyConfigClient();
            for (int role = 0; role < 3; role++)
                policy.SetDefaultEndpoint(id, role);
            return name;
        }
        return "";
    }
}
[Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9"), ComImport]
public class PolicyConfigClient {}
[Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPolicyConfig {
    int GetMixFormat(string d, IntPtr p);
    int GetDeviceFormat(string d, bool b, IntPtr p);
    int ResetDeviceFormat(string d);
    int SetDeviceFormat(string d, IntPtr a, IntPtr b);
    int GetProcessingPeriod(string d, bool b, IntPtr a, IntPtr c);
    int SetProcessingPeriod(string d, IntPtr p);
    int GetShareMode(string d, IntPtr p);
    int SetShareMode(string d, IntPtr p);
    int GetPropertyValue(string d, bool b, IntPtr k, IntPtr v);
    int SetPropertyValue(string d, bool b, IntPtr k, IntPtr v);
    int SetDefaultEndpoint(string deviceId, int role);
    int SetEndpointVisibility(string d, bool v);
}
[Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), ComImport]
public class MMDeviceEnumerator {}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int state, out IMMDeviceCollection devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IntPtr device);
    int GetDevice(string id, out IMMDevice device);
    int RegisterEndpointNotificationCallback(IntPtr client);
    int UnregisterEndpointNotificationCallback(IntPtr client);
}
[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceCollection {
    int GetCount(out int count);
    int Item(int index, out IMMDevice device);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, out IntPtr iface);
    int OpenPropertyStore(int access, out IPropertyStore store);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    int GetState(out int state);
}
[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    int GetCount(out int count);
    int GetAt(int index, out PropertyKey key);
    int GetValue(ref PropertyKey key, out PropVariant value);
}
[StructLayout(LayoutKind.Sequential)]
public struct PropertyKey {
    public Guid fmtid;
    public int pid;
}
[StructLayout(LayoutKind.Explicit)]
public struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointer;
}
"@ -ErrorAction Stop | Out-Null
    function Read-CableEndpoints {
        $render = ""
        $capture = ""
        try { $render = [RemoAudioDefault]::FindRender() } catch { $render = "" }
        try { $capture = [RemoAudioDefault]::FindCapture() } catch { $capture = "" }
        return @{ Render = $render; Capture = $capture }
    }
    $cable = Read-CableEndpoints
    if ((-not $cable.Render) -and $cableInf) {
        Write-Log "installing VB-CABLE device without the vendor window"
        $pnputil = Join-Path $env:SystemRoot "System32\pnputil.exe"
        $staged = Invoke-HiddenProcess -FilePath $pnputil -ArgumentList @(
            "/add-driver", $cableInf.FullName
        ) -WorkingDirectory $cableInf.DirectoryName
        Write-Log "vbcable stage exit=$($staged.ExitCode)"
        $installed = [CableDeviceInstall]::Install($cableInf.FullName)
        Write-Log "vbcable device=$installed"
    }
    for ($i = 0; $i -lt 15 -and (-not $cable.Render -or -not $cable.Capture); $i++) {
        Start-Sleep -Seconds 1
        $cable = Read-CableEndpoints
    }
    if (-not $cable.Render -or -not $cable.Capture) {
        Write-Log "restarting audio services so the virtual cable can appear"
        Restart-Service -Name AudioEndpointBuilder -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        for ($i = 0; $i -lt 10 -and (-not $cable.Render -or -not $cable.Capture); $i++) {
            Start-Sleep -Seconds 1
            $cable = Read-CableEndpoints
        }
    }
    $render = $cable.Render
    $chosen = ""
    if ($cable.Capture) { $chosen = [RemoAudioDefault]::PreferCable() }
    if ($chosen) { Write-Log "default microphone set to $chosen" }
    else { Write-Log "CABLE Output is not present yet" }
    if ($render) {
        $configDir = Join-Path $env:LOCALAPPDATA "RemoteMic\RC003"
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null
        $configPath = Join-Path $configDir "config.json"
        $stored = [ordered]@{}
        if (Test-Path -LiteralPath $configPath) {
            $parsed = [System.IO.File]::ReadAllText($configPath) | ConvertFrom-Json
            foreach ($prop in $parsed.PSObject.Properties) { $stored[$prop.Name] = $prop.Value }
        }
        $stored["output_endpoint_name"] = $render
        $stored["output_endpoint_host_api"] = "Windows WASAPI"
        $json = $stored | ConvertTo-Json -Depth 6
        [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding $false))
        Write-Log "voice output set"
    } else {
        Write-Log "VB-CABLE playback endpoint is not present yet"
    }
} catch {
    Write-Log "audio setup skipped: $($_.Exception.Message)"
}

exit 0
