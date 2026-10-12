"""Opt-in login startup for the voice daemon.

The permissions page is the only place this is turned on. The default is
off, and turning it off removes the login entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

VALUE_NAME = "Remo"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_enabled(key_path: str = RUN_KEY) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(str(value).strip())


def apply(enabled: bool, executable: Path, key_path: str = RUN_KEY) -> None:
    if sys.platform != "win32":
        raise OSError("开机自启仅支持 Windows")
    import winreg

    if enabled:
        if not executable.is_file():
            raise FileNotFoundError(str(executable))
        command = f'"{executable}" --backend ble --execute'
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError as exc:
        if getattr(exc, "winerror", None) in {2, 3}:
            return
        raise
