"""Async-loop-owned ATVV commands; no Windows dependency or input injection."""

from __future__ import annotations

import asyncio
import logging

from . import atvv_protocol as proto
from .atvv_session import AudioStarted, AudioStopped, CapsReceived


class ATVVLifecycle:
    """Serialize open/renew/reset and cancel all writes before GATT disposal.

    One instance belongs to one BLE connection. Session revisions additionally
    prevent work from a previous recording from writing into the next one.
    """

    def __init__(self, write, reset_audio, on_error, *, interval=2.5, open_timeout=5.0, audio_timeout=2.0):
        self._write = write
        self._reset_audio = reset_audio
        self._on_error = on_error
        self._interval = interval
        self._open_timeout = open_timeout
        self._audio_timeout = audio_timeout
        self._last_audio = 0.0
        self._watchdog_task = None
        self._logger = logging.getLogger(__name__)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._renew_task = None
        self._open_timer = None
        self._reset_task = None
        self._closed = False
        self._revision = 0
        self._version = None
        self._stream = None
        self._active = False
        self._opening = False

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not self._closed:
            self._logger.warning("ATVV lifecycle failed: %s", error)
            self._closed = True
            self._cancel_activity()
            self._on_error(error)

    def _cancel_activity(self):
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()

    def handle(self, event):
        if self._closed:
            return
        if isinstance(event, CapsReceived):
            self._version = event.capabilities.version
        elif isinstance(event, AudioStarted):
            if self._active:
                # Windows may replay AUDIO_START while CCCD recovery is still
                # settling.  Starting another stream here races _finish(),
                # produces out-of-order revisions, and can repeatedly reset
                # the audio subscription.  Keep the first active stream as
                # the owner until its AUDIO_STOPPED arrives.
                self._logger.warning(
                    "ATVV duplicate AUDIO_START ignored: active_stream=%s incoming=%s",
                    self._stream,
                    event.session_id,
                )
                return
            self._revision += 1
            self._active, self._opening = True, False
            self._stream = event.session_id
            for task in (self._renew_task, self._open_timer, self._watchdog_task):
                if task is not None:
                    task.cancel()
            self._logger.info("ATVV stream started: revision=%s stream=%s", self._revision, self._stream)
            self._last_audio = asyncio.get_running_loop().time()
            self._watchdog_task = self._spawn(self._watch_audio(self._revision))
            if self._version is not None and self._version >= 0x0100 and self._stream is not None:
                self._renew_task = self._spawn(self._renew(self._revision, self._stream))
        elif isinstance(event, AudioStopped):
            if not self._active and not self._opening:
                return  # MIC_CLOSE acknowledgement must not start another reset
            self._active, self._opening = False, False
            self._revision += 1
            for task in (self._renew_task, self._open_timer, self._watchdog_task):
                if task is not None:
                    task.cancel()
            if self._reset_task is not None and not self._reset_task.done():
                self._reset_task.cancel()
            self._reset_task = self._spawn(self._finish(self._revision, self._stream))

    def request_open(self):
        if self._closed or self._active or self._opening or self._version is None:
            return
        self._opening = True
        return self._spawn(self._open(self._revision))

    def expect_start(self):
        """Bound a physical pre-arm even if START_SEARCH never arrives."""
        if not self._closed and not self._active:
            if self._open_timer is None or self._open_timer.done():
                self._open_timer = self._spawn(self._wait_for_start(self._revision))

    async def _open(self, revision):
        if self._reset_task is not None:
            await asyncio.shield(self._reset_task)
        async with self._lock:
            if self._closed or revision != self._revision or self._active:
                return
            await self._write(proto.mic_open_command(self._version))
            self._logger.info("ATVV MIC_OPEN sent: revision=%s", revision)
        if not self._closed and revision == self._revision and self._opening:
            if self._open_timer is not None:
                self._open_timer.cancel()
            self._open_timer = self._spawn(self._wait_for_start(revision))

    async def _wait_for_start(self, revision):
        await asyncio.sleep(self._open_timeout)
        if revision == self._revision and not self._active and not self._closed:
            raise TimeoutError("ATVV voice request received no AUDIO_START")

    async def _renew(self, revision, stream):
        while True:
            await asyncio.sleep(self._interval)
            async with self._lock:
                if self._closed or not self._active or revision != self._revision:
                    return
                await self._write(proto.mic_extend_command(self._version, stream))
                self._logger.info("ATVV MIC_EXTEND sent: revision=%s stream=%s", revision, stream)

    def audio_received(self):
        if self._active and not self._closed:
            self._last_audio = asyncio.get_running_loop().time()

    async def _watch_audio(self, revision):
        while not self._closed and revision == self._revision and self._active:
            await asyncio.sleep(min(0.5, self._audio_timeout))
            if asyncio.get_running_loop().time() - self._last_audio >= self._audio_timeout:
                raise TimeoutError("ATVV stream stopped delivering audio; voice session reset required")

    async def _finish(self, revision, stream):
        async with self._lock:
            if self._closed or revision != self._revision:
                return
            if self._version is not None:
                await self._write(proto.mic_close_command(self._version, stream or 0))
            await self._reset_audio()
            self._logger.info("ATVV audio subscription restored: revision=%s", revision)

    async def close(self):
        self.suspend()
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def suspend(self):
        self._closed = True
        self._revision += 1
        self._cancel_activity()
