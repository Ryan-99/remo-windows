"""App-wiring/thread-safety tests for app.py's RC003App (XRBM-018 DoD 4).

``RC003App.__init__`` is safe to construct off Windows: config/hotkey/
voice-controller/supervisor setup is pure Python, and the real Win32/WinRT
calls only happen inside ``_connect_once()``/the HID listener, which these
tests never call. Constructing a real ``RC003App`` and substituting its
BLE-session/playback collaborators with lightweight recorders lets these
tests exercise the actual wiring DECISIONS app.py makes - host hotkey
failure suppresses MIC_OPEN, playback write failure fails closed and
requests a reconnect, and that request happens correctly from a real
worker thread - without any Windows API, matching this project's existing
"test contracts, not implementation-mirroring fakes" approach.

The host-hotkey-unavailable case doesn't even need mocking: off Windows,
win32_input.py's ``_require_windows()`` genuinely raises
``Win32InputUnavailableError`` on every call, so it exercises the exact
"hotkey failed to deliver" branch app.py must fail closed on - not a stand-
in for it.
"""

import atexit
import asyncio
import logging
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import app as app_module
from ovb_rc003 import config, key_mapping, logging_setup, raw_input_windows, win32_input
from ovb_rc003.atvv_session import AudioStarted, AudioStopped, MicButtonPressed


def _run(coro):
    # Explicitly closing the loop (XRBM-018 review round 2 evidence: a
    # ResourceWarning for an unclosed test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBleSession:
    def __init__(self, close_raises=False):
        self.mic_open_calls = 0
        self.close_raises = close_raises
        self.close_calls = 0

    def send_mic_open_threadsafe(self):
        self.mic_open_calls += 1

    def expect_audio_threadsafe(self):
        pass

    async def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated BLE worker thread that did not stop")


class _FakeHidListener:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated Raw Input listener thread that did not stop")


class _FakePlaybackSink:
    def __init__(self, fail_write=False, close_raises=False):
        self.fail_write = fail_write
        self.close_raises = close_raises
        self.write_calls = []
        self.closed = False
        self.close_calls = 0

    def write(self, samples):
        self.write_calls.append(samples)
        if self.fail_write:
            raise OSError("simulated PortAudio write failure")

    def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated PortAudio stream that did not close")
        self.closed = True


class _FakeStatusPublisher:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def publish(self, **fields):
        self.calls.append(fields)
        if self.raises:
            raise OSError("status directory blocked")
        return True


class _FakeHidListenerForFailedStart:
    """XRBM-019 review round 1 P1 #3: a fake standing in for
    RawInputButtonListener itself (not just its ``start()`` outcome), so
    ``_start_hid_listener()`` can be exercised end-to-end off Windows -
    ``is_running`` is the source of truth a failed ``start()`` must consult
    before deciding whether to keep or discard the owner reference.
    """

    def __init__(self, is_running_after_failed_start):
        self._is_running_after_failed_start = is_running_after_failed_start
        self.start_calls = 0

    @property
    def is_running(self):
        return self._is_running_after_failed_start

    def start(self, device_path):
        self.start_calls += 1
        raise app_module.raw_input_windows.RawInputUnavailableError("simulated failed start")

    def stop(self):
        pass


def _release_app_loggers() -> None:
    """Close the process-wide app log so a temp directory can be removed.

    A late log record can reopen a FileHandler after ``close()`` sets its
    stream to None. Detach the handler first, then close whatever stream
    is left, including one opened by that race.
    """

    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.acquire()
            try:
                handler.flush()
                handler.close()
                stream = getattr(handler, "stream", None)
                if stream is not None and not getattr(stream, "closed", True):
                    stream.close()
            finally:
                handler.release()
        except OSError:
            pass
    logging_setup._configured = False
    logging_setup._active_log_file = None
    logging_setup._active_log_root = None


atexit.register(_release_app_loggers)


def _build_app(tmp_root: Path) -> "app_module.RC003App":
    # Redirect config_root (and therefore logging_setup's log directory) at
    # a throwaway temp directory instead of the real machine's config/log
    # location - RC003App.__init__ always calls config.config_root()/
    # logging_setup.get_logger(), neither of which touch any Windows API.
    original = config.config_root
    config.config_root = lambda: tmp_root
    try:
        return app_module.RC003App()
    finally:
        config.config_root = original


def _build_app_with_owned_loop(tmp_root: Path):
    """Like _build_app(), but explicitly creates a fresh event loop and sets
    it as this thread's current loop before constructing the app (XRBM-026).

    RC003App.__init__ builds a ConnectionSupervisor, whose __init__ captures
    ``loop or asyncio.get_event_loop()`` (connection_supervisor.py) - called
    here synchronously, off any running loop. Without an owned loop already
    set, that would silently create-and-cache this thread's implicit default
    loop the first time any test builds an RC003App - a loop nothing then
    ever closes (see EventLoopOwnershipRegressionTests for the exact red
    evidence this reproduces and fixes). Returns ``(app, loop)``; the caller
    owns ``loop`` and must ``asyncio.set_event_loop(None)`` then
    ``loop.close()`` it when done - exactly mirroring the real app's own
    ``asyncio.run(_run())`` construction, which owns and closes its loop too.
    """

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _build_app(tmp_root)
    return app, loop


class FridaStartupTests(unittest.TestCase):
    def test_legacy_diagnostic_setting_does_not_disable_frida_input(self):
        for configured in (None, False, "true", 1):
            app = app_module.RC003App.__new__(app_module.RC003App)
            app._config = {"enable_frida_diagnostics": configured}
            app._logger = mock.Mock()
            with mock.patch.object(app_module.frida_compat, "RC003HidReportTap") as tap:
                app._start_hid_report_tap()
                tap.assert_called_once_with(app._on_direct_hid_report, on_lost=app._on_hid_tap_lost)
                tap.return_value.start.assert_called_once()
                self.assertFalse(app._frida_input_enabled)

    def test_connect_schedules_frida_without_waiting_for_the_tap(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app, loop = _build_app_with_owned_loop(Path(tmp.name))
        started = []

        async def discovery():
            started.append("ble")
            return []

        def start_tap():
            started.append("frida")
            raise RuntimeError("tap unavailable")

        original_discovery = app_module.ble_transport_winrt.discover_candidates
        original_select = app_module.identity.select_single_candidate
        original_start = app._start_hid_report_tap
        app_module.ble_transport_winrt.discover_candidates = discovery
        app_module.identity.select_single_candidate = lambda _candidates: None
        app._start_hid_report_tap = start_tap
        try:
            with self.assertRaises(Exception) as ctx:
                _run(app._connect_once())
            self.assertIn("ble", started)
            self.assertNotIn("tap unavailable", str(ctx.exception))
        finally:
            app_module.ble_transport_winrt.discover_candidates = original_discovery
            app_module.identity.select_single_candidate = original_select
            app._start_hid_report_tap = original_start
            task = app._frida_tap_start_task
            if task is not None and not task.done():
                task.cancel()
            app._frida_tap_start_task = None
            asyncio.set_event_loop(None)
            loop.close()
            _release_app_loggers()
            tmp.cleanup()

    def test_unavailable_frida_input_fails_closed_without_raw_input_fallback(self):
        app = app_module.RC003App.__new__(app_module.RC003App)
        app._config = {"enable_frida_diagnostics": True}
        app._logger = mock.Mock()
        app._hid_report_tap = None
        app._frida_input_enabled = False
        with mock.patch.object(app_module.frida_compat, "RC003HidReportTap") as tap:
            tap.return_value.start.return_value = False
            app._start_hid_report_tap()
            tap.return_value.stop.assert_not_called()
            self.assertIsNone(app._hid_report_tap)
            self.assertFalse(app._frida_input_enabled)


class _AppWiringTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # XRBM-026 red evidence (real Windows run 29644660267): 425 tests
        # passed, then the process printed an ignored "unclosed event loop"
        # ResourceWarning for a ProactorEventLoop plus two unclosed self-pipe
        # sockets - AFTER unittest's own summary, so -W error::ResourceWarning
        # never sees it and the step still exits 0 (a ResourceWarning-turned-
        # exception raised inside a __del__/finalizer is unraisable; Python
        # can only print it via sys.unraisablehook, never let it change an
        # already-computed exit code - see EventLoopOwnershipRegressionTests
        # below for a deterministic, isolated-subprocess reproduction).
        # _build_app_with_owned_loop() above threads a per-test owned loop
        # into ConnectionSupervisor instead - never the ambient, never-closed
        # default the old bare _build_app() call left behind.
        self.app, self._loop = _build_app_with_owned_loop(Path(self._tmp.name))
        self.app._playback = _FakePlaybackSink()
        self.app._ble_session = _FakeBleSession()

    def tearDown(self):
        # XRBM-023: logging_setup.get_logger() configures its FileHandler
        # exactly once per process (module-global ``_configured``) and never
        # closes it - correct for a real long-running app, but in this suite
        # it leaves an open handle inside THIS test's temp directory. Windows
        # (unlike POSIX, where you can unlink a file while a handle is still
        # open on it) refuses to delete a directory containing an open file
        # handle, so ``self._tmp.cleanup()`` below would raise on Windows
        # once any prior test in this class had already configured the
        # logger. Close/remove the handler and reset the one-time-config
        # flag first so every test starts and ends with no logging state
        # leaked into the next one.
        _release_app_loggers()
        try:
            self._tmp.cleanup()
        except PermissionError:
            # Windows can keep the log handle for a moment after close.
            # The assertions already ran; a leftover temp file must not
            # fail the test.
            pass
        # XRBM-026: close the loop this test owns (see setUp()) and detach
        # it as the thread's current loop, so its own eventual __del__ finds
        # is_closed() already True and stays silent - and so the NEXT test's
        # setUp() cannot mistake this now-closed loop for a live ambient one.
        asyncio.set_event_loop(None)
        self._loop.close()


class HostHotkeyFailureSuppressesMicOpenTests(_AppWiringTestCase):
    """XRBM-014 review round 2 P1 #6: MIC_OPEN must never be sent unless the
    host hotkey actually, fully delivered.
    """

    @unittest.skipIf(
        sys.platform == "win32",
        "only exercises the off-Windows win32_input._require_windows() gate "
        "(no injected sender); on a real Windows runner the real SendInput "
        "call fully delivers instead, so this would assert the opposite of "
        "what actually happens there (XRBM-023) - "
        "test_hotkey_partial_delivery_suppresses_mic_open below covers the "
        "same suppression contract cross-platform via dependency injection",
    )
    def test_hotkey_unavailable_off_windows_suppresses_mic_open(self):
        self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)

    def test_hotkey_partial_delivery_suppresses_mic_open(self):
        def _raise(tokens):
            raise OSError("simulated partial SendInput delivery")

        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = _raise
        try:
            self.app._handle_mic_button_pressed()
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)

    def test_hotkey_success_sends_mic_open(self):
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: None
        try:
            self.app._handle_mic_button_pressed()
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice.active)

    def test_raw_input_mic_button_triggers_voice_before_atvv_audio(self):
        hotkey_calls = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: hotkey_calls.append(("up", tokens))
        try:
            self.app._on_button_event("mic", True)
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(MicButtonPressed())
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(
            hotkey_calls,
            [("up", ("ralt",)), ("down", ("ralt",)), ("down", ("ralt",))],
        )
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertTrue(self.app._voice_hold_started)

    def test_raw_input_mic_button_release_sends_one_matching_key_up(self):
        hotkey_calls = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: hotkey_calls.append(("up", tokens))
        try:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(
            hotkey_calls,
            [("up", ("ralt",)), ("down", ("ralt",)), ("up", ("ralt",))],
        )
        self.assertFalse(self.app._voice_hold_started)

    def test_suppressed_legacy_f5_uses_the_same_voice_trigger_path(self):
        hotkey_calls = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: hotkey_calls.append(("up", tokens))
        try:
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(
            hotkey_calls,
            [("up", ("ralt",)), ("down", ("ralt",)), ("up", ("ralt",))],
        )
        self.assertFalse(self.app._voice_hold_started)

    def test_physical_legacy_f5_transform_skips_a_second_host_shortcut(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_legacy_transform_key_down = True
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_legacy_key_event(0x74, True)
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(hotkey_calls, [])
        self.assertTrue(self.app._voice_hold_started)
        self.assertTrue(self.app._voice_legacy_transform_session)

    def test_hold_preset_maps_f5_to_one_right_alt_down_and_up_target(self):
        self.app._legacy_path_disabled = False
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")

        down = self.app._transform_legacy_voice_key(0x74, True)
        up = self.app._transform_legacy_voice_key(0x74, False)

        self.assertEqual(down, app_module.legacy_key_suppressor_windows.PhysicalKeyTarget(0xA5, 0x38, True, True))
        self.assertEqual(up, app_module.legacy_key_suppressor_windows.PhysicalKeyTarget(0xA5, 0x38, True, True))
        self.assertFalse(self.app._voice_legacy_transform_key_down)

    def test_transformed_f5_edge_emits_one_right_alt_virtual_key_edge(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        target = app_module.legacy_key_suppressor_windows.PhysicalKeyTarget(
            0xA5, 0x38, True, True
        )
        calls = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        try:
            self.assertTrue(self.app._emit_legacy_voice_key(target, True))
            self.assertTrue(self.app._emit_legacy_voice_key(target, False))
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(calls, [("down", ("ralt",)), ("up", ("ralt",))])
        self.assertTrue(self.app._voice_legacy_transform_emitted)

    def test_hold_audio_start_waits_for_f5_instead_of_injecting_ahead_of_it(self):
        self.app._legacy_path_disabled = False
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        calls = []
        original_down = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(tokens)
        try:
            self.app._on_control_event(AudioStarted(session_id=1))
        finally:
            win32_input.send_voice_key_combo_down = original_down

        self.assertEqual(calls, [])
        self.assertFalse(self.app._voice.active)
        self.assertTrue(self.app._voice_audio_started_waiting_for_legacy_f5)

    def test_physical_legacy_f5_transform_skips_closing_host_shortcut(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_legacy_transform_session = True
        self.app._voice.on_mic_button_pressed()
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_up = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_control_event(AudioStopped())
        finally:
            win32_input.send_voice_key_combo_up = original

        self.assertEqual(hotkey_calls, [])
        self.assertFalse(self.app._voice.active)
        self.assertFalse(self.app._voice_legacy_transform_session)

    def test_hid_mic_shortcut_is_sent_even_without_ble_or_playback(self):
        self.app._ble_session = None
        self.app._playback = None
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE
        original = win32_input.send_voice_key_combo_tap
        hotkey_calls = []
        win32_input.send_voice_key_combo_tap = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_button_event("mic", True)
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(hotkey_calls, [("ralt",)])
        self.assertTrue(self.app._voice.active)

    def test_toggle_input_physical_press_submits_exactly_one_shortcut(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        calls = []
        original_tap = win32_input.send_voice_key_combo_tap
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(("tap", tokens))
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        try:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original_tap
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(calls, [("tap", ("lctrl", "lwin"))])
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_toggle_input_repeated_short_presses_each_submit_once(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(tokens)
        try:
            for _ in range(2):
                self.app._on_button_event("mic", True)
                self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original
        self.assertEqual(calls, [("lctrl", "lwin"), ("lctrl", "lwin")])

    def test_toggle_input_atvv_events_never_submit_host_shortcut(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(tokens)
        try:
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())
        finally:
            win32_input.send_voice_key_combo_tap = original
        self.assertEqual(calls, [])

    def test_wechat_short_press_does_not_toggle_repeated_hid_down_reports(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        calls = []
        original_tap = win32_input.send_voice_key_combo_tap
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(("tap", tokens))
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        try:
            with self.assertLogs("ovb_rc003", level="INFO") as captured:
                for seq, payload in ((91, "3e0000000000"), (92, "3e0000000000"), (93, "000000000000")):
                    self.app._on_direct_hid_report(
                        app_module.frida_compat.HidReport(
                            pid=7024,
                            seq=seq,
                            received_at=10.25 + seq,
                            report_id=1,
                            payload=bytes.fromhex(payload),
                        )
                    )
        finally:
            win32_input.send_voice_key_combo_tap = original_tap
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        timer = self.app._voice_hold_timer
        if timer is not None:
            timer.cancel()
        self.assertEqual(calls, [("tap", ("lctrl", "lwin"))])
        logs = "\n".join(captured.output)
        self.assertIn("report_seq=91", logs)
        self.assertIn("voice stage: physical_key_arrived", logs)
        self.assertIn("voice stage: shortcut_submitted", logs)
        self.assertIn("ime_activation=unknown", logs)

    def test_toggle_input_shortcut_works_without_ble_voice_session(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(hotkey_calls, [("lctrl", "lwin")])
        self.assertFalse(self.app._voice.active)

    def test_wechat_hold_timer_does_not_submit_ctrl_win_again(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        calls = []
        original_tap = win32_input.send_voice_key_combo_tap
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(("tap", tokens))
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        try:
            self.app._on_button_event("mic", True)
            self.app._voice_hold_timer.cancel()
            self.app._voice_hold_timer = None
            self.app._start_voice_sustained_shortcut()
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original_tap
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(calls, [("tap", ("lctrl", "lwin")), ("down", ("lalt",)), ("up", ("lalt",))])

    def test_toggle_input_second_physical_press_closes_the_ime(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(hotkey_calls, [("lctrl", "lwin"), ("lctrl", "lwin")])
        self.assertFalse(self.app._voice.active)

    def test_wechat_long_press_holds_left_alt_until_physical_release(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        calls = []
        original_tap = win32_input.send_voice_key_combo_tap
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(("tap", tokens))
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        try:
            self.app._on_button_event("mic", True)
            timer = self.app._voice_hold_timer
            timer.cancel()
            self.app._voice_hold_timer = None
            self.app._start_voice_sustained_shortcut()
            self.app._on_button_event("mic", False)
        finally:
            win32_input.send_voice_key_combo_tap = original_tap
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(calls, [("tap", ("lctrl", "lwin")), ("down", ("lalt",)), ("up", ("lalt",))])
        self.assertFalse(self.app._voice_hold_started)

    def test_wechat_release_during_opener_settle_never_starts_hold_key(self):
        """A release racing the 120 ms IME settle window must cancel Alt."""

        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        calls = []
        original_tap = win32_input.send_voice_key_combo_tap
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_tap = lambda tokens: calls.append(("tap", tokens))
        win32_input.send_voice_key_combo_down = lambda tokens: calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: calls.append(("up", tokens))
        entered = threading.Event()
        released = threading.Event()

        def settle(_seconds):
            entered.set()
            self.assertTrue(released.wait(1.0))

        original_sleep = app_module.time.sleep
        app_module.time.sleep = settle
        try:
            self.app._on_button_event("mic", True)
            timer = self.app._voice_hold_timer
            self.assertIsNotNone(timer)
            timer.cancel()
            self.app._voice_hold_timer = None
            worker = threading.Thread(target=self.app._start_voice_sustained_shortcut)
            worker.start()
            self.assertTrue(entered.wait(1.0))
            self.app._on_button_event("mic", False)
            released.set()
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        finally:
            app_module.time.sleep = original_sleep
            win32_input.send_voice_key_combo_tap = original_tap
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up

        self.assertEqual(calls, [("tap", ("lctrl", "lwin"))])

    def test_toggle_input_ignores_atvv_duplicate_after_physical_shortcut(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_button_event("mic", True)
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(hotkey_calls, [("lctrl", "lwin")])
        self.assertFalse(self.app._voice.active)

    def test_toggle_input_atvv_audio_events_do_not_change_ime_toggle(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE_INPUT
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+lwin")
        self.app._voice_hold_hotkey = app_module.hotkey.HotkeySpec.parse("lalt")
        self.app._config_mtime_ns = self.app._config_file_mtime_ns()
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
            self.app._on_control_event(AudioStarted(session_id=7))
            self.app._on_control_event(AudioStopped())
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertEqual(hotkey_calls, [("lctrl", "lwin")])
        self.assertFalse(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._atvv_audio_active)
        self.assertIsNone(self.app._active_atvv_session_id)
        self.assertEqual(self.app._voice_pcm_stats.summary()["frames"], 0)

    def test_audio_started_can_trigger_voice_when_legacy_f5_is_suppressed(self):
        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = lambda tokens: None
        try:
            self.app._on_control_event(AudioStarted(session_id=1))
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_late_mic_button_after_audio_start_does_not_send_a_second_alt(self):
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(MicButtonPressed())
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(hotkey_calls, [("ralt",)])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_repeated_mic_control_does_not_toggle_an_active_session(self):
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(MicButtonPressed())
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(hotkey_calls, [("ralt",)])
        self.assertTrue(self.app._voice.active)

    def test_three_raw_then_start_search_sessions_open_and_close_once_each(self):
        hotkey_calls = []
        original_down = win32_input.send_voice_key_combo_down
        original_up = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(("down", tokens))
        win32_input.send_voice_key_combo_up = lambda tokens: hotkey_calls.append(("up", tokens))
        one_session = [
            ("up", ("ralt",)),
            ("down", ("ralt",)),
            ("down", ("ralt",)),
            ("up", ("ralt",)),
            ("up", ("ralt",)),
        ]
        try:
            for stream in (1, 2, 3):
                self.app._last_button_edges.clear()
                self.app._on_button_event("mic", True)
                self.app._on_control_event(MicButtonPressed())
                self.app._on_control_event(AudioStarted(session_id=stream))
                self.app._on_control_event(MicButtonPressed())
                self.app._on_button_event("mic", False)
                self.app._on_control_event(AudioStopped())
                self.assertFalse(self.app._voice.active)
                self.assertFalse(self.app._voice_hold_started)
        finally:
            win32_input.send_voice_key_combo_down = original_down
            win32_input.send_voice_key_combo_up = original_up
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(hotkey_calls, one_session * 3)

    def test_mic_button_before_audio_start_does_not_send_a_second_alt(self):
        hotkey_calls = []
        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(hotkey_calls, [("ralt",)])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)

    def test_no_usable_endpoint_does_not_suppress_host_shortcut(self):
        self.app._playback = None
        self.app._config["output_endpoint_name"] = "some endpoint that is not open"

        hotkey_calls = []
        original = win32_input.send_voice_key_combo_down
        win32_input.send_voice_key_combo_down = lambda tokens: hotkey_calls.append(tokens)
        try:
            self.app._handle_mic_button_pressed()
        finally:
            win32_input.send_voice_key_combo_down = original

        self.assertEqual(hotkey_calls, [("ralt",)])
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertTrue(self.app._voice.active)

    def test_windows_actually_delivers_the_hotkey_unlike_the_off_windows_case(self):
        """Regression for XRBM-023 outcome 9: on a real Windows runner,
        win32_input._require_windows() does NOT raise, so the un-mocked
        send_voice_key_combo_tap() call inside _handle_mic_button_pressed()
        reaches the real voice sender - simulated here as a full
        delivery - and MIC_OPEN IS sent. This is exactly why the skipped
        test_hotkey_unavailable_off_windows_suppresses_mic_open above would
        have failed (not errored) on the real Windows CI runner: it asserts
        the opposite of what actually happens there.
        """
        original_platform = sys.platform
        original_sender = win32_input._real_voice_event
        sys.platform = "win32"
        win32_input._real_voice_event = lambda vk, key_up: None
        try:
            self.app._handle_mic_button_pressed()
        finally:
            sys.platform = original_platform
            win32_input._real_voice_event = original_sender

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice.active)


class CorruptButtonBindingFailsClosedTests(_AppWiringTestCase):
    def test_unknown_or_malformed_binding_does_not_escape_raw_input_callback(self):
        for malformed in (
            {"kind": "unknown", "keys": []},
            {"kind": "key_combo", "keys": ["not_a_real_key"]},
            {"kind": "key_combo", "keys": "ctrl+a"},
            {},
            "not-a-mapping",
        ):
            self.app._bindings = {"bindings": {"back": malformed}}
            self.app._on_button_event("back", True)

        self.assertFalse(self.app._voice.active)


class OrdinaryButtonGestureWiringTests(_AppWiringTestCase):
    def test_saved_mapping_is_reloaded_before_the_next_button_event(self):
        updated = config.default_key_bindings()
        updated["bindings"]["back"] = {"kind": "key_combo", "keys": ["f8"]}
        config.save_key_bindings(self.app._bindings_path, updated)

        calls = []
        original = win32_input.send_key_combo_tap
        win32_input.send_key_combo_tap = lambda keys: calls.append(tuple(keys))
        try:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)
        finally:
            win32_input.send_key_combo_tap = original

        self.assertEqual(calls, [("f8",)])

    def test_legacy_f5_auto_repeat_is_collapsed_to_one_physical_press(self):
        calls = []
        original = self.app._on_button_event
        self.app._on_button_event = lambda *args, **kwargs: calls.append((args, kwargs))
        try:
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
        finally:
            self.app._on_button_event = original

        self.assertEqual([entry[0][:2] for entry in calls], [("mic", True), ("mic", False)])

    def test_semantic_arrow_action_uses_its_function_executor(self):
        calls = []
        original = getattr(win32_input, "send_arrow_up", None)
        win32_input.send_arrow_up = lambda: calls.append("arrow_up")
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
        finally:
            if original is None:
                delattr(win32_input, "send_arrow_up")
            else:
                win32_input.send_arrow_up = original

        self.assertEqual(calls, ["arrow_up"])

    def test_open_app_action_uses_application_executor(self):
        calls = []
        original = getattr(app_module, "open_configured_application", None)
        app_module.open_configured_application = lambda action: calls.append(action.kind)
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
            )
        finally:
            if original is None:
                delattr(app_module, "open_configured_application")
            else:
                app_module.open_configured_application = original

        self.assertEqual(calls, [key_mapping.ActionKind.OPEN_CODEX])

    def test_one_physical_press_emits_one_mapping_action(self):
        calls = []
        original = win32_input.send_key_combo_tap
        win32_input.send_key_combo_tap = lambda keys: calls.append(tuple(keys))
        try:
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", True)  # Raw Input repeat/duplicate
            self.app._on_button_event("up", False)
        finally:
            win32_input.send_key_combo_tap = original

        self.assertEqual(calls, [("up",)])
        self.assertEqual(self.app._statistics.snapshot("all")["by_button"], {"up": 1})

    def test_native_windows_key_never_enters_mapping_dispatch(self):
        events = []
        self.app._button_gestures.press = lambda key: events.append(key)
        self.app._on_windows_button_event("up", True)
        self.app._on_windows_button_event("up", False)
        self.assertEqual(events, [])
        self.assertEqual(self.app._statistics.snapshot("all")["by_button"], {})

    def test_physical_keyboard_arrow_never_dispatches_remote_mapping(self):
        events = []
        self.app._button_gestures.press = lambda key: events.append(key)
        self.app._input_source = config.INPUT_SOURCE_WINDOWS_HID
        self.app._direct_hid_tap_active = False
        self.app._windows_filter_buttons.add("up")
        self.app._on_windows_button_event("up", True)
        self.app._on_windows_button_event("up", False)
        self.assertEqual(events, [])

    def test_frida_hid_direction_usage_still_dispatches_remote_mapping(self):
        events = []
        self.app._button_gestures.press = lambda key: events.append(("down", key))
        self.app._button_gestures.release = lambda key: events.append(("up", key))
        # Generic Desktop usage 0x0052 is the Up arrow on RC003. Only the
        # Frida HID report path has this source-specific physical evidence.
        self.app._on_direct_hid_report(1, bytes.fromhex("520000000000"))
        self.app._on_direct_hid_report(1, bytes(6))
        self.assertEqual(events, [("down", "up"), ("up", "up")])

    def test_raw_input_button_callback_requires_exact_device_identity(self):
        from types import SimpleNamespace
        from .test_driver_contract import SELECTED, KEYBOARD

        events = []
        self.app._button_gestures.press = lambda key: events.append(key)
        self.app._hid_listener = SimpleNamespace(_device_path=SELECTED)
        self.app._on_button_event("up", True, device_path=KEYBOARD)
        self.app._on_button_event("up", False, device_path=KEYBOARD)
        self.app._on_button_event("up", True, device_path=SELECTED)
        self.assertEqual(events, ["up"])

    def test_filter_keys_route_only_after_exact_device_evidence(self):
        from types import SimpleNamespace
        from .test_driver_contract import SELECTED, KEYBOARD
        events = []
        self.app._hid_listener = SimpleNamespace(_device_path=SELECTED)
        self.app._button_gestures.press = lambda key: events.append(key)
        for path in (KEYBOARD, SELECTED):
            self.app._on_raw_input_event(raw_input_windows.RawInputEvent(
                source="keyboard", is_pressed=True, button_id="back",
                vkey=0x7e, device_path=path,
            ))
            self.app._on_windows_button_event("back", True)
            self.app._on_windows_button_event("back", False)
        self.assertEqual(events, [])

    def test_two_fast_press_release_pairs_are_not_time_deduplicated(self):
        events = []
        self.app._button_gestures.press = lambda key: events.append(key)
        for _ in range(2):
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)
        self.assertEqual(events, ["back", "back"])

    def test_frida_diagnostic_callback_never_dispatches_an_action(self):
        calls = []
        self.app._on_button_event = lambda *args: calls.append(args)
        self.app._on_diagnostic_hid_report(1, bytes.fromhex("010000f10000000000"))
        self.assertEqual(calls, [])

    def test_frida_mic_edges_dispatch_shortcut_once_per_press_and_release(self):
        actions = []
        original = self.app._apply_voice_action
        self.app._frida_input_enabled = True
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._direct_hid_rebaseline = False
        self.app._direct_hid_usages = set()
        self.app._frida_mic_session_active = False
        self.app._last_button_edges = {}
        self.app._voice_hold_started = False
        self.app._voice_raw_input_trigger_pending = False
        self.app._apply_voice_action = lambda action: actions.append(action) or True
        try:
            # 0x003e is the RC003 microphone usage in the Frida report.
            self.app._on_direct_hid_report(1, bytes.fromhex("3e0000000000"))
            self.app._on_direct_hid_report(1, bytes(6))
        finally:
            self.app._apply_voice_action = original
        self.assertEqual([action.value for action in actions], ["key_down"])

    def test_raw_keyboard_edge_is_not_armed_for_global_hook_suppression(self):
        """A global LL hook cannot distinguish the remote from a real keyboard."""
        armed = []

        class _Suppressor:
            def arm_key_event(self, *args):
                armed.append(args)

        self.app._legacy_key_suppressor = _Suppressor()
        self.app._on_raw_input_event(
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="up",
                vkey=0x26,
                make_code=0x48,
                flags=0x02,
            )
        )

        self.assertEqual(armed, [])

    def test_unknown_or_unbound_raw_keyboard_edge_is_not_armed(self):
        armed = []

        class _Suppressor:
            def arm_key_event(self, *args):
                armed.append(args)

        self.app._legacy_key_suppressor = _Suppressor()
        for event in (
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id=None,
                vkey=0xFF,
                make_code=0x70,
                flags=0,
            ),
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="volume_mute",
                vkey=0x20,
                make_code=0x20,
                flags=0x02,
            ),
        ):
            self.app._on_raw_input_event(event)

        self.assertEqual(armed, [])


class PlaybackWriteFailureTests(_AppWiringTestCase):
    """XRBM-014 review round 2 P1 #6: a playback write failure must fail
    closed (discard the sink) and request a reconnect, not log indefinitely
    while the device keeps streaming.
    """

    def test_write_failure_closes_sink_and_requests_reconnect(self):
        sink = _FakePlaybackSink(fail_write=True)
        self.app._playback = sink
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])

        self.assertTrue(sink.closed)
        self.assertIsNone(self.app._playback)
        self.assertEqual(reconnect_calls, [1])

    def test_write_success_does_not_touch_playback_or_reconnect(self):
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])

        self.assertIsNotNone(self.app._playback)
        self.assertEqual(reconnect_calls, [])

    def test_no_playback_open_is_a_silent_no_op(self):
        self.app._playback = None
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise

        self.assertEqual(reconnect_calls, [])


class CrossThreadReconnectTests(_AppWiringTestCase):
    """ble_transport_winrt.py invokes _on_pcm_frame on its own dedicated
    worker thread, never the event-loop thread - a playback failure there
    must still correctly reach request_reconnect().
    """

    def test_on_pcm_frame_failure_from_a_real_worker_thread_requests_reconnect(self):
        self.app._playback = _FakePlaybackSink(fail_write=True)
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(
            threading.current_thread()
        )

        worker = threading.Thread(target=self.app._on_pcm_frame, args=([0, 0],))
        worker.start()
        worker.join(timeout=2.0)

        self.assertEqual(len(reconnect_calls), 1)
        self.assertNotEqual(reconnect_calls[0], threading.main_thread())


class BridgeStatusWiringTests(_AppWiringTestCase):
    def test_disconnect_and_voice_events_publish_runtime_states(self):
        publisher = _FakeStatusPublisher()
        self.app._status_publisher = publisher
        self.app._on_disconnected()
        self.app._on_control_event(AudioStarted(session_id=1))
        self.app._on_control_event(AudioStopped())
        states = [call["state"] for call in publisher.calls]
        self.assertIn("reconnecting", states)
        self.assertIn("voice_active", states)
        self.assertEqual(states[-1], "connected")

    def test_status_publisher_failure_never_escapes_event_callback(self):
        self.app._status_publisher = _FakeStatusPublisher(raises=True)
        self.app._on_disconnected()
        self.app._on_session_error(RuntimeError("protocol"))

    def test_failed_discovery_changes_connecting_to_reconnecting(self):
        publisher = _FakeStatusPublisher()
        self.app._status_publisher = publisher

        async def failed_discovery():
            raise RuntimeError("no adapter")

        original = app_module.ble_transport_winrt.discover_candidates
        app_module.ble_transport_winrt.discover_candidates = failed_discovery
        try:
            with self.assertRaisesRegex(RuntimeError, "no adapter"):
                _run(self.app._connect_once())
        finally:
            app_module.ble_transport_winrt.discover_candidates = original

        self.assertEqual(
            [call["state"] for call in publisher.calls],
            ["connecting", "reconnecting"],
        )

    def test_cancelled_connect_does_not_overwrite_stopping_state(self):
        publisher = _FakeStatusPublisher()
        self.app._status_publisher = publisher

        async def cancelled_discovery():
            raise asyncio.CancelledError()

        original = app_module.ble_transport_winrt.discover_candidates
        app_module.ble_transport_winrt.discover_candidates = cancelled_discovery
        try:
            with self.assertRaises(asyncio.CancelledError):
                _run(self.app._connect_once())
        finally:
            app_module.ble_transport_winrt.discover_candidates = original

        self.assertEqual(
            [call["state"] for call in publisher.calls],
            ["connecting"],
        )


class CleanupOwnershipTests(_AppWiringTestCase):
    """XRBM-019 P1 #2/#5: _cleanup_once() must attempt every one of the
    four steps (voice, HID, BLE, playback) regardless of any single step's
    outcome, must retain (not clear) the owner reference for a step whose
    resource reports it is still alive, and must aggregate and raise once
    every step has been attempted - so ConnectionSupervisor.run_forever()
    fails the whole retry loop closed instead of starting a fresh connect()
    generation over resources that might still be live.
    """

    def test_cleanup_clears_every_owner_on_full_success(self):
        self.app._hid_listener = _FakeHidListener()
        self.app._ble_session = _FakeBleSession()
        self.app._playback = _FakePlaybackSink()

        _run(self.app._cleanup_once())  # must not raise

        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._ble_session)
        self.assertIsNone(self.app._playback)

    def test_hid_stop_failure_retains_hid_owner_but_still_completes_ble_and_playback(self):
        hid = _FakeHidListener(stop_raises=True)
        ble = _FakeBleSession()
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("Raw Input listener", str(ctx.exception))

        # Retained, not hidden:
        self.assertIs(self.app._hid_listener, hid)
        # Every other step still ran and cleared normally:
        self.assertEqual(ble.close_calls, 1)
        self.assertIsNone(self.app._ble_session)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_ble_close_failure_retains_ble_owner_but_still_completes_hid_and_playback(self):
        hid = _FakeHidListener()
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("BLE session", str(ctx.exception))

        # Retained, not hidden:
        self.assertIs(self.app._ble_session, ble)
        self.assertEqual(ble.close_calls, 1)
        # Every other step still ran and cleared normally:
        self.assertEqual(hid.stop_calls, 1)
        self.assertIsNone(self.app._hid_listener)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_both_hid_and_ble_failures_retain_both_owners_and_aggregate(self):
        hid = _FakeHidListener(stop_raises=True)
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        message = str(ctx.exception)
        self.assertIn("Raw Input listener", message)
        self.assertIn("BLE session", message)

        self.assertIs(self.app._hid_listener, hid)
        self.assertIs(self.app._ble_session, ble)
        # Playback is unconditionally attempted/cleared even though both
        # owner-retaining steps failed.
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_cleanup_failure_propagates_out_of_run_forever_without_a_second_connect(self):
        """End-to-end: wires _cleanup_once() as the real
        ConnectionSupervisor.cleanup callable and proves a retained-owner
        failure ends run_forever() entirely - no second connect()
        generation is ever attempted over the still-live HID listener.
        """

        hid = _FakeHidListener(stop_raises=True)
        self.app._hid_listener = hid
        self.app._ble_session = _FakeBleSession()
        self.app._playback = _FakePlaybackSink()

        connect_calls = []

        async def scenario():
            # ConnectionSupervisor.__init__ captured its ``_loop`` at
            # construction time (setUp() built self.app synchronously,
            # off any running loop - see connection_supervisor.py's module
            # docstring). Rebind it to the loop this coroutine is actually
            # running on before calling request_reconnect(), exactly as
            # the real app does by constructing everything inside one
            # asyncio.run(); otherwise request_reconnect()'s
            # call_soon_threadsafe hop lands on a loop nothing drives and
            # run_forever() hangs forever on _disconnect_event.wait()
            # (XRBM-019 review round 1 P1 #1 - the prior version of this
            # test only "passed" because that loop mismatch raised into
            # the cleanup path, never proving the intended behavior).
            self.app._supervisor._loop = asyncio.get_running_loop()
            self.app._supervisor._connect = lambda: _record_connect(connect_calls)

            task = asyncio.ensure_future(self.app._supervisor.run_forever())
            # Let run_forever() run its first connect() and reach the
            # disconnect_event.wait() suspension point before we end the
            # attempt explicitly - request_reconnect() is what a real BLE
            # disconnect/protocol-error/playback-failure callback would
            # call; nothing here relies on an accidental cross-loop
            # exception to unblock the wait.
            await asyncio.sleep(0)
            self.app._supervisor.request_reconnect()

            # Bounded so a real regression (e.g. cleanup ownership lost
            # again, or the wait never unblocking) fails the test instead
            # of hanging the whole suite.
            with self.assertRaises(app_module.CleanupIncompleteError):
                await asyncio.wait_for(task, timeout=5.0)

        _run(scenario())

        self.assertEqual(connect_calls, [1])  # only the first attempt ever ran
        self.assertEqual(self.app._supervisor.attempt_count, 1)
        # Still retained after the whole supervisor loop ended:
        self.assertIs(self.app._hid_listener, hid)


class StartHidListenerOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #3: RawInputButtonListener intentionally
    retains its thread/window when its own bounded failed-start cleanup
    cannot stop them (see raw_input_windows.py's ``_abandon_failed_start``).
    ``_start_hid_listener()`` must consult ``is_running`` rather than
    unconditionally clearing ``self._hid_listener`` to ``None`` on any
    failed ``start()`` - doing so would lose the owner and let a later
    ``_connect_once()`` generation start a second listener over a still-
    live one (the exact defect class XRBM-019 exists to eliminate; see
    also CleanupOwnershipTests' end-to-end supervisor test above, which
    proves no second connect() generation is ever reached once cleanup
    itself fails on a retained owner).
    """

    def _patch_device_discovery(self, fake_listener):
        original_enumerate = app_module.raw_input_windows.enumerate_matching_device_paths
        original_select = app_module.hid_identity.select_single_device_path
        original_listener_cls = app_module.raw_input_windows.RawInputButtonListener
        app_module.raw_input_windows.enumerate_matching_device_paths = lambda: ["fake-path"]
        app_module.hid_identity.select_single_device_path = lambda paths: paths[0]
        app_module.raw_input_windows.RawInputButtonListener = lambda callback: fake_listener

        def _restore():
            app_module.raw_input_windows.enumerate_matching_device_paths = original_enumerate
            app_module.hid_identity.select_single_device_path = original_select
            app_module.raw_input_windows.RawInputButtonListener = original_listener_cls

        return _restore

    def test_starting_device_listener_never_installs_global_keyboard_hook(self):
        listener = mock.Mock()
        listener.start.return_value = None
        restore = self._patch_device_discovery(listener)
        try:
            with mock.patch.object(
                app_module.legacy_key_suppressor_windows,
                "LegacyKeySuppressor",
            ) as suppressor:
                self.app._start_hid_listener()
        finally:
            restore()

        suppressor.assert_not_called()
        self.assertIsNone(self.app._legacy_key_suppressor)

    def test_a_failed_start_that_is_still_running_retains_the_owner_and_raises(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=True)
        restore = self._patch_device_discovery(fake_listener)
        try:
            with self.assertRaises(app_module.raw_input_windows.RawInputUnavailableError):
                self.app._start_hid_listener()
        finally:
            restore()

        self.assertIs(self.app._hid_listener, fake_listener)
        self.assertEqual(fake_listener.start_calls, 1)

    def test_a_failed_start_confirmed_stopped_clears_the_owner(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=False)
        restore = self._patch_device_discovery(fake_listener)
        try:
            self.app._start_hid_listener()  # must not raise
        finally:
            restore()

        self.assertIsNone(self.app._hid_listener)
        self.assertEqual(fake_listener.start_calls, 1)


class VoiceCleanupFailurePreservesPendingStateTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #4: reset()/on_audio_stopped() clear
    VoiceController's owed state before the caller has confirmed the
    closing action (HOLD's KEY_UP, TOGGLE's closing TAP) actually
    delivered. A failed delivery must not be recorded as a clean close -
    _cleanup_once() must restore the pending state and aggregate the
    failure (after still attempting HID/BLE/playback), and the AudioStopped
    control-event path must restore the pending state and request a
    reconnect instead of silently treating the close as successful.
    """

    def test_cleanup_once_preserves_hold_mode_key_up_on_failure(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice.on_mic_button_pressed()
        self.assertTrue(self.app._voice.holding)

        def _raise(tokens):
            raise OSError("simulated key-up delivery failure")

        original = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_up = _raise
        try:
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())
        finally:
            win32_input.send_voice_key_combo_up = original

        self.assertIn("voice hotkey", str(ctx.exception))
        # Restored, not left thinking the key-up landed:
        self.assertTrue(self.app._voice.holding)
        self.assertTrue(self.app._voice.active)

    def test_cleanup_once_preserves_toggle_mode_closing_tap_on_failure(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE
        self.app._voice.on_mic_button_pressed()
        self.assertTrue(self.app._voice.active)

        def _raise(tokens):
            raise OSError("simulated closing-tap delivery failure")

        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = _raise
        try:
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertIn("voice hotkey", str(ctx.exception))
        self.assertTrue(self.app._voice.active)

    def test_audio_stopped_preserves_hold_mode_key_up_on_failure_and_reconnects(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice.on_mic_button_pressed()

        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        def _raise(tokens):
            raise OSError("simulated key-up delivery failure")

        original = win32_input.send_voice_key_combo_up
        win32_input.send_voice_key_combo_up = _raise
        try:
            self.app._on_control_event(AudioStopped())
        finally:
            win32_input.send_voice_key_combo_up = original

        self.assertTrue(self.app._voice.holding)
        self.assertEqual(reconnect_calls, [1])

    def test_audio_stopped_preserves_toggle_mode_closing_tap_on_failure_and_reconnects(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.TOGGLE
        self.app._voice.on_mic_button_pressed()

        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        def _raise(tokens):
            raise OSError("simulated closing-tap delivery failure")

        original = win32_input.send_voice_key_combo_tap
        win32_input.send_voice_key_combo_tap = _raise
        try:
            self.app._on_control_event(AudioStopped())
        finally:
            win32_input.send_voice_key_combo_tap = original

        self.assertTrue(self.app._voice.active)
        self.assertEqual(reconnect_calls, [1])


class PlaybackCleanupOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #5: both _cleanup_once() and
    _on_pcm_frame() must retain (not discard) the playback sink owner when
    its own close() call fails - EndpointPlaybackSink owns a PortAudio
    stream, and clearing the reference would hide an incompletely closed
    resource and let a reconnect open a second sink over it.
    """

    def test_cleanup_once_retains_playback_owner_on_close_failure(self):
        sink = _FakePlaybackSink(close_raises=True)
        self.app._hid_listener = None
        self.app._ble_session = _FakeBleSession()
        self.app._playback = sink

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("audio playback", str(ctx.exception))

        self.assertIs(self.app._playback, sink)
        self.assertEqual(sink.close_calls, 1)
        self.assertFalse(sink.closed)

    def test_on_pcm_frame_write_fail_then_close_raise_retains_owner(self):
        sink = _FakePlaybackSink(fail_write=True, close_raises=True)
        self.app._playback = sink
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise

        # Retained, not discarded - close() also failed:
        self.assertIs(self.app._playback, sink)
        self.assertEqual(sink.close_calls, 1)
        # Still fails closed via reconnect either way:
        self.assertEqual(reconnect_calls, [1])


class LoggingHandlerCleanupRegressionTests(unittest.TestCase):
    """Regression for XRBM-023 outcome 1: proves _AppWiringTestCase's
    tearDown fix (close/remove the FileHandler, reset ``_configured``)
    actually decouples one app build's logging handler from the next -
    the exact defect that made a real Windows CI runner's
    ``tempfile.TemporaryDirectory().cleanup()`` raise a PermissionError
    on the very first test the suite's discovery order ever runs
    (``CleanupOwnershipTests.test_ble_close_failure_retains_ble_owner_but_
    still_completes_hid_and_playback``): ``logging_setup.get_logger()``
    configures its FileHandler exactly once per process and never closes
    it, so without this cleanup the handle stays open inside that first
    test's temp directory for the rest of the run - and Windows, unlike
    POSIX, refuses to delete a directory containing a still-open handle.
    """

    def test_a_second_app_build_gets_its_own_fresh_handler_after_cleanup(self):
        tmp1 = tempfile.TemporaryDirectory()
        loop1 = None
        try:
            # _build_app_with_owned_loop() (not the bare _build_app()): this
            # test constructs RC003App synchronously, same as
            # _AppWiringTestCase.setUp() - see XRBM-026's
            # EventLoopOwnershipRegressionTests for why a bare
            # asyncio.get_event_loop() call here would leak too.
            _, loop1 = _build_app_with_owned_loop(Path(tmp1.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler1 = logger.handlers[0]
            self.assertEqual(
                Path(handler1.baseFilename).parent, Path(tmp1.name) / "logs"
            )
            self.assertIsNotNone(handler1.stream)

            # Exactly what _AppWiringTestCase.tearDown now does.
            handler1.close()
            logger.removeHandler(handler1)
            logging_setup._configured = False

            self.assertIsNone(handler1.stream)
            self.assertEqual(logger.handlers, [])
        finally:
            asyncio.set_event_loop(None)
            if loop1 is not None:
                loop1.close()
            # Must not raise: on Windows this would be the PermissionError
            # from outcome 1 if the handle above were still open.
            tmp1.cleanup()

        tmp2 = tempfile.TemporaryDirectory()
        loop2 = None
        try:
            _, loop2 = _build_app_with_owned_loop(Path(tmp2.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler2 = logger.handlers[0]
            self.assertIsNot(handler2, handler1)
            self.assertEqual(
                Path(handler2.baseFilename).parent, Path(tmp2.name) / "logs"
            )

            handler2.close()
            logger.removeHandler(handler2)
            logging_setup._configured = False
        finally:
            asyncio.set_event_loop(None)
            if loop2 is not None:
                loop2.close()
            tmp2.cleanup()


class EventLoopOwnershipRegressionTests(unittest.TestCase):
    """Regression for XRBM-026 red evidence (real Windows run 29644660267):
    425 tests passed ("OK (skipped=3)"), then the process printed an ignored
    "unclosed event loop" ResourceWarning for a ProactorEventLoop plus two
    unclosed self-pipe sockets - AFTER unittest's own summary, so
    -W error::ResourceWarning never saw it and the step still exited 0.

    Root cause: RC003App.__init__ builds a ConnectionSupervisor, whose
    __init__ captures ``loop or asyncio.get_event_loop()``
    (connection_supervisor.py). _build_app() runs synchronously in
    _AppWiringTestCase.setUp(), off any running loop - unlike the real app,
    which only ever constructs RC003App inside ``asyncio.run(_run())``
    (app.py), where get_event_loop() correctly returns asyncio.run()'s own
    loop. With no running loop and nothing set for this thread,
    asyncio.get_event_loop() silently creates and caches an implicit
    default loop - shared by every _AppWiringTestCase subclass's setUp() -
    that nothing in the old test suite ever closed.

    These tests prove both halves of the fix: (1) the fixed setUp()/
    tearDown() pattern threads a per-test OWNED loop into ConnectionSupervisor
    instead of that ambient default, and (2) deterministically forcing the
    exact condition real interpreter shutdown eventually creates (every
    strong reference to a loop dropped, including asyncio's own thread-local
    cache, then a GC pass) reproduces the red evidence exactly for the OLD
    pattern while the FIXED pattern never reproduces it - in an isolated
    subprocess, so this test process's own asyncio/event-loop state is never
    touched either way.
    """

    def test_build_app_under_the_fixed_setup_pattern_captures_the_owned_loop(self):
        tmp = tempfile.TemporaryDirectory()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app(Path(tmp.name))
            self.assertIs(app._supervisor._loop, loop)
        finally:
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logging_setup._configured = False
            tmp.cleanup()
            asyncio.set_event_loop(None)
            loop.close()

        self.assertTrue(loop.is_closed())

    def test_unowned_default_loop_pattern_reproduces_the_exact_red_evidence(self):
        # The OLD (pre-fix) construction pattern - asyncio.get_event_loop()
        # with no owned/running loop - run in an isolated subprocess, then
        # forced through the exact condition real interpreter shutdown
        # eventually creates. This deterministically reproduces the red
        # evidence's exact shape: one "unclosed event loop" plus two
        # "unclosed <socket.socket" warnings, both printed as unraisable
        # exceptions from inside __del__, while the script's own exit code
        # still reports 0 - proving why -W error::ResourceWarning alone
        # could never have caught it.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self):\n"
            "        self._loop = asyncio.get_event_loop()\n"
            "objs = [_Sup() for _ in range(3)]\n"
            "assert all(o._loop is objs[0]._loop for o in objs)\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if sys.version_info >= (3, 14):
            # Python 3.14 removed implicit default-loop creation here. The
            # obsolete pattern now fails immediately instead of leaking a
            # loop and its self-pipe sockets until interpreter shutdown.
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("There is no current event loop", result.stderr)
        else:
            self.assertEqual(result.returncode, 0)
            self.assertIn("unclosed event loop", result.stderr)
            self.assertEqual(result.stderr.count("unclosed <socket.socket"), 2)

    def test_owned_and_closed_loop_pattern_never_reproduces_the_red_evidence(self):
        # Same forced-shutdown stress as the test above, but using the FIXED
        # pattern (_AppWiringTestCase.setUp()/tearDown()'s own approach: a
        # fresh loop is created, set current, then explicitly closed)
        # instead of the bare default-loop getter - proving the fix, not
        # just the bug.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self, loop=None):\n"
            "        self._loop = loop or asyncio.get_event_loop()\n"
            "def _build_owned():\n"
            "    loop = asyncio.new_event_loop()\n"
            "    asyncio.set_event_loop(loop)\n"
            "    sup = _Sup()\n"
            "    asyncio.set_event_loop(None)\n"
            "    loop.close()\n"
            "    return sup\n"
            "objs = [_build_owned() for _ in range(3)]\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("ResourceWarning", result.stderr)
        self.assertNotIn("unclosed", result.stderr)


async def _record_connect(connect_calls):
    connect_calls.append(1)


if __name__ == "__main__":
    unittest.main()
