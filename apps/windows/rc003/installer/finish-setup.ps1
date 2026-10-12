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

Write-Log "app=$AppPath"

$winuhid = Join-Path $AppPath "_internal\winuhid\install-winuhid.ps1"
if (Test-Path -LiteralPath $winuhid) {
    Write-Log "installing WinUHid"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $winuhid -Mode InstallElevated
    Write-Log "winuhid exit=$LASTEXITCODE"
} else {
    Write-Log "missing $winuhid"
}

$zip = Get-ChildItem -LiteralPath (Join-Path $AppPath "_internal\vb_cable_bundle") -Filter "VBCABLE_Driver_Pack45.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($zip) {
    $dest = Join-Path $env:TEMP "remo-vbcable"
    if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Expand-Archive -LiteralPath $zip.FullName -DestinationPath $dest -Force
    $setup = Get-ChildItem -LiteralPath $dest -Recurse -Filter "VBCABLE_Setup_x64.exe" | Select-Object -First 1
    if ($setup) {
        Write-Log "launching $($setup.FullName)"
        $proc = Start-Process -FilePath $setup.FullName -WorkingDirectory $setup.DirectoryName -Wait -PassThru
        Write-Log "vbcable exit=$($proc.ExitCode)"
    } else {
        Write-Log "VBCABLE_Setup_x64.exe not found in the zip"
    }
} else {
    Write-Log "VB-CABLE zip not bundled"
}

try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class RemoAudioDefault {
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
    $chosen = [RemoAudioDefault]::PreferCable()
    if ($chosen) { Write-Log "default microphone set to $chosen" }
    else { Write-Log "CABLE Output is not present yet; a restart may be required" }
} catch {
    Write-Log "default microphone step skipped: $($_.Exception.Message)"
}

exit 0
