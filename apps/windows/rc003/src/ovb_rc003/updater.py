"""Small, dependency-free GitHub Release updater for the Remo client.

The updater deliberately downloads a packaged installer rather than trying
to replace files in the running PyInstaller directory.  The public release
page is used because an installed desktop client cannot safely contain an
access token, and the anonymous release API is too easy to rate-limit.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

UPDATE_REPOSITORY = "Ryan-99/remo-windows"
LATEST_RELEASE_URL = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest"
_VERSION_RE = re.compile(r"^(?:v)?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$", re.I)
_TAG_IN_URL_RE = re.compile(r"/releases/tag/((?:v)?\d+(?:\.\d+){0,2})(?:[/?#]|$)", re.I)


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


def _tag_from_release_url(url: str) -> str:
    match = _TAG_IN_URL_RE.search(str(url or ""))
    if not match:
        raise ValueError("无法识别最新版本")
    return match.group(1)


def _latest_release_tag(timeout: float = 8.0) -> str:
    """Read the version from the public release page.

    The REST release API allows only a few anonymous requests per hour and
    answers 403 once that quota is gone. The release page redirects
    ``/releases/latest`` to ``/releases/tag/vX.Y.Z`` without that quota.
    """

    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"Accept": "text/html", "User-Agent": "Remo-Windows-Updater"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
    return _tag_from_release_url(final_url)


def check_latest_release() -> UpdateInfo:
    tag = _latest_release_tag()
    version = tag[1:] if tag[:1].lower() == "v" else tag
    installer_name = f"RemoSetup-{version}.exe"
    return UpdateInfo(
        current_version=_current_release_version(),
        latest_version=tag,
        release_url=f"https://github.com/{UPDATE_REPOSITORY}/releases/tag/{tag}",
        installer_url=(
            f"https://github.com/{UPDATE_REPOSITORY}/releases/download/{tag}/{installer_name}"
        ),
        installer_name=installer_name,
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
