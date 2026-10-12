#requires -Version 5.1
<#
.SYNOPSIS
    Captures all five pages from a frozen Remote Mic settings window.

.DESCRIPTION
    Qt Quick's custom controls are not consistently exposed as UI Automation
    Button elements on every Windows host. This smoke test therefore sends
    ordinary client-area mouse messages to the fixed navigation rail and uses
    PrintWindow to capture the real frozen process. It never starts bridge,
    BLE, HID, audio, or installer code.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class RemoteMicWindowNative {
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out Rect rect);

    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint flags);

    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    [DllImport("user32.dll")]
    public static extern uint GetDpiForWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);
}
"@

# Match the frozen Qt process' per-monitor DPI mode before creating any GDI
# objects. Without this, PrintWindow applies the host's 200% scale twice and
# produces a misleading capture whose right half appears cropped.
[RemoteMicWindowNative]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

function Invoke-ClientClick {
    param([IntPtr]$WindowHandle, [int]$X, [int]$Y)
    $lParam = [IntPtr](($Y -shl 16) -bor ($X -band 0xffff))
    [RemoteMicWindowNative]::SendMessage($WindowHandle, 0x0201, [IntPtr]1, $lParam) | Out-Null
    [RemoteMicWindowNative]::SendMessage($WindowHandle, 0x0202, [IntPtr]0, $lParam) | Out-Null
}

function Save-WindowCapture {
    param([IntPtr]$WindowHandle, [string]$Path)
    $rect = New-Object RemoteMicWindowNative+Rect
    if (-not [RemoteMicWindowNative]::GetWindowRect($WindowHandle, [ref]$rect)) {
        throw "GetWindowRect failed"
    }
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 900 -or $height -lt 620) {
        throw "unexpected settings window size: ${width}x${height}"
    }

    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $hdc = $graphics.GetHdc()
        try {
            if (-not [RemoteMicWindowNative]::PrintWindow($WindowHandle, $hdc, 2)) {
                throw "PrintWindow failed"
            }
        } finally {
            $graphics.ReleaseHdc($hdc)
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$process = Start-Process -FilePath $ExePath -ArgumentList "--settings" -PassThru
try {
    for ($attempt = 0; $attempt -lt 60 -and $process.MainWindowHandle -eq 0; $attempt++) {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
    }
    if ($process.MainWindowHandle -eq 0) {
        throw "settings window did not appear"
    }

    $dpi = [RemoteMicWindowNative]::GetDpiForWindow($process.MainWindowHandle)
    if ($dpi -eq 0) { $dpi = 96 }
    $scale = $dpi / 96.0

    # The settings window owns a 38px in-app title bar.  Client coordinates
    # for the navigation rail therefore start below that bar (the old native
    # title bar used to be outside the client area).
    $pages = @(
        @{ Name = "connection"; Y = 94 },
        @{ Name = "buttons"; Y = 158 },
        @{ Name = "statistics"; Y = 222 },
        @{ Name = "permissions"; Y = 286 },
        @{ Name = "about"; Y = 350 }
    )
    foreach ($page in $pages) {
        Invoke-ClientClick -WindowHandle $process.MainWindowHandle `
            -X ([Math]::Round(56 * $scale)) `
            -Y ([Math]::Round($page.Y * $scale))
        Start-Sleep -Milliseconds 450
        $capturePath = Join-Path $OutputDirectory ("frozen-{0}.png" -f $page.Name)
        Save-WindowCapture -WindowHandle $process.MainWindowHandle -Path $capturePath
        Write-Host $capturePath
    }
} finally {
    if (-not $process.HasExited) {
        $process.CloseMainWindow() | Out-Null
        if (-not $process.WaitForExit(5000)) {
            Stop-Process -Id $process.Id -Force
            $process.WaitForExit()
        }
    }
}

if ($process.ExitCode -ne 0) {
    throw "settings process exited with code $($process.ExitCode)"
}
Write-Host "frozen UI capture passed"
