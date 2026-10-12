"""Optional WinUHid virtual keyboard/consumer-control transport.

The Voice_VibeCoding reference uses WinUHid because several Windows input
methods ignore ``SendInput``/``keybd_event`` for IME wake-up.  This module
keeps that transport optional and explicit: it dynamically loads a
user/vendor-supplied ``WinUHid.dll`` when present, never installs a driver,
and exposes deterministic report builders that can be tested off Windows.

The DLL API is the small C ABI used by the reference project:
``WinUHidGetDriverInterfaceVersion``, ``WinUHidCreateDevice``,
``WinUHidStartDevice``, ``WinUHidSubmitInputReport`` and
``WinUHidDestroyDevice``.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Optional, Sequence


class WinUHidUnavailableError(RuntimeError):
    """Raised when the optional virtual-HID transport cannot be used."""


KEYBOARD_REPORT_DESCRIPTOR = bytes(
    (
        0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0,
        0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08,
        0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05,
        0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
        0x95, 0x01, 0x75, 0x03, 0x91, 0x01, 0x95, 0x06, 0x75, 0x08,
        0x15, 0x00, 0x25, 0x73, 0x05, 0x07, 0x19, 0x00, 0x29, 0x73,
        0x81, 0x00, 0xC0,
    )
)
CONSUMER_REPORT_DESCRIPTOR = bytes(
    (0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x15, 0x00, 0x26, 0xFF,
     0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01,
     0x81, 0x00, 0xC0)
)


def modifier_bit(vk: int) -> Optional[int]:
    return {
        0x11: 0x01, 0xA2: 0x01,  # Ctrl
        0x10: 0x02, 0xA0: 0x02,  # Shift
        0x12: 0x04, 0xA4: 0x04,  # Alt
        0x5B: 0x08,  # left GUI
        0xA3: 0x10,  # right Ctrl
        0xA1: 0x20,  # right Shift
        0xA5: 0x40,  # right Alt
        0x5C: 0x80,  # right GUI
    }.get(int(vk))


def consumer_usage(vk: int) -> Optional[int]:
    return {
        0xAD: 0x00E2,  # volume mute
        0xAE: 0x00EA,  # volume down
        0xAF: 0x00E9,  # volume up
        0xB0: 0x00B6,  # next track
        0xB1: 0x00B5,  # previous track
        0xB2: 0x00B7,  # stop
        0xB3: 0x00CD,  # play/pause
        0xB7: 0x0192,  # launch app 2 / calculator
    }.get(int(vk))


def vk_usage(vk: int) -> Optional[int]:
    """Map a Windows VK to the HID keyboard-page usage used by WinUHid."""

    value = int(vk)
    if modifier_bit(value) is not None:
        return None
    fixed = {
        0x08: 0x2A, 0x09: 0x2B, 0x0D: 0x28, 0x1B: 0x29,
        0x20: 0x2C, 0x21: 0x4B, 0x22: 0x4E, 0x23: 0x4D,
        0x24: 0x4A, 0x25: 0x50, 0x26: 0x52, 0x27: 0x4F,
        0x28: 0x51, 0x2D: 0x49, 0x2E: 0x4C, 0x5D: 0x65,
    }
    if value in fixed:
        return fixed[value]
    if 0x70 <= value <= 0x7B:
        return value - 0x70 + 0x3A
    if 0x41 <= value <= 0x5A:
        return value - 0x41 + 0x04
    if 0x31 <= value <= 0x39:
        return value - 0x31 + 0x1E
    if value == 0x30:
        return 0x27
    return None


def build_keyboard_report(vks: Iterable[int]) -> bytes:
    modifier = 0
    usages: list[int] = []
    for raw_vk in vks:
        vk = int(raw_vk)
        bit = modifier_bit(vk)
        if bit is not None:
            modifier |= bit
            continue
        usage = vk_usage(vk)
        if usage is None:
            raise ValueError(f"WinUHid keyboard unsupported VK 0x{vk:02X}")
        if usage not in usages:
            usages.append(usage)
    if len(usages) > 6:
        raise ValueError("WinUHid keyboard supports at most six non-modifier keys")
    report = bytearray(8)
    report[0] = modifier
    report[2:2 + len(usages)] = bytes(usages)
    return bytes(report)


def _modifier_rank(vk: int) -> int:
    if int(vk) in (0x11, 0xA2, 0xA3):
        return 0
    if int(vk) in (0x10, 0xA0, 0xA1):
        return 1
    if int(vk) in (0x5B, 0x5C):
        return 2
    if int(vk) in (0x12, 0xA4, 0xA5):
        return 3
    return 4


def split_modifiers(vks: Sequence[int]) -> tuple[list[int], list[int]]:
    modifiers = [int(vk) for vk in vks if modifier_bit(int(vk)) is not None]
    keys = [int(vk) for vk in vks if modifier_bit(int(vk)) is None]
    modifiers.sort(key=_modifier_rank)
    return modifiers, keys


@dataclass
class _Device:
    handle: Any
    descriptor: Any
    instance_id: Any
    config: Any


class _WinUHidDeviceConfig(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("supported_events", ctypes.c_int32),
        ("vendor_id", ctypes.c_uint16),
        ("product_id", ctypes.c_uint16),
        ("version_number", ctypes.c_uint16),
        ("report_descriptor_length", ctypes.c_uint16),
        ("report_descriptor", ctypes.POINTER(ctypes.c_uint8)),
        ("container_id", ctypes.c_ubyte * 16),
        ("instance_id", ctypes.POINTER(ctypes.c_uint16)),
        ("hardware_ids", ctypes.POINTER(ctypes.c_uint16)),
        ("read_report_period_us", ctypes.c_uint32),
    ]


def dll_candidates_for_roots(roots: Iterable[Path]) -> tuple[Path, ...]:
    """Build deterministic DLL candidates for tests and frozen/source runs."""

    candidates: list[Path] = []
    for root in roots:
        # The manual package is the ABI-matched source of truth.
        candidates.extend((root / "winuhid" / "WinUHid.dll", root / "WinUHid.dll"))
        candidates.append(root / "assets" / "winuhid" / "WinUHid.dll")
    return tuple(dict.fromkeys(candidates))


def dll_candidates() -> tuple[Path, ...]:
    """Return ordered, non-sensitive local DLL candidates."""

    candidates: list[Path] = []
    configured = os.environ.get("REMOTE_MIC_WINUHID_DLL", "").strip()
    if configured:
        candidates.append(Path(configured))
    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    try:
        roots.append(Path(sys.executable).resolve().parent)
    except OSError:
        pass
    roots.append(Path(__file__).resolve().parents[2] / "build" / "third_party")
    candidates.extend(dll_candidates_for_roots(roots))
    return tuple(dict.fromkeys(candidates))


class WinUHidBackend:
    """Thread-safe optional WinUHid keyboard + consumer-control backend."""

    # Voice IMEs do not all accept a multi-modifier chord when every key is
    # presented in one HID report.  In particular, WeChat/WeType expects the
    # first modifier to be observable before the Windows key arrives.  Keep
    # ordinary mapped actions on the fast 4 ms path, but use the longer voice
    # interval in ``press_single`` below.
    MODIFIER_STAGGER_SECONDS = 0.004
    VOICE_MODIFIER_STAGGER_SECONDS = 0.08
    # Driver installation and PnP enumeration can finish after the bridge has
    # already started.  A failed first probe must not make the backend
    # permanently unavailable for the lifetime of that process.
    RETRY_INTERVAL_SECONDS = 2.0

    def __init__(
        self,
        *,
        dll_path: Optional[Path] = None,
        loader=None,
        instance_tag: Optional[str] = None,
    ) -> None:
        self._dll_path = Path(dll_path) if dll_path else None
        self._loader = loader
        self._dll: Any = None
        self._get_version: Any = None
        self._create: Any = None
        self._start: Any = None
        self._submit_fn: Any = None
        self._destroy_fn: Any = None
        self._keyboard: Optional[_Device] = None
        self._consumer: Optional[_Device] = None
        self._attempted = False
        self._last_attempt_monotonic = 0.0
        self._failure_stage = ""
        self._error = ""
        self._lock = RLock()
        # WinUHid exposes the instance id to PnP. Reusing a fixed id after a
        # crashed bridge can leave a stale virtual collection behind and make
        # the next CreateDevice call fail with ERROR_INVALID_PARAMETER (87).
        # Each bridge owns a fresh pair of ids; the tag is injectable so the
        # pure backend tests remain deterministic.
        supplied_tag = str(instance_tag or "").strip()
        self._instance_tag = supplied_tag or f"{os.getpid()}-{uuid.uuid4().hex[:10]}"

    @property
    def error(self) -> str:
        return self._error

    @property
    def failure_stage(self) -> str:
        """The last initialization/report stage that failed, if any."""

        return self._failure_stage

    @property
    def loaded_path(self) -> Optional[Path]:
        return self._dll_path if self._dll is not None else None

    def is_available(self) -> bool:
        with self._lock:
            return self.ensure()

    def ensure(self) -> bool:
        with self._lock:
            if self._keyboard is not None and self._consumer is not None:
                return True
            now = time.monotonic()
            if (
                self._attempted
                and now - self._last_attempt_monotonic < self.RETRY_INTERVAL_SECONDS
            ):
                return False
            self._attempted = True
            self._last_attempt_monotonic = now
            if sys.platform != "win32":
                self._failure_stage = "platform"
                self._error = "WinUHid 不可用 stage=platform: 仅支持 Windows"
                return False
            path = self._dll_path or next((p for p in dll_candidates() if p.is_file()), None)
            if path is None:
                self._failure_stage = "dll_discovery"
                self._error = "WinUHid 不可用 stage=dll_discovery: 未找到 WinUHid.dll"
                return False
            stage = "dll_load"
            try:
                self._dll_path = path
                self._dll = (
                    self._loader(path)
                    if self._loader
                    else ctypes.WinDLL(str(path), use_last_error=True)
                )
                stage = "interface_setup"
                self._configure_functions()
                stage = "interface_version"
                version = int(self._get_version())
                if version < 1:
                    native_error = _last_win32_error()
                    detail = f"驱动接口版本 {version} 不可用"
                    if native_error:
                        detail = f"{detail} {native_error}"
                    raise WinUHidUnavailableError(detail)
                stage = "keyboard_create"
                self._keyboard = self._create_device(
                    KEYBOARD_REPORT_DESCRIPTOR,
                    1,
                    f"RemoteMicKeyboard-{self._instance_tag}",
                )
                try:
                    stage = "consumer_create"
                    self._consumer = self._create_device(
                        CONSUMER_REPORT_DESCRIPTOR,
                        2,
                        f"RemoteMicConsumer-{self._instance_tag}",
                    )
                except Exception:
                    self._destroy(self._keyboard)
                    self._keyboard = None
                    raise
                stage = "keyboard_initial_report"
                self._submit(self._keyboard, bytes(8))
                stage = "consumer_initial_report"
                self._submit(self._consumer, bytes(2))
                self._failure_stage = ""
                self._error = ""
                return True
            except Exception as exc:  # DLL/vendor errors are diagnostic state
                self._failure_stage = stage
                self._error = f"WinUHid 不可用 stage={stage}: {_exception_summary(exc)}"
                self.close()
                return False

    def _configure_functions(self) -> None:
        self._get_version = self._dll.WinUHidGetDriverInterfaceVersion
        self._get_version.restype = ctypes.c_uint32
        self._create = self._dll.WinUHidCreateDevice
        self._create.argtypes = [ctypes.POINTER(_WinUHidDeviceConfig)]
        self._create.restype = ctypes.c_void_p
        self._start = self._dll.WinUHidStartDevice
        self._start.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._start.restype = ctypes.c_int32
        self._submit_fn = self._dll.WinUHidSubmitInputReport
        self._submit_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32]
        self._submit_fn.restype = ctypes.c_int32
        self._destroy_fn = self._dll.WinUHidDestroyDevice
        self._destroy_fn.argtypes = [ctypes.c_void_p]
        self._destroy_fn.restype = None

    def _create_device(self, descriptor: bytes, product_id: int, instance: str) -> _Device:
        descriptor_buffer = (ctypes.c_uint8 * len(descriptor)).from_buffer_copy(descriptor)
        instance_buffer = (ctypes.c_uint16 * (len(instance) + 1))()
        encoded_instance = instance.encode("utf-16-le")
        for index in range(0, len(encoded_instance), 2):
            if index % 2 == 0:
                instance_buffer[index // 2] = encoded_instance[index] | (encoded_instance[index + 1] << 8)
        config = _WinUHidDeviceConfig(
            0, 0, product_id, 1, len(descriptor),
            ctypes.cast(descriptor_buffer, ctypes.POINTER(ctypes.c_uint8)),
            (ctypes.c_ubyte * 16)(),
            ctypes.cast(instance_buffer, ctypes.POINTER(ctypes.c_uint16)),
            None, 0,
        )
        handle = self._create(ctypes.byref(config))
        if not handle:
            native_error = _last_win32_error()
            detail = f"WinUHidCreateDevice failed for {instance}"
            if native_error:
                detail = f"{detail} {native_error}"
            raise WinUHidUnavailableError(detail)
        if not self._start(handle, None, None):
            self._destroy_fn(handle)
            native_error = _last_win32_error()
            detail = f"WinUHidStartDevice failed for {instance}"
            if native_error:
                detail = f"{detail} {native_error}"
            raise WinUHidUnavailableError(detail)
        return _Device(handle, descriptor_buffer, instance_buffer, config)

    def _destroy(self, device: Optional[_Device]) -> None:
        if device is not None and self._destroy_fn is not None:
            try:
                self._destroy_fn(device.handle)
            except Exception:
                pass

    def _submit(self, device: _Device, report: bytes) -> None:
        payload = (ctypes.c_uint8 * len(report)).from_buffer_copy(report)
        if not self._submit_fn(device.handle, payload, len(report)):
            native_error = _last_win32_error()
            detail = "WinUHidSubmitInputReport failed"
            if native_error:
                detail = f"{detail} {native_error}"
            raise WinUHidUnavailableError(detail)

    def press(self, vks: Sequence[int]) -> None:
        with self._lock:
            if not self.ensure():
                raise WinUHidUnavailableError(self._error or "WinUHid unavailable")
            consumer = next((consumer_usage(vk) for vk in vks if consumer_usage(vk) is not None), None)
            if consumer is not None:
                if len(vks) != 1:
                    raise ValueError("Consumer Control keys cannot be combined")
                self._submit(self._consumer, int(consumer).to_bytes(2, "little"))
                return
            modifiers, keys = split_modifiers(vks)
            if len(modifiers) <= 1:
                self._submit(self._keyboard, build_keyboard_report(vks))
                return
            for step in range(1, len(modifiers) + 1):
                partial = modifiers[:step] + keys
                self._submit(self._keyboard, build_keyboard_report(partial))
                if step < len(modifiers):
                    time.sleep(self.MODIFIER_STAGGER_SECONDS)

    def press_single(self, vks: Sequence[int]) -> None:
        """Submit one complete keyboard report for a voice chord."""

        with self._lock:
            if not self.ensure():
                raise WinUHidUnavailableError(self._error or "WinUHid unavailable")
            if any(consumer_usage(vk) is not None for vk in vks):
                raise ValueError("voice chords cannot contain Consumer Control keys")
            modifiers, keys = split_modifiers(vks)
            if len(modifiers) <= 1:
                self._submit(self._keyboard, build_keyboard_report(vks))
                return
            # Deliver modifier edges as separate reports.  A single report
            # containing Ctrl+Win is accepted by the virtual HID driver but
            # is ignored by some IMEs; the staged reports match a physical
            # chord without changing the public down/up contract.
            for step in range(1, len(modifiers) + 1):
                self._submit(
                    self._keyboard,
                    build_keyboard_report(modifiers[:step] + keys),
                )
                if step < len(modifiers):
                    time.sleep(self.VOICE_MODIFIER_STAGGER_SECONDS)

    def release_single(self) -> None:
        """Release every virtual keyboard key with one all-zero report."""

        with self._lock:
            if not self.ensure():
                raise WinUHidUnavailableError(self._error or "WinUHid unavailable")
            self._submit(self._keyboard, bytes(8))

    def release_all(self) -> None:
        """Best-effort panic release for both virtual HID collections."""

        with self._lock:
            if not self.ensure():
                raise WinUHidUnavailableError(self._error or "WinUHid unavailable")
            first_error: Optional[BaseException] = None
            try:
                self._submit(self._keyboard, bytes(8))
            except BaseException as exc:
                first_error = exc
            try:
                self._submit(self._consumer, bytes(2))
            except BaseException as exc:
                first_error = first_error or exc
            if first_error:
                raise first_error

    def release(self, vks: Sequence[int]) -> None:
        with self._lock:
            if not self.ensure():
                raise WinUHidUnavailableError(self._error or "WinUHid unavailable")
            if any(consumer_usage(vk) is not None for vk in vks):
                self._submit(self._consumer, bytes(2))
                return
            modifiers, keys = split_modifiers(vks)
            first_error: Optional[BaseException] = None
            try:
                if len(modifiers) <= 1:
                    self._submit(self._keyboard, bytes(8))
                else:
                    for held in range(len(modifiers) - 1, -1, -1):
                        self._submit(self._keyboard, build_keyboard_report(modifiers[:held] + keys) if held else bytes(8))
                        if held:
                            time.sleep(self.MODIFIER_STAGGER_SECONDS)
            except BaseException as exc:  # always attempt sanitizer below
                first_error = exc
            try:
                self._submit(self._keyboard, bytes(8))
            except BaseException as exc:
                first_error = first_error or exc
            if first_error:
                raise first_error

    def tap(self, vks: Sequence[int], hold_ms: int = 70) -> None:
        self.press(vks)
        try:
            time.sleep(max(20, min(1000, int(hold_ms))) / 1000.0)
            self.release(vks)
        except Exception:
            try:
                self.release(vks)
            except Exception:
                pass
            raise

    def close(self) -> None:
        with self._lock:
            self._destroy(self._keyboard)
            self._destroy(self._consumer)
            self._keyboard = None
            self._consumer = None
            self._dll = None


def _exception_summary(exc: BaseException) -> str:
    """Keep the useful native failure without dumping a traceback/path."""

    message = str(exc).strip() or "no message"
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        message = f"{message} winerror={int(winerror)}"
    errno = getattr(exc, "errno", None)
    if errno is not None and f"errno={int(errno)}" not in message:
        message = f"{message} errno={int(errno)}"
    return f"{type(exc).__name__}: {message}"


def _last_win32_error() -> str:
    """Return the DLL's last Win32 error without failing off Windows."""

    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return ""
    try:
        code = int(getter())
    except Exception:
        return ""
    if not code:
        return ""
    try:
        message = ctypes.FormatError(code).strip()
    except Exception:
        message = ""
    return f"winerror={code}" + (f" ({message})" if message else "")


_backend: Optional[WinUHidBackend] = None


def get_backend() -> WinUHidBackend:
    global _backend
    if _backend is None:
        _backend = WinUHidBackend()
    return _backend


def reset_backend() -> None:
    global _backend
    if _backend is not None:
        _backend.close()
    _backend = None
