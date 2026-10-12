"""Small, dependency-free GitHub Release updater for the Remo client.

The updater deliberately downloads a signed/packaged installer rather than
trying to replace files in the running PyInstaller directory.  GitHub's public
release API is used because an installed desktop client cannot safely contain
an access token for a private repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__

UPDATE_REPOSITORY = "Ryan-99/remo-windows"
RELEASE_API_URL = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest"
_VERSION_RE = re.compile(r"^(?:v)?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$", re.I)


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    installer_url: str
    installer_name: str

    @property
    def available(self) -> bool:
        return _version_key(self.latest_version) > _version_key(self.current_version)


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())


def _current_release_version() -> str:
    return str(__version__).removesuffix("-candidate")


def _request_json(url: str, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Remo-Windows-Updater",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub release response is not an object")
    return payload


def check_latest_release() -> UpdateInfo:
    payload = _request_json(RELEASE_API_URL)
    tag = str(payload.get("tag_name") or payload.get("name") or "")
    assets = payload.get("assets") or []
    installer = next(
        (
            asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("name", "")).lower().endswith((".exe", ".msi"))
            and asset.get("browser_download_url")
        ),
        None,
    )
    if installer is None:
        raise ValueError("最新 Release 没有可用的 Windows 安装包")
    return UpdateInfo(
        current_version=_current_release_version(),
        latest_version=tag,
        release_url=str(payload.get("html_url") or ""),
        installer_url=str(installer["browser_download_url"]),
        installer_name=str(installer.get("name") or "Remo-Update.exe"),
    )


def download_and_start_installer(info: UpdateInfo, *, timeout: float = 30.0) -> Path:
    """Download a release installer to temp and start it silently.

    The installer is responsible for replacing the installed one-dir client.
    We do not delete or overwrite the running executable from Python.
    """

    target_dir = Path(tempfile.mkdtemp(prefix="remo-update-"))
    target = target_dir / Path(info.installer_name).name
    request = urllib.request.Request(
        info.installer_url,
        headers={"Accept": "application/octet-stream", "User-Agent": "Remo-Windows-Updater"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    if target.stat().st_size < 1024 * 1024:
        raise ValueError("下载的更新包大小异常，已取消安装")
    subprocess.Popen(
        [str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
        cwd=str(target_dir),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return target
