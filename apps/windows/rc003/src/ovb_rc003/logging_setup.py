"""Logging configuration that never touches voice content or device identity.

Callers must only ever log opaque markers (button names, session ids,
sample/frame counts, boolean flags) - never a Bluetooth address, HID
interface path, device token, or decoded voice content. This module doesn't
attempt to scrub arbitrary strings after the fact (a redaction filter would
give false confidence); the guarantee instead comes from code review plus
tests/test_privacy_contract.py, which statically scans this package's source
for the sensitive field names and MAC-address-shaped literals.

``log_dir``/``log_file_path`` (XRBM-029) expose this module's canonical
``%LOCALAPPDATA%\\RemoteMic\\RC003\\logs\\app.log`` location WITHOUT the
side effect ``get_logger()`` has of creating that directory - the settings
window's "open log directory" entry needs to tell a user "this doesn't exist
yet, the app hasn't run" as an honest fact, which would be impossible if
merely asking the question always created the directory first.
``describe_log_location``/``open_log_location`` build on those two path
functions to answer "does it exist" and "open it" respectively, the latter
via an injectable ``_open_directory`` callable (mirrors this package's other
OS-facing modules, e.g. ``single_instance.py``) so
tests/test_logging_setup_location.py can exercise every branch without
actually invoking Windows Explorer.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import config

LOGGER_NAME = "ovb_rc003"
LOG_FILENAME = "app.log"
FALLBACK_LOG_DIRECTORY = "logs-fallback"
EMERGENCY_LOG_DIRECTORY = "RemoteMicRC003-logs"
LOG_INITIALIZATION_FAILED_EXIT_CODE = 14

_configured = False
_active_log_file: Optional[Path] = None
_active_log_root: Optional[Path] = None


class LogInitializationError(RuntimeError):
    """Raised only after every writable log location has been exhausted."""

    def __init__(self, attempted_directories: Iterable[Path]) -> None:
        self.attempted_directories = tuple(attempted_directories)
        locations = ", ".join(str(path) for path in self.attempted_directories)
        super().__init__(f"No writable Remo log directory: {locations}")


def get_logger(
    root: Optional[Path] = None,
    *,
    _temp_directory: Optional[Path] = None,
) -> logging.Logger:
    global _active_log_file, _active_log_root, _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    candidates = _log_file_candidates(root, _temp_directory=_temp_directory)
    attempted_directories: list[Path] = []
    handler: Optional[logging.FileHandler] = None
    selected_path: Optional[Path] = None

    for candidate in candidates:
        directory = candidate.parent
        if directory not in attempted_directories:
            attempted_directories.append(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(candidate, encoding="utf-8")
        except OSError:
            continue
        selected_path = candidate
        break

    if handler is None or selected_path is None:
        raise LogInitializationError(attempted_directories)

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    _active_log_file = selected_path
    _active_log_root = root
    _configured = True
    return logger


def _log_file_candidates(
    root: Optional[Path],
    *,
    _temp_directory: Optional[Path] = None,
) -> tuple[Path, ...]:
    """Ordered, non-destructive destinations for one bridge process."""

    process_id = os.getpid()
    canonical_directory = log_dir(root)
    return (
        canonical_directory / LOG_FILENAME,
        fallback_log_file_path(directory=canonical_directory, process_id=process_id),
        secondary_log_file_path(root, process_id=process_id),
        emergency_log_file_path(
            root,
            process_id=process_id,
            _temp_directory=_temp_directory,
        ),
    )


def log_dir(root: Optional[Path] = None) -> Path:
    """The canonical log directory - does NOT create it (see module
    docstring); only ``get_logger()`` does that, as a side effect of
    actually needing to write to it.
    """

    return (root or config.config_root()) / "logs"


def log_file_path(root: Optional[Path] = None) -> Path:
    return log_dir(root) / LOG_FILENAME


def fallback_log_file_path(
    root: Optional[Path] = None,
    *,
    directory: Optional[Path] = None,
    process_id: Optional[int] = None,
) -> Path:
    """A non-destructive per-process fallback used only when app.log is busy."""

    resolved_directory = directory or log_dir(root)
    resolved_pid = os.getpid() if process_id is None else process_id
    return resolved_directory / f"app-{resolved_pid}.log"


def secondary_log_dir(root: Optional[Path] = None) -> Path:
    """Fallback beside ``logs`` when that directory itself is unusable."""

    return (root or config.config_root()) / FALLBACK_LOG_DIRECTORY


def secondary_log_file_path(
    root: Optional[Path] = None,
    *,
    process_id: Optional[int] = None,
) -> Path:
    resolved_pid = os.getpid() if process_id is None else process_id
    return secondary_log_dir(root) / f"app-{resolved_pid}.log"


def emergency_log_scope(root: Optional[Path] = None) -> str:
    """Stable opaque namespace preventing unrelated TEMP logs from mixing."""

    resolved_root = (root or config.config_root()).resolve(strict=False)
    digest = hashlib.sha256(str(resolved_root).casefold().encode("utf-8")).hexdigest()
    return digest[:12]


def emergency_log_dir(
    root: Optional[Path] = None,
    *,
    _temp_directory: Optional[Path] = None,
) -> Path:
    base = Path(tempfile.gettempdir()) if _temp_directory is None else _temp_directory
    return base / EMERGENCY_LOG_DIRECTORY / emergency_log_scope(root)


def emergency_log_file_path(
    root: Optional[Path] = None,
    *,
    process_id: Optional[int] = None,
    _temp_directory: Optional[Path] = None,
) -> Path:
    resolved_pid = os.getpid() if process_id is None else process_id
    return emergency_log_dir(
        root,
        _temp_directory=_temp_directory,
    ) / f"app-{resolved_pid}.log"


class LogLocationStatus(Enum):
    DIRECTORY_MISSING = "directory_missing"
    FILE_MISSING = "file_missing"
    READY = "ready"


@dataclass(frozen=True)
class LogLocation:
    status: LogLocationStatus
    directory: Path
    file_path: Path


def describe_log_location(
    root: Optional[Path] = None,
    *,
    _temp_directory: Optional[Path] = None,
) -> LogLocation:
    """A pure filesystem check - never creates anything (DoD: "不存在时给出
    诚实提示；不得创建伪日志"). ``FILE_MISSING`` (directory exists, no
    ``app.log`` yet) is distinct from ``DIRECTORY_MISSING`` (the app has
    never even been run on this machine) so a caller can phrase each
    honestly instead of collapsing them into one generic "not found".
    """

    directory = log_dir(root)
    file_path = log_file_path(root)

    # An active bridge knows the exact file it created. Prefer it rather than
    # a stale app.log left by an older process with an exclusive file lock.
    if _active_log_file is not None and _active_log_root == root:
        if _active_log_file.is_file():
            return LogLocation(
                LogLocationStatus.READY,
                _active_log_file.parent,
                _active_log_file,
            )

    candidate_directories = [directory, secondary_log_dir(root)]
    if root is None or _temp_directory is not None:
        candidate_directories.append(
            emergency_log_dir(root, _temp_directory=_temp_directory)
        )

    # Settings can be launched separately from the bridge, so it cannot rely
    # on `_active_log_file`. Search every legitimate destination and choose
    # the newest real log; this is the only honest answer when an old locked
    # canonical app.log coexists with a newer fallback log.
    log_files = [
        candidate
        for candidate_directory in candidate_directories
        for candidate in _newest_log_files(candidate_directory)
    ]
    if log_files:
        newest_file = max(log_files, key=_safe_mtime)
        return LogLocation(LogLocationStatus.READY, newest_file.parent, newest_file)

    if not directory.is_dir():
        return LogLocation(LogLocationStatus.DIRECTORY_MISSING, directory, file_path)
    return LogLocation(LogLocationStatus.FILE_MISSING, directory, file_path)


def _newest_log_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        return [
            candidate
            for candidate in directory.glob("app*.log")
            if candidate.is_file()
        ]
    except OSError:
        return []


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


class LogOpenOutcome(Enum):
    OPENED = "opened"
    DIRECTORY_MISSING = "directory_missing"
    OPEN_FAILED = "open_failed"


@dataclass(frozen=True)
class LogOpenResult:
    outcome: LogOpenOutcome
    location: LogLocation
    error: Optional[str] = None


def _default_open_directory(directory: Path) -> None:
    # os.startfile only exists on Windows (CPython does not define it on
    # other platforms at all) - checked explicitly rather than caught as an
    # AttributeError, so the resulting OSError message is clear about why.
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("os.startfile is only available on Windows")
    startfile(str(directory))


def open_log_location(
    root: Optional[Path] = None,
    *,
    _open_directory: Callable[[Path], None] = _default_open_directory,
) -> LogOpenResult:
    """Opens the log directory in the OS file browser - never the log FILE
    directly, since which application handles a bare ``.log`` extension is
    unpredictable and not this project's concern. Refuses to open (or
    create) a directory that does not exist yet; any exception the actual
    open call raises is caught and reported rather than propagated, since
    this is called directly from a Tk button handler that must not crash
    the settings window over it.
    """

    location = describe_log_location(root)
    if location.status is LogLocationStatus.DIRECTORY_MISSING:
        return LogOpenResult(LogOpenOutcome.DIRECTORY_MISSING, location)
    try:
        _open_directory(location.directory)
    except Exception as exc:  # noqa: BLE001 - report, never crash the settings window
        return LogOpenResult(LogOpenOutcome.OPEN_FAILED, location, error=str(exc))
    return LogOpenResult(LogOpenOutcome.OPENED, location)


def _rust_log_directory() -> Path:
    override = os.environ.get("REMO_LOG_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path(tempfile.gettempdir())
    return root / "Remo" / "rust" / "logs"


_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
_BARE_ADDRESS_RE = re.compile(r"(地址\s*)[0-9A-Fa-f]{12}\b")


def _redact_export_text(text: str) -> str:
    text = _MAC_RE.sub("[redacted]", text)
    return _BARE_ADDRESS_RE.sub(r"\1[redacted]", text)


def _write_redacted(archive: zipfile.ZipFile, source: Path, name: str) -> None:
    text = source.read_text(encoding="utf-8", errors="replace")
    archive.writestr(name, _redact_export_text(text))


def export_support_bundle(root: Optional[Path] = None) -> Path:
    """Pack recent local logs into one zip the user can send back.

    The archive contains run state only: app.log, bridge status, and the
    latest Rust event journals. Those journals already omit audio samples
    and device addresses.
    """

    config_root = root or config.config_root()
    destination_dir = log_dir(config_root)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / f"Remo-support-{stamp}.zip"
    note = (
        "Remo 排错日志。\n"
        "里面只有运行状态、按键名和语音链路结果，没有录音内容。\n"
        "把这个压缩包发给支持人员即可。\n"
    )
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", note)
        app_log = log_file_path(config_root)
        if app_log.is_file():
            _write_redacted(archive, app_log, "app.log")
        status = config_root / "bridge-status.json"
        if status.is_file():
            _write_redacted(archive, status, "bridge-status.json")
        rust_dir = _rust_log_directory()
        if rust_dir.is_dir():
            journals = sorted(
                rust_dir.glob("events-*.jsonl"),
                key=lambda path: path.stat().st_mtime,
            )[-3:]
            for journal in journals:
                _write_redacted(archive, journal, f"rust/{journal.name}")
    return destination
