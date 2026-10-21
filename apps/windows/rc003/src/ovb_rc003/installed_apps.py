"""Start Menu programs the settings page can bind to a remote button.

A chosen shortcut is resolved to its target executable when that file still
exists. The saved name is what the page shows, and what the daemon uses to
find the shortcut again if the executable path later disappears.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional


def start_menu_roots() -> list[Path]:
    roots = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        root = Path(raw) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if root.is_dir():
            roots.append(root)
    return roots


def list_start_menu_apps(roots: Optional[Iterable[Path]] = None) -> list[dict]:
    found: list[dict] = []
    seen = set()
    for root in roots if roots is not None else start_menu_roots():
        if not root.is_dir():
            continue
        try:
            shortcuts = root.rglob("*.lnk")
        except OSError:
            continue
        for shortcut in shortcuts:
            name = shortcut.stem.strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            found.append({"name": name, "path": str(shortcut)})
    found.sort(key=lambda item: item["name"].casefold())
    return found


def describe_application(
    path: Path, *, resolve: Callable[[Path], str] = None
) -> Optional[dict]:
    if not path.is_file():
        return None
    suffix = path.suffix.casefold()
    if suffix not in {".exe", ".lnk"}:
        return None
    name = path.stem.strip()
    if not name:
        return None
    saved = str(path)
    if suffix == ".lnk":
        target = (resolve or resolve_shortcut)(path)
        if target and Path(target).is_file():
            saved = target
    return {"name": name, "path": saved}


def resolve_shortcut(path: Path) -> str:
    if sys.platform != "win32" or path.suffix.casefold() != ".lnk":
        return ""
    try:
        return _resolve_shortcut_com(path)
    except OSError:
        return ""


def _resolve_shortcut_com(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoCreateInstance.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.HRESULT

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def make_guid(text: str) -> GUID:
        value = GUID()
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
        ole32.CLSIDFromString.restype = ctypes.HRESULT
        if ole32.CLSIDFromString(text, ctypes.byref(value)) < 0:
            raise OSError("bad guid")
        return value

    hr = ole32.CoInitialize(None)
    if hr < 0:
        raise OSError(hr)
    shell_link = ctypes.c_void_p()
    try:
        clsid = make_guid("{00021401-0000-0000-C000-000000000046}")
        iid_link = make_guid("{000214F9-0000-0000-C000-000000000046}")
        iid_file = make_guid("{0000010B-0000-0000-C000-000000000046}")
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, 1, ctypes.byref(iid_link), ctypes.byref(shell_link)
        )
        if hr < 0 or not shell_link:
            raise OSError(hr)
        table = ctypes.cast(shell_link, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        query = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
        )(table[0])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        get_path = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )(table[3])
        file_ptr = ctypes.c_void_p()
        hr = query(shell_link, ctypes.byref(iid_file), ctypes.byref(file_ptr))
        if hr < 0 or not file_ptr:
            raise OSError(hr)
        file_table = ctypes.cast(file_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        load = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD
        )(file_table[5])
        try:
            hr = load(file_ptr, str(path), 0)
            if hr < 0:
                raise OSError(hr)
        finally:
            release(file_table[2])(file_ptr)
        buffer = ctypes.create_unicode_buffer(32768)
        hr = get_path(shell_link, buffer, len(buffer), None, 0)
        if hr < 0:
            raise OSError(hr)
        return buffer.value
    finally:
        if shell_link:
            release_table = ctypes.cast(
                shell_link, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            )[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release_table[2])(shell_link)
        ole32.CoUninitialize()
