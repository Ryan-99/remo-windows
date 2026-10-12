"""Small, privacy-safe status channel between bridge and settings processes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

SCHEMA_VERSION = 1
STATUS_FILENAME = "bridge-status.json"
ACTIVE_STATES = frozenset(
    {"starting", "connecting", "connected", "voice_active", "reconnecting", "stopping"}
)
VALID_STATES = ACTIVE_STATES | {"stopped", "error"}

STATE_TEXT = {
    "stopped": "桥接未运行",
    "starting": "桥接正在启动",
    "connecting": "正在连接 RC003",
    "connected": "RC003 已连接",
    "voice_active": "RC003 已连接，正在接收语音",
    "reconnecting": "连接已断开，正在重连",
    "stopping": "桥接正在停止",
    "error": "桥接运行异常",
}


@dataclass(frozen=True)
class BridgeStatus:
    state: str = "stopped"
    detail: str = ""
    pid: int = 0
    audio_active: bool = False
    attempt: int = 0
    updated_at: str = ""
    schema_version: int = SCHEMA_VERSION
    last_button: str = ""
    last_button_state: str = ""
    button_seq: int = 0
    # Short event history prevents a fast pair of physical presses from
    # being collapsed when the settings window polls the status file.
    button_events: Tuple[Tuple[int, str, str], ...] = ()
    last_action: str = ""
    last_action_state: str = ""
    last_action_error: str = ""
    debug_events: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def _scope_id(config_root: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(config_root)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def status_paths(config_root: Path) -> tuple[Path, Path]:
    canonical = Path(config_root) / STATUS_FILENAME
    fallback = (
        Path(tempfile.gettempdir())
        / "RemoteMicRC003-status"
        / _scope_id(Path(config_root))
        / STATUS_FILENAME
    )
    return canonical, fallback


def write_status_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _parse_status(path: Path) -> Optional[BridgeStatus]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    state = raw.get("state")
    if state not in VALID_STATES:
        return None
    pid = raw.get("pid", 0)
    attempt = raw.get("attempt", 0)
    audio_active = raw.get("audio_active", False)
    detail = raw.get("detail", "")
    updated_at = raw.get("updated_at", "")
    last_button = raw.get("last_button", "")
    last_button_state = raw.get("last_button_state", "")
    button_seq = raw.get("button_seq", 0)
    raw_events = raw.get("button_events", [])
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 0
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 0
        or not isinstance(audio_active, bool)
        or not isinstance(detail, str)
        or not isinstance(updated_at, str)
        or not isinstance(last_button, str)
        or not isinstance(last_button_state, str)
        or isinstance(button_seq, bool)
        or not isinstance(button_seq, int)
        or button_seq < 0
        or not isinstance(raw_events, list)
    ):
        return None
    events = []
    for event in raw_events[-32:]:
        if not isinstance(event, list) or len(event) != 3:
            continue
        seq, button, edge = event
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            continue
        if not isinstance(button, str) or not isinstance(edge, str):
            continue
        events.append((seq, button, edge))
    raw_debug = raw.get("debug_events", [])
    debug_events = tuple(str(item) for item in raw_debug[-40:] if isinstance(item, str)) if isinstance(raw_debug, list) else ()
    return BridgeStatus(
        state=state,
        detail=detail,
        pid=pid,
        audio_active=audio_active,
        attempt=attempt,
        updated_at=updated_at,
        last_button=last_button,
        last_button_state=last_button_state,
        button_seq=button_seq,
        button_events=tuple(events),
        last_action=str(raw.get("last_action", "")),
        last_action_state=str(raw.get("last_action_state", "")),
        last_action_error=str(raw.get("last_action_error", "")),
        debug_events=debug_events,
    )


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            open_process.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                error = ctypes.get_last_error()
                return error != 87  # ERROR_INVALID_PARAMETER means no such PID.
            close_handle(handle)
            return True
        except (AttributeError, OSError):
            # If the Win32 query is unavailable, do not misreport an active
            # bridge as stopped; status ownership will still be checked by PID.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_bridge_status(
    config_root: Path,
    *,
    _is_process_alive: Callable[[int], bool] = is_process_alive,
) -> BridgeStatus:
    candidates = []
    for path in status_paths(Path(config_root)):
        parsed = _parse_status(path)
        if parsed is None:
            continue
        try:
            modified_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, parsed))
    if not candidates:
        return BridgeStatus()
    status = max(candidates, key=lambda item: item[0])[1]
    if status.state in ACTIVE_STATES and not _is_process_alive(status.pid):
        return BridgeStatus(
            state="stopped",
            detail="上一次桥接进程已经退出",
            updated_at=status.updated_at,
        )
    return status


class NullBridgeStatusPublisher:
    def publish(self, **_fields) -> bool:
        return True


class BridgeStatusPublisher:
    def __init__(
        self,
        config_root: Path,
        *,
        pid: Optional[int] = None,
        _writer: Callable[[Path, dict], None] = write_status_atomic,
    ) -> None:
        self._paths = status_paths(Path(config_root))
        self._pid = os.getpid() if pid is None else int(pid)
        self._writer = _writer
        self.last_path: Optional[Path] = None
        self._button_seq = 0
        self._last_button = ""
        self._last_button_state = ""
        self._button_events: list[tuple[int, str, str]] = []
        self._last_action = ""
        self._last_action_state = ""
        self._last_action_error = ""
        self._debug_events: list[str] = []

    def publish(
        self,
        *,
        state: str,
        detail: str = "",
        audio_active: bool = False,
        attempt: int = 0,
        button_id: str = "",
        button_state: str = "",
        action: str = "",
        action_state: str = "",
        action_error: str = "",
    ) -> bool:
        if state not in VALID_STATES:
            raise ValueError(f"unknown bridge state: {state}")
        if button_id:
            self._button_seq += 1
            self._last_button = str(button_id)
            self._last_button_state = str(button_state or "down")
            self._button_events.append(
                (self._button_seq, self._last_button, self._last_button_state)
            )
            del self._button_events[:-32]
        if action:
            self._last_action = str(action)
            self._last_action_state = str(action_state or "started")
            self._last_action_error = str(action_error or "")
            self._debug_events.append(
                f"{self._last_action} · {self._last_action_state}"
                + (f" · {self._last_action_error}" if self._last_action_error else "")
            )
            del self._debug_events[:-40]
        elif button_id:
            self._debug_events.append(f"{button_id} · {button_state or 'down'}")
            del self._debug_events[:-40]
        payload = BridgeStatus(
            state=state,
            detail=str(detail),
            pid=self._pid,
            audio_active=bool(audio_active),
            attempt=max(0, int(attempt)),
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_button=self._last_button,
            last_button_state=self._last_button_state,
            button_seq=self._button_seq,
            button_events=tuple(self._button_events),
            last_action=self._last_action,
            last_action_state=self._last_action_state,
            last_action_error=self._last_action_error,
            debug_events=tuple(self._debug_events),
        ).to_dict()
        for path in self._paths:
            try:
                self._writer(path, payload)
            except OSError:
                continue
            self.last_path = path
            return True
        return False


def describe_status(status: BridgeStatus) -> str:
    return status.detail.strip() or STATE_TEXT.get(status.state, STATE_TEXT["error"])
