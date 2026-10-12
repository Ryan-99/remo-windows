"""Graceful, per-session control channel for the Windows bridge process."""

from __future__ import annotations

import asyncio
import ctypes
import sys
from enum import Enum
from typing import Callable, Optional

STOP_EVENT_NAME = r"Local\RemoteMicRC003.Stop.v1"

_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_POLL_MILLISECONDS = 200


class BridgeControlUnavailableError(RuntimeError):
    pass


class StopRequestOutcome(str, Enum):
    REQUESTED = "requested"
    NOT_RUNNING = "not_running"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class BridgeStopSignal:
    def __init__(
        self,
        handle: int,
        *,
        wait_for_single_object: Callable[[int, int], int],
        set_event: Callable[[int], int],
        close_handle: Callable[[int], int],
    ) -> None:
        self._handle = handle
        self._wait_for_single_object = wait_for_single_object
        self._set_event = set_event
        self._close_handle = close_handle

    def wait(self, timeout_ms: int = _POLL_MILLISECONDS) -> bool:
        if not self._handle:
            raise BridgeControlUnavailableError("bridge stop signal is closed")
        result = int(self._wait_for_single_object(self._handle, timeout_ms))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        if result == _WAIT_FAILED:
            raise BridgeControlUnavailableError("WaitForSingleObject failed")
        raise BridgeControlUnavailableError(
            f"unexpected WaitForSingleObject result: {result}"
        )

    def request_stop(self) -> None:
        if not self._handle or not self._set_event(self._handle):
            raise BridgeControlUnavailableError("SetEvent failed")

    def close(self) -> None:
        handle = self._handle
        self._handle = 0
        if handle and not self._close_handle(handle):
            raise BridgeControlUnavailableError("CloseHandle failed")


def _kernel32():
    if sys.platform != "win32":
        raise BridgeControlUnavailableError(
            "bridge process control is only available on Windows"
        )
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_wchar_p,
    )
    api.CreateEventW.restype = ctypes.c_void_p
    api.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    api.OpenEventW.restype = ctypes.c_void_p
    api.SetEvent.argtypes = (ctypes.c_void_p,)
    api.SetEvent.restype = ctypes.c_int
    api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.restype = ctypes.c_int
    return api


def create_bridge_stop_signal(*, kernel32=None) -> BridgeStopSignal:
    api = kernel32 or _kernel32()
    handle = api.CreateEventW(None, False, False, STOP_EVENT_NAME)
    if not handle:
        raise BridgeControlUnavailableError("CreateEventW failed")
    return BridgeStopSignal(
        int(handle),
        wait_for_single_object=api.WaitForSingleObject,
        set_event=api.SetEvent,
        close_handle=api.CloseHandle,
    )


async def wait_for_stop_request(
    signal: BridgeStopSignal,
    *,
    shutdown_event: Optional[asyncio.Event] = None,
) -> bool:
    """Wait without blocking the asyncio loop.

    Returns ``True`` when another process sets the named event.  The optional
    local shutdown event lets the bridge tear down this waiter without using
    ``SetEvent`` on its own public control channel.  A wait already running in
    the worker thread is bounded by ``_POLL_MILLISECONDS``; only after that
    call returns can the owner safely close the Win32 handle.
    """

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return False
        if await asyncio.to_thread(signal.wait, _POLL_MILLISECONDS):
            return True


def request_bridge_stop(*, kernel32=None) -> StopRequestOutcome:
    try:
        api = kernel32 or _kernel32()
    except BridgeControlUnavailableError:
        return StopRequestOutcome.UNAVAILABLE

    handle: Optional[int] = api.OpenEventW(
        _EVENT_MODIFY_STATE, False, STOP_EVENT_NAME
    )
    if not handle:
        return StopRequestOutcome.NOT_RUNNING
    try:
        return (
            StopRequestOutcome.REQUESTED
            if api.SetEvent(handle)
            else StopRequestOutcome.FAILED
        )
    finally:
        api.CloseHandle(handle)
