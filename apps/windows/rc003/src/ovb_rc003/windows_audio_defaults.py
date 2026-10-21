"""Windows default playback and recording devices for the voice route.

Remote speech is played into VB-CABLE's speaker side and picked up from
CABLE Output. Two system defaults break that for an input method:

* The default speakers are the virtual cable. WeChat and Doubao treat
  whatever is playing there as echo and cancel it, so the recognized text
  comes back empty.
* The default microphone is not CABLE Output. An input method that follows
  the system default then records a different microphone.

The inspection repair button can still correct both. Opening Remo also
moves the three recording roles onto CABLE Output, and closing Remo puts
back the microphone that was in use before that. Playback devices are left
alone by that handoff. Device ids stay inside this module and are never
written to the handoff file or returned to the UI. The file keeps only the
friendly names Windows already shows the user.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import Structure, c_ubyte, c_void_p
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import audio_output

_ROLES = ("console", "multimedia", "communications")


class VoiceRouteError(Exception):
    """A default-device change could not be completed. The message is safe
    to show in the settings window: no device id, no path.
    """


@dataclass(frozen=True)
class VoiceRouteSnapshot:
    render: tuple[str, str, str]
    capture: tuple[str, str, str]
    render_devices: tuple[str, ...]
    capture_devices: tuple[str, ...]


@dataclass(frozen=True)
class VoiceRoutePlan:
    capture_name: str = ""
    playback_name: str = ""
    playback_roles: tuple[int, ...] = ()
    playback_blocked: bool = False

    @property
    def changes_anything(self) -> bool:
        return bool(self.capture_name or self.playback_name)


def is_virtual_cable_playback(name: str) -> bool:
    """True for VB-CABLE's speaker side, including the 16-channel endpoint.

    CABLE Output is the microphone side and is not a playback device.
    """

    text = (name or "").strip()
    if not text or audio_output.is_cable_output_endpoint(text):
        return False
    if audio_output.is_cable_input_endpoint(text):
        return True
    folded = text.casefold()
    return "vb-audio" in folded or "vb-cable" in folded


def _is_other_virtual_speaker(name: str) -> bool:
    folded = (name or "").casefold()
    return "virtual" in folded or "todesk" in folded


def cable_is_installed(snapshot: VoiceRouteSnapshot) -> bool:
    if any(audio_output.is_cable_output_endpoint(name) for name in snapshot.capture_devices):
        return True
    if any(is_virtual_cable_playback(name) for name in snapshot.render_devices):
        return True
    if any(is_virtual_cable_playback(name) for name in snapshot.render):
        return True
    return any(audio_output.is_cable_output_endpoint(name) for name in snapshot.capture)


def describe_problems(snapshot: VoiceRouteSnapshot) -> list[str]:
    problems: list[str] = []
    virtual_roles = [
        _ROLES[index]
        for index, name in enumerate(snapshot.render)
        if is_virtual_cable_playback(name)
    ]
    if virtual_roles:
        shown = "、".join(name for name in snapshot.render if is_virtual_cable_playback(name))
        problems.append(
            f"系统默认喇叭是虚拟声卡（{shown}）。"
            "微信、豆包会把这块喇叭里正在播放的声音当成回声消掉，遥控器的语音就变成空白。"
        )
    if any(audio_output.is_cable_output_endpoint(name) for name in snapshot.capture_devices):
        wrong = [
            name
            for name in snapshot.capture
            if not audio_output.is_cable_output_endpoint(name)
        ]
        if wrong:
            problems.append(
                "系统默认麦克风不是 CABLE Output。"
                "跟着系统默认走的输入法听不到遥控器。"
            )
    return problems


def real_playback_target(snapshot: VoiceRouteSnapshot) -> str:
    """Prefer the communications speaker when it is a real device."""

    real = [
        name
        for name in snapshot.render_devices
        if name and not is_virtual_cable_playback(name) and not _is_other_virtual_speaker(name)
    ]
    communications = snapshot.render[2]
    if communications in real:
        return communications
    for name in real:
        folded = name.casefold()
        if (
            "realtek" in folded
            or folded.startswith("扬声器")
            or folded.startswith("speakers")
            or folded.startswith("headphones")
            or "耳机" in name
        ):
            return name
    return real[0] if real else ""


def plan_repair(snapshot: VoiceRouteSnapshot) -> VoiceRoutePlan:
    playback_roles = tuple(
        index for index, name in enumerate(snapshot.render) if is_virtual_cable_playback(name)
    )
    playback_name = real_playback_target(snapshot) if playback_roles else ""
    capture_name = ""
    cable_mics = [
        name for name in snapshot.capture_devices if audio_output.is_cable_output_endpoint(name)
    ]
    if cable_mics and any(
        not audio_output.is_cable_output_endpoint(name) for name in snapshot.capture
    ):
        capture_name = cable_mics[0]
    return VoiceRoutePlan(
        capture_name=capture_name,
        playback_name=playback_name,
        playback_roles=playback_roles if playback_name else (),
        playback_blocked=bool(playback_roles) and not playback_name,
    )


def repair_message(plan: VoiceRoutePlan) -> str:
    if not plan.changes_anything and not plan.playback_blocked:
        return "语音路由已经是对的，没有改动系统设备。"
    parts: list[str] = []
    if plan.playback_name:
        parts.append(f"默认喇叭已改回 {plan.playback_name}。")
    elif plan.playback_blocked:
        parts.append("默认喇叭还是虚拟声卡，没有找到可改回的真实扬声器。")
    if plan.capture_name:
        parts.append(f"默认麦克风已改为 {plan.capture_name}。")
    parts.append("微信输入法如果单独选过麦克风，仍要在输入法里选 CABLE Output。")
    return "".join(parts)


def read_snapshot() -> VoiceRouteSnapshot | None:
    if sys.platform != "win32":
        return None
    return _read_snapshot_windows()


@dataclass(frozen=True)
class CaptureHandoffState:
    owner_pid: int
    capture: tuple[str, str, str]


@dataclass(frozen=True)
class CaptureHandoffResult:
    restore_on_exit: bool
    message: str


def remembered_capture(
    current: Sequence[str], saved: Sequence[str] | None = None
) -> tuple[str, str, str]:
    """Microphones to restore when Remo exits.

    A role already on CABLE Output keeps the previously remembered real
    microphone. The virtual cable itself is never treated as that microphone.
    """

    remembered: list[str] = []
    for index in range(3):
        name = current[index] if index < len(current) else ""
        if name and not audio_output.is_cable_output_endpoint(name):
            remembered.append(name)
            continue
        previous = saved[index] if saved is not None and index < len(saved) else ""
        if previous and audio_output.is_cable_output_endpoint(previous):
            previous = ""
        remembered.append(previous or "")
    return (remembered[0], remembered[1], remembered[2])


def cable_capture_name(snapshot: VoiceRouteSnapshot) -> str:
    for name in tuple(snapshot.capture_devices) + tuple(snapshot.capture):
        if audio_output.is_cable_output_endpoint(name):
            return name
    return ""


def roles_to_switch(current: Sequence[str], cable_name: str) -> tuple[int, ...]:
    if not cable_name:
        return ()
    return tuple(
        index
        for index, name in enumerate(list(current)[:3])
        if not audio_output.is_cable_output_endpoint(name)
    )


def roles_to_restore(
    current: Sequence[str], saved: Sequence[str], available: Sequence[str]
) -> tuple[tuple[int, str], ...]:
    present = {name for name in available if name}
    restored: list[tuple[int, str]] = []
    for index, name in enumerate(list(saved)[:3]):
        if not name or audio_output.is_cable_output_endpoint(name) or name not in present:
            continue
        if index < len(current) and current[index] == name:
            continue
        restored.append((index, name))
    return tuple(restored)


def capture_handoff_enabled() -> bool:
    return os.environ.get("REMO_CAPTURE_HANDOFF", "1") != "0"


def engage_capture_session(
    state_path: Path,
    *,
    read=read_snapshot,
    apply_roles=None,
    pid: int | None = None,
    pid_running=None,
) -> CaptureHandoffResult:
    """Point the system microphone at CABLE Output and remember the previous one."""

    if not capture_handoff_enabled():
        return CaptureHandoffResult(False, "")
    if apply_roles is None:
        apply_roles = _apply_capture_roles
    if pid is None:
        pid = os.getpid()
    if pid_running is None:
        pid_running = _pid_is_running
    snapshot = read()
    if snapshot is None:
        return CaptureHandoffResult(False, "")
    saved = _read_handoff(state_path)
    if saved is not None and saved.owner_pid != pid and pid_running(saved.owner_pid):
        return CaptureHandoffResult(False, "")
    cable_name = cable_capture_name(snapshot)
    previous = remembered_capture(
        snapshot.capture, None if saved is None else saved.capture
    )
    if not cable_name:
        if saved is not None and any(previous):
            _restore_saved(snapshot, previous, apply_roles)
            _delete_handoff(state_path)
            return CaptureHandoffResult(False, "虚拟声卡不在了，默认麦克风已改回原来的设备。")
        return CaptureHandoffResult(False, "")
    switching = roles_to_switch(snapshot.capture, cable_name)
    if switching:
        apply_roles(cable_name, switching)
    if not any(previous):
        _delete_handoff(state_path)
        return CaptureHandoffResult(False, "")
    _write_handoff(state_path, CaptureHandoffState(pid, previous))
    if switching:
        return CaptureHandoffResult(
            True, "默认麦克风已改为虚拟声卡。退出 Remo 后会自动改回原来的麦克风。"
        )
    return CaptureHandoffResult(True, "")


def restore_capture_session(
    state_path: Path,
    *,
    read=read_snapshot,
    apply_roles=None,
    pid: int | None = None,
    pid_running=None,
) -> None:
    """Put the remembered microphone back. Another live Remo keeps ownership."""

    if not capture_handoff_enabled():
        return
    if apply_roles is None:
        apply_roles = _apply_capture_roles
    if pid is None:
        pid = os.getpid()
    if pid_running is None:
        pid_running = _pid_is_running
    saved = _read_handoff(state_path)
    if saved is None:
        return
    if saved.owner_pid != pid and pid_running(saved.owner_pid):
        return
    snapshot = read()
    if snapshot is None:
        return
    _restore_saved(snapshot, saved.capture, apply_roles)
    _delete_handoff(state_path)


def _restore_saved(snapshot: VoiceRouteSnapshot, saved: Sequence[str], apply_roles) -> None:
    assignments = roles_to_restore(snapshot.capture, saved, snapshot.capture_devices)
    grouped: dict[str, list[int]] = {}
    for role, name in assignments:
        grouped.setdefault(name, []).append(role)
    for name, roles in grouped.items():
        apply_roles(name, tuple(roles))


def _apply_capture_roles(friendly_name: str, roles: Sequence[int]) -> None:
    _set_default_by_name(1, friendly_name, roles)


def _read_handoff(path: Path) -> CaptureHandoffState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    capture = raw.get("capture")
    owner = raw.get("owner_pid")
    if not isinstance(capture, list) or not isinstance(owner, int):
        return None
    names = [item if isinstance(item, str) else "" for item in capture[:3]]
    while len(names) < 3:
        names.append("")
    return CaptureHandoffState(owner, (names[0], names[1], names[2]))


def _write_handoff(path: Path, state: CaptureHandoffState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"owner_pid": state.owner_pid, "capture": list(state.capture)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _delete_handoff(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def _pid_is_running(pid: int) -> bool:
    if pid <= 0 or sys.platform != "win32":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


def repair_voice_route() -> str:
    snapshot = read_snapshot()
    if snapshot is None:
        raise VoiceRouteError("当前不是 Windows，没法改系统默认音频设备。")
    if not cable_is_installed(snapshot):
        raise VoiceRouteError("还没有 VB-CABLE，先装好虚拟声卡再修复。")
    plan = plan_repair(snapshot)
    if plan.changes_anything:
        _apply_plan_windows(plan)
    return repair_message(plan)


def _apply_plan_windows(plan: VoiceRoutePlan) -> None:
    try:
        if plan.capture_name:
            _set_default_by_name(1, plan.capture_name, (0, 1, 2))
        if plan.playback_name:
            _set_default_by_name(0, plan.playback_name, plan.playback_roles)
    except VoiceRouteError:
        raise
    except Exception as exc:  # noqa: BLE001 - COM failures become one safe sentence
        raise VoiceRouteError("没能改系统默认设备，请在 Windows 声音设置里手动改。") from exc


def _read_snapshot_windows() -> VoiceRouteSnapshot:
    session = _ComSession()
    try:
        render = tuple(session.default_name(0, role) for role in range(3))
        capture = tuple(session.default_name(1, role) for role in range(3))
        return VoiceRouteSnapshot(
            render=(render[0], render[1], render[2]),
            capture=(capture[0], capture[1], capture[2]),
            render_devices=tuple(session.active_names(0)),
            capture_devices=tuple(session.active_names(1)),
        )
    finally:
        session.close()


def _set_default_by_name(flow: int, friendly_name: str, roles: Sequence[int]) -> None:
    session = _ComSession()
    try:
        device_id = session.find_id(flow, friendly_name)
        if not device_id:
            raise VoiceRouteError("没能改系统默认设备，请在 Windows 声音设置里手动改。")
        for role in roles:
            if session.set_default(device_id, role) != 0:
                raise VoiceRouteError("没能改系统默认设备，请在 Windows 声音设置里手动改。")
    finally:
        session.close()


class _ComSession:
    """A short-lived MMDevice + PolicyConfig session. Ids never leave it."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import HRESULT, POINTER, Structure, byref, c_int, c_void_p, c_wchar_p
        from ctypes import wintypes

        self._ctypes = ctypes
        self._byref = byref
        self._c_void_p = c_void_p
        self._HRESULT = HRESULT
        ole32 = ctypes.WinDLL("ole32")
        self._ole32 = ole32
        ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = HRESULT
        ole32.CoCreateInstance.argtypes = [
            POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)
        ]
        ole32.CoCreateInstance.restype = HRESULT
        ole32.CLSIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
        ole32.CLSIDFromString.restype = HRESULT
        ole32.CoTaskMemFree.argtypes = [c_void_p]
        ole32.CoUninitialize.argtypes = []
        hr = ole32.CoInitializeEx(None, 2) & 0xFFFFFFFF
        self._uninit = hr in (0, 1)
        if hr not in (0, 1, 0x80010106):
            raise VoiceRouteError("没能读取系统默认音频设备。")
        self._enumerator = self._create(
            "{BCDE0395-E52F-467C-8E3D-C4579291692E}",
            "{A95664D2-9614-4F35-A746-DE8DB63617E6}",
        )
        self._policy = self._create(
            "{870af99c-171d-4f9e-af0d-e63df40c2bc9}",
            "{f8679f50-850a-41cf-9c72-430f290290c8}",
        )
        self._name_key = PROPERTYKEY()
        ole32.CLSIDFromString("{a45c254e-df1c-4efd-8020-67d146a850e0}", byref(self._name_key.fmtid))
        self._name_key.pid = 14

    def close(self) -> None:
        self._release(getattr(self, "_policy", None))
        self._release(getattr(self, "_enumerator", None))
        self._policy = None
        self._enumerator = None
        if self._uninit:
            self._ole32.CoUninitialize()
            self._uninit = False

    def default_name(self, flow: int, role: int) -> str:
        ctypes = self._ctypes
        device = self._c_void_p()
        get_default = ctypes.WINFUNCTYPE(
            self._HRESULT, self._c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(self._c_void_p)
        )(self._method(self._enumerator, 4))
        hr = get_default(self._enumerator, flow, role, self._byref(device))
        if hr != 0 or not device:
            return ""
        try:
            return self._friendly_name(device)
        finally:
            self._release(device)

    def active_names(self, flow: int) -> list[str]:
        names: list[str] = []
        for device in self._active_devices(flow):
            try:
                name = self._friendly_name(device)
            finally:
                self._release(device)
            if name and name not in names:
                names.append(name)
        return names

    def find_id(self, flow: int, friendly_name: str) -> str:
        for device in self._active_devices(flow):
            try:
                if self._friendly_name(device) == friendly_name:
                    return self._device_id(device)
            finally:
                self._release(device)
        return ""

    def set_default(self, device_id: str, role: int) -> int:
        ctypes = self._ctypes
        set_default = ctypes.WINFUNCTYPE(
            self._HRESULT, self._c_void_p, ctypes.c_wchar_p, ctypes.c_int
        )(self._method(self._policy, 13))
        return int(set_default(self._policy, device_id, role))

    def _active_devices(self, flow: int):
        ctypes = self._ctypes
        collection = self._c_void_p()
        enum = ctypes.WINFUNCTYPE(
            self._HRESULT,
            self._c_void_p,
            ctypes.c_int,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(self._c_void_p),
        )(self._method(self._enumerator, 3))
        hr = enum(self._enumerator, flow, 1, self._byref(collection))
        if hr != 0 or not collection:
            return
        try:
            count = ctypes.c_uint()
            get_count = ctypes.WINFUNCTYPE(
                self._HRESULT, self._c_void_p, ctypes.POINTER(ctypes.c_uint)
            )(self._method(collection, 3))
            if get_count(collection, self._byref(count)) != 0:
                return
            item = ctypes.WINFUNCTYPE(
                self._HRESULT, self._c_void_p, ctypes.c_uint, ctypes.POINTER(self._c_void_p)
            )(self._method(collection, 4))
            for index in range(count.value):
                device = self._c_void_p()
                if item(collection, index, self._byref(device)) == 0 and device:
                    yield device
        finally:
            self._release(collection)

    def _friendly_name(self, device) -> str:
        ctypes = self._ctypes
        store = self._c_void_p()
        open_store = ctypes.WINFUNCTYPE(
            self._HRESULT, self._c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(self._c_void_p)
        )(self._method(device, 4))
        if open_store(device, 0, self._byref(store)) != 0 or not store:
            return ""
        try:
            value = PROPVARIANT()
            get_value = ctypes.WINFUNCTYPE(
                self._HRESULT,
                self._c_void_p,
                ctypes.POINTER(PROPERTYKEY),
                ctypes.POINTER(PROPVARIANT),
            )(self._method(store, 5))
            if get_value(store, self._byref(self._name_key), self._byref(value)) != 0:
                return ""
            if value.vt == 31 and value.ptr:
                name = ctypes.wstring_at(value.ptr)
                self._ole32.CoTaskMemFree(value.ptr)
                return name or ""
            return ""
        finally:
            self._release(store)

    def _device_id(self, device) -> str:
        ctypes = self._ctypes
        raw = self._c_void_p()
        get_id = ctypes.WINFUNCTYPE(
            self._HRESULT, self._c_void_p, ctypes.POINTER(self._c_void_p)
        )(self._method(device, 5))
        if get_id(device, self._byref(raw)) != 0 or not raw:
            return ""
        try:
            return ctypes.wstring_at(raw) or ""
        finally:
            self._ole32.CoTaskMemFree(raw)

    def _create(self, clsid: str, iid: str):
        cls = GUID()
        interface = GUID()
        self._ole32.CLSIDFromString(clsid, self._byref(cls))
        self._ole32.CLSIDFromString(iid, self._byref(interface))
        pointer = self._c_void_p()
        hr = self._ole32.CoCreateInstance(self._byref(cls), None, 23, self._byref(interface), self._byref(pointer))
        if hr != 0 or not pointer:
            raise VoiceRouteError("没能读取系统默认音频设备。")
        return pointer

    def _method(self, com, index):
        ctypes = self._ctypes
        table = ctypes.cast(com, ctypes.POINTER(self._c_void_p))[0]
        return ctypes.cast(table, ctypes.POINTER(self._c_void_p))[index]

    def _release(self, com) -> None:
        if not com:
            return
        ctypes = self._ctypes
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, self._c_void_p)(self._method(com, 2))
        release(com)


class GUID(Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", c_ubyte * 8),
    )


class PROPERTYKEY(Structure):
    _fields_ = (("fmtid", GUID), ("pid", wintypes.DWORD))


class PROPVARIANT(Structure):
    _fields_ = (
        ("vt", wintypes.WORD),
        ("reserved1", wintypes.WORD),
        ("reserved2", wintypes.WORD),
        ("reserved3", wintypes.WORD),
        ("ptr", c_void_p),
    )
