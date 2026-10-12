"""Start and stop the Rust key daemon without launching the old Python bridge."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DAEMON_NAME = "remo-daemon.exe"


def daemon_executable() -> Path:
    override = os.environ.get("REMO_DAEMON")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / DAEMON_NAME
    windows_root = Path(__file__).resolve().parents[3]
    return windows_root / "remo-rust" / "target" / "release" / DAEMON_NAME


def daemon_pids() -> list[int]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value or snapshot == -1:
        return []
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    found: list[int] = []
    try:
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.lower() == DAEMON_NAME:
                found.append(int(entry.th32ProcessID))
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel.CloseHandle(snapshot)
    return found


def is_running() -> bool:
    return bool(daemon_pids())


def start_daemon() -> int:
    if pids := daemon_pids():
        return pids[0]
    executable = daemon_executable()
    if not executable.is_file():
        raise FileNotFoundError(str(executable))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [str(executable), "--backend", "ble", "--execute"],
        cwd=str(executable.parent),
        creationflags=flags,
    )
    return int(process.pid)


def stop_daemon() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    stopped = 0
    for pid in daemon_pids():
        handle = kernel.OpenProcess(0x0001, False, pid)
        if handle and kernel.TerminateProcess(handle, 0):
            stopped += 1
        if handle:
            kernel.CloseHandle(handle)
    return stopped
