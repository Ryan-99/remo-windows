"""Generic connect/run/cleanup/retry loop, decoupled from any concrete BLE,
HID, or audio backend so it is unit-testable with fake async callables (see
tests/test_connection_supervisor.py) rather than requiring real hardware or
a live WinRT/hidapi/PortAudio runtime.

Added after XRBM-014 review RETRY P1 #2: the previous app.py connected once
and waited forever, with no reconnect loop and no guarantee that cleanup ran
on every failure path. This module makes both properties structural:

- ``cleanup`` always runs after every ``connect``/``wait`` attempt, whether
  that attempt succeeded, raised, or was ended by ``request_reconnect()`` -
  it is the only ``finally`` in the loop body.
- A failed or ended attempt always leads to another ``connect`` attempt
  (after ``retry_delay``), until ``stop()`` is called.

Thread safety (fixed after XRBM-014 review round 2 P1 #5): ``asyncio.Event``
is documented as not thread-safe - only code running on the event loop's own
thread may call ``.set()`` directly. ``request_reconnect()`` is invoked from
WinRT/worker-thread callbacks (a BLE disconnect notification, an ATVV
protocol error decoded on ble_transport_winrt.py's worker thread, a failed
audio-playback write), so it now always hops onto the owning loop via
``loop.call_soon_threadsafe()`` instead of mutating the event directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

ConnectFn = Callable[[], Awaitable[None]]
CleanupFn = Callable[[], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]


class ConnectionSupervisor:
    def __init__(
        self,
        connect: ConnectFn,
        cleanup: CleanupFn,
        retry_delay: float = 2.0,
        max_retry_delay: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
        sleep: SleepFn = asyncio.sleep,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        on_retry: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self._connect = connect
        self._cleanup = cleanup
        self._retry_delay = max(0.0, retry_delay)
        if max_retry_delay is None:
            max_retry_delay = self._retry_delay
        self._max_retry_delay = max(self._retry_delay, max_retry_delay)
        self._next_retry_delay = self._retry_delay
        self._logger = logger
        self._sleep = sleep
        self._on_retry = on_retry
        self._retry_wake = asyncio.Event()
        # Captured at construction time (must happen on the loop that will
        # run run_forever()/stop()) so request_reconnect() can safely hop
        # back onto it from any other thread - see module docstring.
        self._loop = loop or asyncio.get_event_loop()
        self._disconnect_event = asyncio.Event()
        self._stopping = False
        # A BLE/WinRT discovery call can still be awaiting while a local
        # "停止桥接" request arrives. Track the supervisor task and its phase
        # so stop() can cancel only an in-flight connect await, then let this
        # loop's finally block perform the normal full cleanup. Keeping
        # connect() inline preserves the established reconnect scheduling.
        self._run_task: Optional[asyncio.Task] = None
        self._is_connecting = False

        # Exposed for tests to assert on, without needing to intercept the
        # injected connect/cleanup callables themselves.
        self.attempt_count = 0
        self.cleanup_count = 0
        self.last_error: Optional[BaseException] = None

    def request_reconnect(self) -> None:
        """Call from any callback (a BLE disconnect notification, a protocol
        error, a dead HID listener, a failed playback write) to end the
        current attempt early and start a fresh connect/cleanup cycle.

        Safe to call from ANY thread, not only the event loop's own thread
        (XRBM-014 review round 2 P1 #5): hops onto the owning loop via
        ``call_soon_threadsafe`` rather than setting the ``asyncio.Event``
        directly, which is only safe from the loop thread itself.
        """

        self._loop.call_soon_threadsafe(self._disconnect_event.set)

    def wake_retry(self) -> None:
        """Allow an exact-device wake event to interrupt backoff, never reconnect a healthy link."""
        self._loop.call_soon_threadsafe(self._retry_wake.set)

    async def _wait_retry(self, delay: float) -> None:
        sleeper = asyncio.create_task(self._sleep(delay))
        wake = asyncio.create_task(self._retry_wake.wait())
        try:
            done, _ = await asyncio.wait((sleeper, wake), return_when=asyncio.FIRST_COMPLETED)
            if wake in done:
                self._next_retry_delay = self._retry_delay
            if sleeper in done:
                await sleeper
        finally:
            for task in (sleeper, wake):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, wake, return_exceptions=True)
            self._retry_wake.clear()

    async def run_forever(self) -> None:
        self._run_task = asyncio.current_task()
        try:
            while not self._stopping:
                self.attempt_count += 1
                self._disconnect_event.clear()
                connected = False
                try:
                    self.last_error = None
                    self._is_connecting = True
                    try:
                        await self._connect()
                    finally:
                        self._is_connecting = False
                    connected = True
                    await self._disconnect_event.wait()
                except asyncio.CancelledError:
                    # stop() deliberately cancels this task only while it is
                    # awaiting connect(), so a stalled WinRT discovery cannot
                    # keep the bridge process alive. This controlled
                    # cancellation still falls through to cleanup below; a
                    # caller cancelling run_forever() directly retains the
                    # ordinary cancellation contract.
                    if self._stopping:
                        pass
                    else:
                        raise
                except Exception as exc:  # noqa: BLE001 - deliberately broad: any
                    # connect()/wait() failure must still fall through to cleanup
                    # and a retry, never propagate out of the supervisor.
                    self.last_error = exc
                    if self._logger is not None:
                        self._logger.info("connection attempt failed: %s", exc)
                finally:
                    # Cleanup establishes ownership of the next attempt. If
                    # it fails, stop here: starting another generation while
                    # the previous transport is still alive causes duplicate
                    # listeners and intermittent input loss.
                    await self._cleanup()
                    self.cleanup_count += 1

                if self._stopping:
                    break
                if connected:
                    self._next_retry_delay = self._retry_delay
                delay = self._next_retry_delay
                if not connected:
                    self._next_retry_delay = min(
                        self._max_retry_delay,
                        max(self._retry_delay, self._next_retry_delay * 2),
                    )
                if self._logger is not None:
                    self._logger.info("reconnect scheduled: attempt=%s delay=%.1fs", self.attempt_count, delay)
                if self._on_retry is not None:
                    self._on_retry(self.attempt_count, delay)
                await self._wait_retry(delay)
        finally:
            self._is_connecting = False
            if self._run_task is asyncio.current_task():
                self._run_task = None

    async def stop(self) -> None:
        """Ends the loop after its current cleanup finishes. Safe to call
        even if run_forever() was never started or already finished.
        """

        self._stopping = True
        self._retry_wake.set()
        self._disconnect_event.set()
        run_task = self._run_task
        if (
            self._is_connecting
            and run_task is not None
            and run_task is not asyncio.current_task()
            and not run_task.done()
        ):
            run_task.cancel()
