"""Entry point wiring: connects the pieces into a running RC003 client.

Windows-only end-to-end (BLE via winrt, HID via Raw Input, key injection via
SendInput). NOT exercised against real hardware in this candidate - no
device pairing/control happens anywhere in this repository or its tests, per
the project's hard boundary. See this package's top-level README.md "Known
gaps" section for what remains 待核验 (to be verified) on a real Windows
machine with a paired RC003.

Reconnect/cleanup contract (fixed after XRBM-014 review RETRY P1 #2 - see
XRBM-014's independent review): ``RC003App`` no longer connects once
and waits forever. ``connection_supervisor.ConnectionSupervisor`` drives a
connect/wait/cleanup/retry loop; a BLE disconnect notification or a protocol
error both call ``request_reconnect()``, which ends the current wait and
guarantees ``_cleanup_once()`` runs before the next connect attempt.
``_cleanup_once()`` releases the voice hotkey, stops the Raw Input listener
(which itself force-releases any stuck button), and closes the BLE session
(which sends MIC_CLOSE, unsubscribes, and closes the device/service) - every
step is individually wrapped so one step's failure never skips the rest.

Voice fail-closed ordering (P1 #3): the output endpoint is resolved and
opened BEFORE any hotkey/MIC_OPEN is sent, not lazily after the device has
already started streaming. If the endpoint is missing or fails to open,
neither the hotkey nor MIC_OPEN are sent at all - voice fails fully closed
while ordinary buttons keep working.

Further fail-closed ordering (XRBM-018, fixing XRBM-014 review round 2 P1
#6): the host hotkey is now sent BEFORE MIC_OPEN, and if it fails to fully
deliver, MIC_OPEN is never sent at all - a device streaming into Windows
without ever having actually tapped/held the configured hotkey is exactly
the "voice opened after host-trigger failure" defect the round-2 review
found. A playback write failure now also fails closed (closes and discards
the sink) and requests a reconnect, instead of logging indefinitely while
the device keeps streaming into nothing.

Cleanup ownership (XRBM-019 P1 #2, fixing XRBM-018 round 2 finding #2):
stopping the Raw Input listener or closing the BLE session can now each
raise when the resource they own reports it is still alive (a thread that
did not stop within its join timeout - see raw_input_windows.py's
``stop()``/ble_transport_winrt.py's ``close()``). ``_cleanup_once()`` still
attempts every one of the four steps (voice hotkey, HID, BLE, playback)
regardless of any single step's outcome, but a step whose owner reports it
is still alive is intentionally left set on ``self._hid_listener``/
``self._ble_session`` - not cleared to ``None`` - so no later code can
mistake a still-running listener/session for a clean slate. Once every step
has been attempted, any such retained-owner failure is aggregated and
raised from ``_cleanup_once()`` itself, which is
``ConnectionSupervisor.run_forever()``'s injected ``cleanup`` callable: that
exception propagates out of ``run_forever()``'s ``finally`` block and ends
the connect/retry loop entirely - the supervisor fails closed rather than
starting a fresh ``connect()`` generation over resources that might still
be live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import List, Optional

from . import (
    audio_output,
    audio_playback,
    action_executor,
    ble_transport_winrt,
    bridge_control,
    bridge_status,
    button_gesture,
    config,
    connection_supervisor,
    doubao_rpc,
    esp32_serial_input,
    frida_compat,
    hid_identity,
    hotkey,
    identity,
    key_mapping,
    legacy_key_suppressor_windows,
    logging_setup,
    raw_input_windows,
    statistics,
    voice_controller,
    win32_input,
    win32_keys,
)
from .atvv_session import AudioStarted, AudioStopped, CapsReceived, MicButtonPressed, PcmStats


class CleanupIncompleteError(RuntimeError):
    """Raised by ``RC003App._cleanup_once()`` when the Raw Input listener
    and/or the BLE session report they are still alive after cleanup was
    attempted - see the module docstring's "Cleanup ownership" note. Every
    other cleanup step still ran before this is raised.
    """


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)


def _voice_output_backend(config_data: dict) -> str:
    """Resolve transport policy, with an explicit test-run override."""

    override = os.environ.get("REMO_VOICE_OUTPUT_BACKEND", "").strip().lower()
    return override or str(config_data.get("voice_output_backend", "winuhid"))


# Opening an IME panel and starting its hold-to-talk shortcut are two separate
# host operations.  A short settling window prevents the second key from
# arriving while the panel is still switching its input context.  This is
# intentionally shared by WeChat/WeType and any future preset that models an
# opener chord followed by a held key.
VOICE_OPENER_SETTLE_SECONDS = 0.12


class RC003App:
    def __init__(self, status_publisher=None) -> None:
        self._config_root = config.config_root()
        self._config = config.load_config(config.config_path(self._config_root))
        self._config_mtime_ns = self._config_file_mtime_ns()
        self._bindings_path = config.key_bindings_path(self._config_root)
        self._bindings = config.load_key_bindings(
            self._bindings_path
        )
        self._bindings_mtime_ns = self._bindings_file_mtime_ns()
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            is_repeatable=self._is_button_repeatable,
            on_trigger=self._on_button_trigger,
        )
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._voice = voice_controller.VoiceController(
            key_mapping.VoiceTriggerMode(self._config["voice_trigger_mode"])
        )
        self._voice_hotkey = hotkey.HotkeySpec.parse(self._config["voice_hotkey"])
        self._voice_hold_hotkey = hotkey.HotkeySpec.parse(
            self._config.get("voice_hold_hotkey") or self._config["voice_hotkey"]
        )
        self._voice_hold_timer: threading.Timer | None = None
        self._voice_hold_started = False
        self._voice_opener_settling = False
        # TOGGLE_INPUT (used by WeChat's Ctrl+Win/Alt pair) is resolved from
        # the physical mic down/up edges.  ATVV has its own lifecycle and must
        # never submit a second host shortcut for the same press.
        self._voice_toggle_input_shortcut_submitted = False
        self._voice_toggle_input_active = False
        win32_input.configure_voice_input_preset(
            self._config.get("voice_input_preset", "custom"),
            backend=_voice_output_backend(self._config),
        )
        self._logger.info(
            "voice output backend policy=%s preset=%s",
            _voice_output_backend(self._config),
            self._config.get("voice_input_preset", "custom"),
        )
        self._voice_audio_start_fallback_pending = False
        self._atvv_audio_active = False
        self._voice_audio_started_waiting_for_legacy_f5 = False
        # Raw Input and the ATVV control channel arrive on different worker
        # threads. Serialize the voice state machine so one physical press
        # cannot race into two host shortcut deliveries.
        self._voice_trigger_lock = threading.Lock()
        self._voice_raw_input_trigger_pending = False
        # When the built-in HOLD shortcut is selected, the low-level F5 hook
        # can deliver one right-Alt edge through the physicalized low-level
        # hook path. Keep this separate from VoiceController's logical state so
        # the normal audio/ATVV lifecycle still deduplicates correctly without
        # sending a second host shortcut.
        self._voice_legacy_transform_key_down = False
        self._voice_legacy_transform_session = False
        self._voice_legacy_transform_emitted = False
        self._legacy_f5_is_down = False
        # Narrow, device-correlated F5 guard. It is armed only by the RC003
        # microphone edge and expires shortly after its matching release.
        self._f5_guard_until = 0.0
        self._mic_physical_down = False
        self._frida_input_enabled = False
        self._ble_session: Optional[ble_transport_winrt.RC003BleSession] = None
        self._hid_listener: Optional[raw_input_windows.RawInputButtonListener] = None
        # Set only while ``input_source`` selects the ESP32 bridge; the two
        # sources never run together (see config.INPUT_SOURCE_*).
        self._esp32_listener: Optional[
            esp32_serial_input.Esp32SerialButtonListener
        ] = None
        self._input_source = str(
            self._config.get("input_source", config.INPUT_SOURCE_WINDOWS_HID)
        )
        if self._input_source not in config.INPUT_SOURCES:
            self._logger.warning(
                "unknown input_source %r; falling back to the Windows HID path",
                self._input_source,
            )
            self._input_source = config.INPUT_SOURCE_WINDOWS_HID
        self._legacy_key_suppressor: Optional[
            legacy_key_suppressor_windows.LegacyKeySuppressor
        ] = None
        # The legacy path is only a single-VK F5 guard. It is not a general
        # keyboard mapper and never consumes ordinary physical keyboard keys.
        self._legacy_path_disabled = True
        # RC003 filter state is retained for diagnostics/backwards-compatible
        # experiments only. It is never a prerequisite for basic voice or
        # Frida HID mapping.
        self._driver_filter_active = False
        self._hid_report_tap: Optional[frida_compat.RC003HidReportTap] = None
        self._frida_tap_start_task: asyncio.Task | None = None
        self._direct_hid_usages: set[int] = set()
        self._direct_hid_lock = threading.Lock()
        # True once the tap has reported at least one full keyboard snapshot.
        # While the tap side channel is live, the keyboard Raw Input path
        # stands down so the same physical edge is not armed/dispatched twice.
        self._direct_hid_tap_active = False
        self._direct_hid_generation = 0
        self._direct_hid_rebaseline = False
        self._last_frida_report_seq: int | None = None
        self._last_frida_report_received_at: float | None = None
        # A microphone edge must remain a single physical session even when
        # the WUDFHost tap emits transient empty/duplicate reports while the
        # remote wakes from idle.  Keep the semantic mic state independent of
        # the raw report baseline and reject a new down until the matching up.
        self._frida_mic_session_active = False
        self._voice_attempt_seq = 0
        self._active_voice_attempt_id = ""
        self._active_atvv_session_id: int | None = None
        self._playback: Optional[audio_playback.EndpointPlaybackSink] = None
        self._voice_pcm_stats = PcmStats()
        self._statistics = statistics.StatisticsStore(self._config_root)
        self._statistics_pressed_buttons: set[str] = set()
        self._last_button_edges: dict[str, bool] = {}
        self._windows_filter_buttons: set[str] = set()
        self._status_publisher = status_publisher or bridge_status.NullBridgeStatusPublisher()

        self._supervisor = connection_supervisor.ConnectionSupervisor(
            connect=self._connect_once,
            cleanup=self._cleanup_once,
            retry_delay=float(self._config.get("retry_delay", 2.0)),
            max_retry_delay=float(self._config.get("max_retry_delay", 60.0)),
            logger=self._logger,
            on_retry=self._on_reconnect_scheduled,
        )

    # -- lifecycle: driven by ConnectionSupervisor -------------------------

    def _on_reconnect_scheduled(self, attempt: int, delay: float) -> None:
        self._publish_status("reconnecting", f"将在 {delay:g} 秒后重试连接", attempt=attempt)

    async def run_forever(self) -> None:
        self._publish_status("starting", "桥接进程已启动")
        try:
            await self._supervisor.run_forever()
        except BaseException:
            self._publish_status("error", "桥接异常退出")
            raise
        else:
            self._publish_status("stopped", "桥接已停止")

    async def stop(self) -> None:
        self._publish_status("stopping", "正在释放蓝牙和音频资源")
        await self._supervisor.stop()

    async def _connect_once(self) -> None:
        # The ESP32 bridge *is* the BLE central.  Attempting the WinRT connect
        # alongside it would fail (the remote accepts one central at a time)
        # and, because the HID listener is only armed after a successful BLE
        # connect, would leave the bridge path permanently unreachable.  The
        # two input sources are therefore separate connect strategies rather
        # than two things that both run.
        if self._input_source == config.INPUT_SOURCE_ESP32_BRIDGE:
            await self._connect_esp32_once()
            return

        self._legacy_path_disabled = True
        # Frida is optional.  Do not advertise it as the production owner
        # before the tap has actually connected; doing so suppresses the
        # device-scoped Raw Input fallback during injection failures.
        self._frida_input_enabled = False
        self._start_f5_guard()
        if self._hid_report_tap is None and self._frida_tap_start_task is None:
            with self._direct_hid_lock:
                self._direct_hid_rebaseline = True
            self._frida_tap_start_task = asyncio.create_task(
                asyncio.to_thread(self._start_hid_report_tap),
                name="rc003-frida-tap-start",
            )
            self._frida_tap_start_task.add_done_callback(self._frida_tap_start_done)

        self._publish_status(
            "connecting",
            "正在查找并连接 RC003",
            attempt=self._supervisor.attempt_count,
        )
        try:
            self._logger.info("startup: resolving RC003 identity")
            # The filter's F13-F20 edges must not depend on ATVV readiness.
            await asyncio.to_thread(self._start_hid_listener)
            candidates = await ble_transport_winrt.discover_candidates()
            # Fail-closed by construction: raises NoCandidateFoundError or
            # AmbiguousCandidateError instead of guessing.
            candidate = identity.select_single_candidate(candidates)
            self._logger.info("startup: exactly one RC003 candidate resolved")

            self._ble_session = ble_transport_winrt.RC003BleSession(
                on_pcm_frame=self._on_pcm_frame,
                on_control_event=self._on_control_event,
                on_error=self._on_session_error,
                on_disconnected=self._on_disconnected,
                gain_db=float(self._config["gain_db"]),
            )
            await asyncio.wait_for(self._ble_session.connect(candidate), timeout=30.0)
            self._publish_status("connecting", "正在确认遥控器语音协议")
            await self._ble_session.wait_ready()

            self._publish_status(
                "connected",
                "RC003 已连接",
                attempt=self._supervisor.attempt_count,
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            self._publish_status(
                "reconnecting",
                "本次连接失败，桥接将在等待后重试",
                attempt=self._supervisor.attempt_count,
            )
            raise

    async def _connect_esp32_once(self) -> None:
        """Connect strategy for ``input_source = "esp32_bridge"``.

        The bridge owns the BLE link, so there is no WinRT session, no ATVV
        channel and no Windows HID listener here - the serial reader is the
        only input source.  Voice therefore fails closed with an explicit
        status rather than a silent dead microphone key.

        The blocking open/join work runs off the event loop so a slow USB
        enumeration cannot stall the supervisor's cancellation.
        """

        self._publish_status(
            "connecting",
            "正在查找 ESP32 桥接板",
            attempt=self._supervisor.attempt_count,
        )
        try:
            listener = esp32_serial_input.Esp32SerialButtonListener(
                self._on_button_event,
                on_closed=self._on_esp32_bridge_closed,
                port=str(self._config.get("esp32_serial_port", "") or ""),
                baud=int(self._config.get("esp32_serial_baud", 115200)),
                start_timeout=float(self._config.get("esp32_start_timeout", 5.0)),
                ping_interval=float(self._config.get("esp32_ping_interval", 10.0)),
                probe_ports=bool(self._config.get("esp32_serial_probe", True)),
                probe_timeout=float(
                    self._config.get("esp32_serial_probe_timeout", 2.5)
                ),
                scan_all_ports=bool(
                    self._config.get("esp32_serial_scan_all_ports", False)
                ),
                logger=self._logger,
            )
            await asyncio.to_thread(listener.start)
            self._esp32_listener = listener
            self._logger.info("startup: ESP32 bridge serial input enabled")
            self._publish_status(
                "connected",
                "ESP32 桥接已连接（语音功能暂不可用）",
                attempt=self._supervisor.attempt_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._publish_status(
                "reconnecting",
                "未找到 ESP32 桥接板，稍后重试",
                attempt=self._supervisor.attempt_count,
            )
            raise

    def _publish_status(
        self,
        state: str,
        detail: str = "",
        *,
        audio_active: bool = False,
        attempt: int = 0,
    ) -> None:
        try:
            published = self._status_publisher.publish(
                state=state,
                detail=detail,
                audio_active=audio_active,
                attempt=attempt,
            )
            if published is False:
                self._logger.info("bridge status update could not be persisted")
        except Exception:
            self._logger.exception("bridge status update failed")


    def _start_hid_listener(self) -> None:
        """Best-effort: buttons fail closed independently of BLE/voice.

        Multiple matching HID device paths -> fail closed for buttons only
        (log and leave the listener unstarted); this must not tear down the
        BLE/voice path, which does not depend on HID at all.

        XRBM-019 review round 1 P1 #3: a failed ``start()`` call does not
        necessarily mean the listener never came alive - it may have left a
        thread/window behind that its own bounded failed-start cleanup could
        not stop (see raw_input_windows.py's ``_abandon_failed_start()``,
        which keeps ``is_running`` honest for exactly this reason). Clearing
        ``self._hid_listener`` to ``None`` unconditionally here would lose
        that owner reference and let a later ``_connect_once()`` generation
        start a second listener over the still-live one. Only clear it once
        the listener itself confirms it is not running; otherwise retain and
        re-raise so this propagates up through ``_connect_once()`` into
        ``ConnectionSupervisor.run_forever()``'s except handler, which still
        falls through to ``_cleanup_once()`` - giving cleanup a chance to
        retry stopping it, exactly like any other retained-owner failure.
        """

        self._legacy_path_disabled = True
        try:
            paths = raw_input_windows.enumerate_matching_device_paths()
            device_path = hid_identity.select_single_device_path(paths)
        except raw_input_windows.RawInputUnavailableError as exc:
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            return
        except hid_identity.NoDevicePathFoundError:
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            return
        except hid_identity.AmbiguousDevicePathError as exc:
            self._logger.info(
                "startup: buttons failing closed, ambiguous HID device paths: %s", exc
            )
            return

        # Raw Input remains device-scoped and is used for diagnostics plus the
        # narrow F5 correlation. Production button mapping stays on Frida,
        # which carries source-specific HID report evidence.
        raw_input_callback = self._on_windows_button_event
        try:
            self._hid_listener = raw_input_windows.RawInputButtonListener(
                raw_input_callback,
                dispatch_keyboard_events=False,
            )
        except TypeError:
            # Keep compatibility with lightweight adapters used by older
            # integrations and tests; production still uses the explicit
            # observation-only constructor contract.
            self._hid_listener = raw_input_windows.RawInputButtonListener(raw_input_callback)
        set_physical_bindings = getattr(
            self._hid_listener, "set_physical_bindings", None
        )
        if callable(set_physical_bindings):
            set_physical_bindings(self._bindings.get("physical_bindings", {}))
        set_raw_event_callback = getattr(self._hid_listener, "set_raw_event_callback", None)
        if set_raw_event_callback is not None:
            set_raw_event_callback(self._on_raw_input_event)
        try:
            self._hid_listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if self._hid_listener.is_running:
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            self._hid_listener = None
            return

        self._logger.info("startup: device-scoped Raw Input listener enabled")

    def _start_f5_guard(self) -> None:
        """Start the reference project's narrow no-driver F5 protection.

        The hook is installed once for the bridge lifetime, but its dynamic
        predicate returns true only after the RC003 microphone edge has been
        observed. It therefore cannot consume ordinary keyboard input.
        """

        if self._legacy_key_suppressor is not None:
            return
        try:
            suppressor = legacy_key_suppressor_windows.LegacyKeySuppressor(
                {0x74},
                on_key_event=self._on_legacy_key_event,
                rc003_vk_codes=frozenset({0x74}),
                # The production LL callback must never wait for Raw Input;
                # late device correlation is handled by the F5 guard state.
                consume_wait_seconds=0.0,
                should_suppress_event=self._should_suppress_f5,
                async_dispatch=True,
            )
            suppressor.start()
            self._legacy_key_suppressor = suppressor
            self._logger.info(
                "startup: narrow RC003 F5 guard enabled; ordinary keyboard input remains passthrough"
            )
        except legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError as exc:
            # Basic Frida/ATVV voice remains usable without the guard. Keep
            # this degradation visible rather than failing BLE connection.
            self._legacy_key_suppressor = None
            self._logger.warning(
                "startup: RC003 F5 guard unavailable; basic voice remains enabled: %s",
                exc,
            )

    def _start_hid_report_tap(self) -> None:
        """The Windows input source uses Frida reports, never Raw Input."""

        tap = frida_compat.RC003HidReportTap(
            self._on_direct_hid_report,
            on_lost=self._on_hid_tap_lost,
        )
        try:
            if tap.start():
                self._hid_report_tap = tap
                # start() only means the observer thread is running. The tap
                # does not own production input until a validated HID report
                # reaches _on_direct_hid_report().
                self._frida_input_enabled = False
                self._logger.info(
                    "startup: RC003 HID report tap waiting for first valid report"
                )
            else:
                self._frida_input_enabled = False
                self._logger.warning(
                    "Frida input unavailable; using device-scoped Raw Input fallback: %s",
                    tap.status,
                )
        except Exception:
            self._logger.exception("startup: RC003 HID report tap failed to start")
            try:
                tap.stop()
            except Exception:
                self._logger.exception("startup: RC003 HID report tap cleanup failed")
            self._frida_input_enabled = False

    def _on_hid_tap_lost(self) -> None:
        # A synthetic release from disconnect must not execute a click action.
        self._logger.warning(
            "voice stage: physical_input_cancelled attempt_id=%s reason=frida_transport_lost",
            self._active_voice_attempt_id or "unassociated",
        )
        self._button_gestures.reset()
        self._mic_physical_down = False
        self._f5_guard_until = 0.0
        self._voice_raw_input_trigger_pending = False
        self._statistics.voice_stopped()
        with self._voice_trigger_lock:
            action = self._voice.reset()
            if action is not None and not self._apply_voice_action(action):
                self._voice.restore_pending(action)
        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
            self._direct_hid_generation += 1
            self._direct_hid_rebaseline = True
            # The semantic mic latch belongs to the tap generation. A lost
            # transport cancels the old physical session; keeping it set
            # would suppress the first mic down after reconnect forever.
            self._frida_mic_session_active = False
        self._last_button_edges.clear()
        self._direct_hid_tap_active = False
        self._frida_input_enabled = False
        self._publish_status("error", "遥控器按键通道已断开，正在重连")
        # The tap owns the HID input side channel independently from BLE and
        # ATVV. Restart only its runtime; do not tear down the audio session.
        if self._input_source == config.INPUT_SOURCE_WINDOWS_HID:
            self._logger.warning("RC003 HID tap lost; scheduling tap restart")
            try:
                self._supervisor._loop.call_soon_threadsafe(self._restart_hid_tap)
            except Exception:
                self._logger.exception("failed to schedule RC003 HID tap restart")

    def _restart_hid_tap(self) -> None:
        tap = self._hid_report_tap
        if tap is not None:
            try:
                tap.stop()
                self._hid_report_tap = None
                self._start_hid_report_tap()
            except Exception:
                self._logger.exception("RC003 HID tap restart failed")

    def _frida_tap_start_done(self, task: asyncio.Task) -> None:
        if self._frida_tap_start_task is task:
            self._frida_tap_start_task = None
        try:
            task.result()
        except Exception:
            self._frida_input_enabled = False
            self._logger.exception("Frida input startup task failed")

    def _on_diagnostic_hid_report(self, report_id: int, payload: bytes) -> None:
        self._logger.info("Frida diagnostic report: id=%s bytes=%s", report_id, len(payload))

    def _on_direct_hid_report(
        self,
        report: frida_compat.HidReport | int,
        payload: bytes | None = None,
        *,
        report_seq: int | None = None,
        received_at: float | None = None,
    ) -> None:
        """Translate every RC003 keyboard HID usage into button edges.

        Includes microphone down/up. The report tap is the source-specific
        capture path; the separate F5 guard only protects the translated
        microphone key and never handles ordinary keyboard mappings.
        """

        if isinstance(report, frida_compat.HidReport):
            report_id = report.report_id
            payload = report.payload
            report_seq = report.seq
            received_at = report.received_at
        else:
            report_id = report
        if payload is None or report_id != 1 or len(payload) != 6:
            self._logger.debug("RC003 direct HID report ignored: id=%s bytes=%s", report_id, len(payload or b""))
            return
        active = {
            int.from_bytes(payload[index : index + 2], "little")
            for index in range(0, len(payload), 2)
        } & set(frida_compat.TAP_USAGE_TO_BUTTON)
        with self._direct_hid_lock:
            if self._direct_hid_rebaseline:
                # Establish the new baseline, but preserve a mic down carried
                # by the first valid report. The old unconditional return
                # dropped exactly that edge after a Frida reconnect.
                previous_baseline = set(self._direct_hid_usages)
                self._direct_hid_usages = set(active)
                self._direct_hid_rebaseline = False
                pressed = active - previous_baseline
                released = previous_baseline - active
                is_rebaseline = True
            else:
                previous = self._direct_hid_usages
                if active == previous:
                    self._last_frida_report_seq = report_seq
                    self._last_frida_report_received_at = time.monotonic() if received_at is None else received_at
                    self._frida_input_enabled = True
                    return
                pressed = active - previous
                released = previous - active
                self._direct_hid_usages = set(active)
                is_rebaseline = False
        self._last_frida_report_seq = report_seq
        self._frida_input_enabled = True
        self._last_frida_report_received_at = (
            time.monotonic() if received_at is None else received_at
        )
        if is_rebaseline:
            self._logger.info(
                "RC003 HID tap baseline restored: report_seq=%s active_buttons=%s",
                report_seq if report_seq is not None else "legacy",
                ",".join(frida_compat.TAP_USAGE_TO_BUTTON[u] for u in sorted(active)) or "-",
            )
        # Frida can briefly produce alternating malformed/empty snapshots
        # during a BLE wake-up.  Ordinary buttons may be re-baselined, but a
        # mic session must not be toggled by a synthetic down/up pair: that is
        # exactly what makes Doubao appear to open and immediately close.
        if 0x003E in pressed:
            if self._frida_mic_session_active:
                pressed = pressed - {0x003E}
            else:
                self._frida_mic_session_active = True
        if 0x003E in released:
            if not self._frida_mic_session_active:
                released = released - {0x003E}
            else:
                self._frida_mic_session_active = False
        self._logger.info(
            "RC003 direct HID report: report_seq=%s received_at=%.6f usages=%s active_buttons=%s",
            self._last_frida_report_seq if self._last_frida_report_seq is not None else "legacy",
            self._last_frida_report_received_at or time.monotonic(),
            ",".join(f"0x{usage:04x}" for usage in sorted(active)) or "-",
            ",".join(frida_compat.TAP_USAGE_TO_BUTTON[usage] for usage in sorted(active)) or "-",
        )
        if active:
            self._direct_hid_tap_active = True
        for usage in sorted(pressed):
            button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
            self._logger.info(
                "RC003 direct HID usage down: 0x%04x -> %s",
                usage,
                button,
            )
            self._arm_from_direct_usage(usage, True)
            self._on_button_event(button, True)
        for usage in sorted(released):
            button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
            self._logger.info(
                "RC003 direct HID usage up: 0x%04x -> %s",
                usage,
                button,
            )
            self._arm_from_direct_usage(usage, False)
            self._on_button_event(button, False)

    def _arm_from_direct_usage(self, usage: int, is_pressed: bool) -> None:
        """Arm the microphone's translated F5 edge seen by the tap thread.

        Uses the same vk/scan/extended values the low-level hook observes for
        that physical key, so ``consume_armed_key_event`` matches regardless
        of whether the arm arrived from Raw Input or from the tap.
        """

        # F5 is the only translated RC003 key that needs protection. Other
        # remote keys stay on the source-specific Frida path and are never
        # armed in a process-global hook.
        if usage != 0x003E:
            return
        self._arm_f5_guard(is_pressed)
        suppressor = self._legacy_key_suppressor
        if suppressor is not None:
            suppressor.arm_key_event(
                0x74,
                0x3F,
                False,
                bool(is_pressed),
                lifetime_seconds=0.300,
            )
            self._logger.info(
                "voice stage: f5_guard_armed source=frida edge=%s",
                "down" if is_pressed else "up",
            )

    def _arm_f5_guard(self, is_pressed: bool) -> None:
        now = time.monotonic()
        # Keep the guard live for the physical hold; after release retain a
        # short tail for native F5 up/typematic ordering races.
        self._f5_guard_until = now + (30.0 if is_pressed else 0.300)

    def _should_suppress_f5(self, vk_code: int, flags: int) -> bool:
        if vk_code != 0x74 or (flags & legacy_key_suppressor_windows.LLKHF_INJECTED):
            return False
        return self._mic_physical_down or time.monotonic() < self._f5_guard_until


    async def _cleanup_once(self) -> None:
        """Every step is independently attempted: one failing must never
        skip the rest (XRBM-014 review RETRY P1 #4). XRBM-019 P1 #2/#5: the
        HID listener and BLE session owners are only cleared to ``None``
        when their own stop()/close() call reports success - if either
        reports its resource is still alive (raises), the owner reference
        is deliberately retained so no later code can mistake a still-
        running listener/session for a clean slate, and this method raises
        ``CleanupIncompleteError`` once every step has still been
        attempted (see the module docstring's "Cleanup ownership" note for
        how that ends the connect/retry loop).
        """

        failures: List[str] = []

        if self._hid_report_tap is not None:
            try:
                self._hid_report_tap.stop()
                self._hid_report_tap = None
            except Exception:
                self._logger.exception("cleanup: stopping the RC003 HID report tap failed")
                failures.append("RC003 HID report tap did not stop; owner retained")
        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
            self._frida_mic_session_active = False
        self._direct_hid_tap_active = False
        self._driver_filter_active = False
        self._windows_filter_buttons.clear()

        # Cancel gesture timers before stopping Raw Input. The listener's
        # forced releases then clear the dispatcher state without a late
        # double/long callback racing the next connection generation.
        self._button_gestures.reset()
        self._last_button_edges.clear()
        self._statistics_pressed_buttons.clear()
        self._statistics.voice_stopped()
        self._atvv_audio_active = False

        try:
            with self._voice_trigger_lock:
                self._mic_physical_down = False
                self._voice_audio_start_fallback_pending = False
                self._voice_audio_started_waiting_for_legacy_f5 = False
                self._voice_raw_input_trigger_pending = False
                reset_action = self._voice.reset()
                if reset_action is not None and not self._apply_voice_action(reset_action):
                    # _apply_voice_action() already logged the specific failure.
                    # reset() already cleared the controller's own pending
                    # state before we knew delivery would fail - restore it so
                    # a HOLD-mode key isn't recorded as released while it may
                    # still be physically down, and a TOGGLE-mode closing tap
                    # isn't forgotten (XRBM-019 review round 1 P1 #4).
                    self._voice.restore_pending(reset_action)
                    failures.append("voice hotkey release did not fully deliver; state retained")
                self._voice_legacy_transform_key_down = False
                self._voice_legacy_transform_session = False
                self._voice_legacy_transform_emitted = False
                self._legacy_f5_is_down = False
                self._f5_guard_until = 0.0
                self._voice_toggle_input_shortcut_submitted = False
                self._voice_opener_settling = False
        except Exception:
            self._logger.exception("cleanup: releasing the voice hotkey failed")

        if self._hid_listener is not None:
            try:
                self._hid_listener.stop()
                self._hid_listener = None
            except Exception:
                self._logger.exception("cleanup: stopping the Raw Input listener failed")
                failures.append("Raw Input listener did not stop; owner retained")
                # self._hid_listener is intentionally NOT cleared here: it
                # may still be a live thread/window.

        if self._esp32_listener is not None:
            try:
                # stop() releases any held button before joining, so a bridge
                # that vanished mid-press cannot leave that button latched in
                # the gesture dispatcher across the reconnect.
                self._esp32_listener.stop()
                self._esp32_listener = None
            except Exception:
                self._logger.exception("cleanup: stopping the ESP32 serial listener failed")
                failures.append("ESP32 serial listener did not stop; owner retained")
                # self._esp32_listener is intentionally NOT cleared here: it
                # may still own a live thread and a held COM port.

        if self._legacy_key_suppressor is not None:
            try:
                self._legacy_key_suppressor.stop()
                self._legacy_key_suppressor = None
            except Exception:
                self._logger.exception("cleanup: stopping legacy keyboard compatibility hook failed")
                failures.append("legacy keyboard compatibility hook did not stop; owner retained")

        # The low-level Doubao physicalizer belongs to the removed global-hook
        # compatibility path.  Never leave it running across reconnects.
        try:
            doubao_rpc.stop_physicalizer()
        except Exception:
            self._logger.exception("cleanup: stopping legacy voice physicalizer failed")

        timer = self._voice_hold_timer
        self._voice_hold_timer = None
        if timer is not None:
            timer.cancel()
        if self._voice_hold_started:
            try:
                hold_tokens = tuple(self._voice_hold_hotkey.modifiers) + (self._voice_hold_hotkey.key,)
                win32_input.send_voice_key_combo_up(hold_tokens)
                self._logger.info("cleanup: released sustained voice shortcut=%s", "+".join(hold_tokens))
            except Exception:
                self._logger.exception("cleanup: releasing sustained voice shortcut failed")
            finally:
                self._voice_hold_started = False
            failures.append("legacy voice physicalizer did not stop")

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        if self._playback is not None:
            try:
                self._playback.close()
                self._playback = None
            except Exception:
                self._logger.exception("cleanup: closing audio playback failed")
                failures.append("audio playback did not fully close; owner retained")
                # self._playback is intentionally NOT cleared here either -
                # it owns a PortAudio stream; discarding the reference would
                # hide an incompletely closed resource and let a reconnect
                # open a second sink over it (XRBM-019 review round 1 P1
                # #5).

        self._logger.info("cleanup: attempted release of hotkey state and BLE/HID/audio")

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned resources: " + "; ".join(failures)
            )

    # -- disconnect / error callbacks: hand off to the supervisor ----------

    def _on_esp32_bridge_closed(self) -> None:
        """The bridge reader loop ended on its own: unplugged, or board reset."""

        self._logger.info("ESP32 bridge stream ended; requesting reconnect")
        self._publish_status("reconnecting", "ESP32 桥接已断开，正在重连")
        self._supervisor.request_reconnect()

    def _on_disconnected(self) -> None:
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._publish_status("reconnecting", "连接已断开，正在重连")
        self._supervisor.request_reconnect()

    def _on_session_error(self, exc: BaseException) -> None:
        if isinstance(exc, TimeoutError) and "audio" in str(exc).casefold():
            self._logger.warning("ATVV audio session timed out; preserving HID tap: %s", exc)
            self._atvv_audio_active = False
            self._publish_status("connected", "RC003 已连接，语音会话已重置")
            return
        self._logger.info("ATVV protocol error, requesting reconnect: %s", exc)
        self._publish_status("reconnecting", "语音连接异常，正在重连")
        self._supervisor.request_reconnect()

    def _legacy_voice_transform_enabled(self) -> bool:
        """Return whether the optional legacy F5 compatibility path is live.

        The old F5-to-right-Alt transform is retired from production. The
        active no-driver path uses Frida's source-specific mic edge plus the
        separate narrow F5 guard; this predicate remains only for old
        diagnostic compatibility tests.
        """

        # The verified Frida full-report path already provides mic edges. The
        # old global F5 transform is only a legacy fallback and must stay off
        # once that path is active, otherwise F5 leaks to the foreground app.
        if self._direct_hid_tap_active:
            return False
        return not self._legacy_path_disabled and (
            self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD
            and self._voice_hotkey.serialize() in {"ralt", "lctrl+win", "lctrl+lwin"}
        )

    def _emit_legacy_voice_key(
        self,
        target: legacy_key_suppressor_windows.PhysicalKeyTarget,
        is_pressed: bool,
    ) -> bool:
        """Emit exactly one right-Alt edge for a physical F5 edge.

        The original F5 is swallowed by ``LegacyKeySuppressor``. This callback
        emits the single marked right-Alt edge for Doubao's verified callback
        physicalizer. No second host shortcut is sent for this session.
        """

        expected = legacy_key_suppressor_windows.PhysicalKeyTarget(
            vk_code=0xA5,
            scan_code=0x38,
            extended=True,
            system_key=True,
        )
        if target != expected:
            self._voice_legacy_transform_emitted = False
            return False
        try:
            if is_pressed:
                win32_input.send_voice_key_combo_down(("ralt",))
            else:
                win32_input.send_voice_key_combo_up(("ralt",))
            self._voice_legacy_transform_emitted = True
            self._logger.info(
                "voice physical F5 replaced with one right-Alt edge via %s: %s",
                win32_input.voice_backend_name(),
                "down" if is_pressed else "up",
            )
            return True
        except (win32_input.Win32InputUnavailableError, OSError):
            self._voice_legacy_transform_emitted = False
            if is_pressed:
                self._voice_legacy_transform_key_down = False
            self._logger.exception(
                "voice physical right-Alt replacement failed; using host fallback"
            )
            return False

    def _transform_legacy_voice_key(
        self, vk_code: int, is_pressed: bool
    ) -> Optional[legacy_key_suppressor_windows.PhysicalKeyTarget]:
        """Replace a physical RC003 F5 edge with one physical right-Alt edge.

        The callback runs inside the low-level hook.  It deliberately only
        arms a new down edge while no voice trigger is already in flight; a
        matching up edge is still transformed after the app has marked the
        session active.  This prevents a Raw Input duplicate from opening a
        second host shortcut while preserving the hold/release pair.
        """

        if vk_code != 0x74 or not self._legacy_voice_transform_enabled():
            return None
        if is_pressed:
            if (
                self._voice.active
                or self._voice_raw_input_trigger_pending
                or self._voice_legacy_transform_key_down
                or self._legacy_f5_is_down
            ):
                return None
            self._voice_legacy_transform_key_down = True
        elif not self._voice_legacy_transform_key_down:
            return None
        else:
            self._voice_legacy_transform_key_down = False
        return legacy_key_suppressor_windows.PhysicalKeyTarget(
            vk_code=0xA5,
            scan_code=0x38,
            extended=True,
            system_key=True,
        )

    def _on_legacy_key_event(self, vk_code: int, is_pressed: bool) -> None:
        """Use the already-suppressed physical F5 leak as a voice edge.

        Some RC003 firmware/Windows input-class combinations do not produce
        a device-scoped Raw Input keyboard record for the microphone button,
        even though the same physical press is visible to the low-level hook
        as F5. The hook is configured only for that legacy F5 and swallows it
        before it reaches the foreground app; route the edge through the same
        deduplicated voice path as Raw Input.
        """

        if vk_code == 0x74:
            if self._direct_hid_tap_active or self._frida_input_enabled:
                self._logger.debug("ignored legacy F5 edge; Frida mic path owns the key")
                return
            if is_pressed:
                # WH_KEYBOARD_LL also reports auto-repeat key-down messages
                # while the remote button is held.  They are not new remote
                # gestures; collapse them until the matching physical up.
                if self._legacy_f5_is_down:
                    return
                self._legacy_f5_is_down = True
                self._logger.info(
                    "voice legacy F5 trigger received from low-level keyboard hook"
                )
                if (
                    self._voice_legacy_transform_emitted
                    or self._voice_legacy_transform_key_down
                ):
                    self._voice_legacy_transform_session = True
            elif not self._legacy_f5_is_down:
                return
            else:
                self._legacy_f5_is_down = False
            host_action_handled = self._voice_legacy_transform_session
            self._on_button_event(
                "mic",
                is_pressed,
                host_action_handled=host_action_handled,
            )
            self._voice_legacy_transform_emitted = False

    def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
        """Arm the exact original keyboard edge for duplicate suppression.

        The selected RC003 Raw Input listener is device-scoped; the low-level
        keyboard hook is not. Only the mic/F5 pair is passed across this seam;
        arrows, Enter, Home, and consumer keys are never armed here.
        """

        if event.button_id is not None:
            if self._ble_session is None:
                self._supervisor.wake_retry()
            selected = getattr(self._hid_listener, "_device_path", None)
            # Raw Input is already scoped to the selected RC003 device.  The
            # old implementation only enabled mappings after an experimental
            # KMDF filter rewrote the report to F13-F20, which made the
            # no-driver build observe presses but silently skip configured
            # actions.  Mark every selected-device edge as mappable; retain
            # the filter decoder only for backwards-compatible F13-F20
            # reports.  Other keyboards never reach this listener's selected
            # device path and therefore cannot be swallowed or remapped.
            same_device = bool(
                event.device_path
                and selected
                and hid_identity.normalize_device_path(event.device_path)
                == hid_identity.normalize_device_path(selected)
            )
            if same_device and event.button_id == "mic" and event.vkey == 0x74:
                # Raw Input can arrive just after WH_KEYBOARD_LL. The
                # suppressor waits only 60ms for this exact RC003 edge.
                self._arm_f5_guard(event.is_pressed)
                suppressor = self._legacy_key_suppressor
                if suppressor is not None:
                    suppressor.arm_key_event(
                        0x74,
                        int(event.make_code or 0x3F),
                        bool(int(event.flags or 0) & 0x02),
                        bool(event.is_pressed),
                        lifetime_seconds=0.300,
                    )
                    self._logger.info(
                        "voice stage: f5_guard_armed source=raw_input edge=%s",
                        "down" if event.is_pressed else "up",
                    )

    # -- HID button events --------------------------------------------------

    def _bindings_file_mtime_ns(self) -> int:
        try:
            return self._bindings_path.stat().st_mtime_ns
        except OSError:
            return -1

    def _config_file_mtime_ns(self) -> int:
        try:
            return config.config_path(self._config_root).stat().st_mtime_ns
        except OSError:
            return -1

    def _reload_runtime_config_if_changed(self) -> None:
        """Apply voice/output settings saved by the settings process.

        The bridge and QML settings window are separate processes. Previously
        only ``key_bindings.json`` was hot-reloaded, so changing the embedded
        voice preset left the running bridge using the old hotkey until a full
        restart. Reload the scalar runtime config before each physical edge;
        never replace the voice state machine while a session is active.
        """

        current_mtime_ns = self._config_file_mtime_ns()
        if current_mtime_ns == self._config_mtime_ns:
            return
        try:
            refreshed = config.load_config(config.config_path(self._config_root))
            refreshed_mode = key_mapping.VoiceTriggerMode(
                refreshed.get("voice_trigger_mode", "toggle")
            )
            refreshed_hotkey = hotkey.HotkeySpec.parse(
                refreshed.get("voice_hotkey", self._voice_hotkey.serialize())
            )
            refreshed_hold_hotkey = hotkey.HotkeySpec.parse(
                refreshed.get("voice_hold_hotkey") or refreshed_hotkey.serialize()
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._logger.warning("runtime config reload skipped: %s", type(exc).__name__)
            self._config_mtime_ns = current_mtime_ns
            return

        self._config = refreshed
        if not self._voice.active:
            self._config_mtime_ns = current_mtime_ns
            self._voice.trigger_mode = refreshed_mode
        self._voice_hotkey = refreshed_hotkey
        self._voice_hold_hotkey = refreshed_hold_hotkey
        win32_input.configure_voice_input_preset(
            refreshed.get("voice_input_preset", "custom"),
            backend=_voice_output_backend(refreshed),
        )
        self._logger.info(
            "runtime voice config reloaded: preset=%s hotkey=%s mode=%s backend=%s",
            refreshed.get("voice_input_preset", "custom"),
            refreshed_hotkey.serialize(),
            refreshed_mode.value,
            _voice_output_backend(refreshed),
        )

    def _reload_bindings_if_changed(self) -> None:
        """Apply settings edits without requiring a bridge restart."""

        current_mtime_ns = self._bindings_file_mtime_ns()
        if current_mtime_ns == self._bindings_mtime_ns:
            return
        try:
            refreshed = config.load_key_bindings(self._bindings_path)
        except Exception as exc:  # noqa: BLE001 - keep the last valid mapping
            self._logger.warning("settings reload skipped: %s", exc)
            self._bindings_mtime_ns = current_mtime_ns
            return
        self._bindings = refreshed
        self._bindings_mtime_ns = current_mtime_ns
        # The settings window and bridge are separate processes.  Updating
        # only the in-memory action table leaves a running Raw Input listener
        # using stale learned physical signatures until the next reconnect.
        # Push the refreshed overrides into the live listener immediately.
        listener = self._hid_listener
        update_physical_bindings = getattr(listener, "set_physical_bindings", None)
        if callable(update_physical_bindings):
            try:
                update_physical_bindings(refreshed.get("physical_bindings", {}))
            except Exception:
                self._logger.exception(
                    "settings reload: failed to update live physical bindings"
                )
        self._logger.info("settings mappings reloaded from disk")

    def _on_button_event(
        self, button_id: str, is_pressed: bool, *, host_action_handled: bool = False,
        allow_mapping: bool = True, device_path: str | None = None,
    ) -> None:
        if self._input_source == config.INPUT_SOURCE_WINDOWS_HID and device_path is not None:
            listener_path = getattr(self._hid_listener, "_device_path", None)
            if (
                not listener_path
                or hid_identity.normalize_device_path(device_path)
                != hid_identity.normalize_device_path(listener_path)
            ):
                self._logger.warning(
                    "Raw Input mapping rejected: device path mismatch button=%s",
                    button_id,
                )
                return
        elif (
            self._input_source == config.INPUT_SOURCE_WINDOWS_HID
            and self._hid_listener is not None
            and not self._direct_hid_tap_active
            and not host_action_handled
        ):
            self._logger.warning(
                "Raw Input mapping rejected: production event has no device identity button=%s",
                button_id,
            )
            return
        self._reload_runtime_config_if_changed()
        self._reload_bindings_if_changed()
        # Sources already collapse held-key repeats. Compare the current
        # state, not a time window that could erase a rapid up/down pair.
        if self._last_button_edges.get(button_id, False) == bool(is_pressed):
            return
        self._last_button_edges[button_id] = bool(is_pressed)
        report_seq = self._last_frida_report_seq
        source = "frida" if self._direct_hid_tap_active else "legacy"
        self._logger.info(
            "RC003 button edge: source=%s report_seq=%s button=%s state=%s host_action_handled=%s",
            source,
            report_seq if report_seq is not None else "legacy",
            button_id,
            "down" if is_pressed else "up",
            host_action_handled,
        )
        # Publish only the logical RC003 button id and edge state so the
        # settings window can animate a real-remote press. No HID path,
        # address, report bytes, or other device identity crosses processes.
        state = "voice_active" if self._atvv_audio_active else "connected"
        if self._input_source == config.INPUT_SOURCE_WINDOWS_HID and self._ble_session is None:
            state = "reconnecting"
        detail = "遥控器按键已接收" if allow_mapping else "原生按键已接收"
        self._publish_status(state, detail)
        publisher = self._status_publisher
        publish_button = getattr(publisher, "publish", None)
        if callable(publish_button):
            try:
                publish_button(
                    state=state,
                    detail=detail,
                    audio_active=self._atvv_audio_active,
                    attempt=self._supervisor.attempt_count,
                    button_id=button_id,
                    button_state="down" if is_pressed else "up",
                )
            except Exception:
                self._logger.exception("button event status publish failed")
        if button_id != "mic":
            if is_pressed and button_id not in self._statistics_pressed_buttons:
                self._statistics_pressed_buttons.add(button_id)
                self._statistics.record_button(button_id)
            elif not is_pressed:
                self._statistics_pressed_buttons.discard(button_id)
        if not allow_mapping:
            if is_pressed:
                self._logger.info("RC003 native passthrough: button=%s; mapping requires verified filter report", button_id)
            return
        if button_id == "mic":
            if not is_pressed:
                self._mic_physical_down = False
                timer = self._voice_hold_timer
                self._voice_hold_timer = None
                if timer is not None:
                    timer.cancel()
                with self._voice_trigger_lock:
                    if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
                        if self._voice_hold_started:
                            try:
                                hold_tokens = tuple(self._voice_hold_hotkey.modifiers) + (self._voice_hold_hotkey.key,)
                                win32_input.send_voice_key_combo_up(hold_tokens)
                                self._logger.info(
                                    "voice stage: sustained_shortcut_released attempt_id=%s shortcut=%s",
                                    self._active_voice_attempt_id or "unassociated",
                                    "+".join(hold_tokens),
                                )
                            except Exception:
                                self._logger.exception("voice sustained shortcut release failed")
                            finally:
                                self._voice_hold_started = False
                        elif not self._voice_toggle_input_shortcut_submitted:
                            self._logger.info(
                                "voice short press released before opener delivery; no retry is sent"
                            )
                        # The opener is submitted on the physical down edge,
                        # so a short press and a long press share exactly one
                        # IME activation. Release only closes the sustained
                        # hold key; it must never submit a second opener.
                        self._voice_raw_input_trigger_pending = False
                        self._voice_toggle_input_shortcut_submitted = False
                        self._voice_opener_settling = False
                        return
                    self._voice_raw_input_trigger_pending = False
                    # In Frida/physical-edge mode the host shortcut is owned
                    # by the down edge. A release must never synthesize a
                    # second tap for legacy TOGGLE/HOLD controller state.
                    action = self._voice.on_audio_stopped()
                    # A Frida physical edge already submitted the opening
                    # action for the legacy TOGGLE path. Its release is not
                    # an ATVV AUDIO_STOP and must not submit a second TAP;
                    # HOLD still needs the matching KEY_UP below.
                    if (
                        self._frida_input_enabled
                        and self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE
                    ):
                        action = None
                    elif self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE:
                        # The physical down edge already owns the host TAP;
                        # a legacy/raw release is only an edge marker.
                        action = None
                    if self._voice_hold_started:
                        try:
                            hold_tokens = tuple(self._voice_hold_hotkey.modifiers) + (self._voice_hold_hotkey.key,)
                            win32_input.send_voice_key_combo_up(hold_tokens)
                        except Exception:
                            self._logger.exception("voice sustained shortcut release failed")
                        self._voice_hold_started = False
                    if action is not None:
                        if action == voice_controller.VoiceHostAction.KEY_UP:
                            self._logger.info(
                                "voice stage: hold_shortcut_releasing attempt_id=%s shortcut=%s",
                                self._active_voice_attempt_id or "unassociated",
                                self._voice_hotkey.serialize(),
                            )
                        if not self._apply_voice_action(action):
                            self._voice.restore_pending(action)
                            self._supervisor.request_reconnect()
                return
            self._mic_physical_down = True
            with self._voice_trigger_lock:
                self._voice_attempt_seq += 1
                self._active_voice_attempt_id = (
                    f"{self._supervisor.attempt_count}-{self._voice_attempt_seq}"
                )
                self._logger.info(
                    "voice stage: physical_key_arrived attempt_id=%s report_seq=%s received_at=%.6f button_state=down",
                    self._active_voice_attempt_id,
                    report_seq if report_seq is not None else "legacy",
                    self._last_frida_report_received_at or time.monotonic(),
                )
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
                    if self._voice_hold_started:
                        # Repeated HID snapshots while the mic remains held
                        # must never start another shortcut.
                        return
                    self._voice_raw_input_trigger_pending = True
                    self._voice_toggle_input_shortcut_submitted = False
                    # Ctrl+Win opens the input method for both gestures.
                    # Submit it immediately on down; a long press only adds
                    # the configured sustained key after the settle window.
                    if not self._apply_voice_action(
                        voice_controller.VoiceHostAction.TAP,
                        hotkey_spec=self._voice_hotkey,
                    ):
                        self._voice_raw_input_trigger_pending = False
                        return
                    self._voice_toggle_input_shortcut_submitted = True
                    self._statistics.record_button("mic")
                    self._statistics.voice_started()
                    self._logger.info(
                        "voice stage: opener_submitted attempt_id=%s shortcut=%s trigger=physical_down",
                        self._active_voice_attempt_id or "unassociated",
                        self._voice_hotkey.serialize(),
                    )
                    # Long-hold Alt is a continuation of the same physical
                    # gesture. The timer is release-safe and does not block
                    # the HID callback while the IME settles.
                    timer = threading.Timer(0.32, self._start_voice_sustained_shortcut)
                    timer.daemon = True
                    self._voice_hold_timer = timer
                    timer.start()
                    self._logger.info(
                        "voice toggle-input physical trigger: shortcut=%s hold_shortcut=%s",
                        "+".join((*self._voice_hotkey.modifiers, self._voice_hotkey.key)),
                        "+".join((*self._voice_hold_hotkey.modifiers, self._voice_hold_hotkey.key)),
                    )
                    self._logger.info(
                        "voice stage: ime_activation=unknown attempt_id=%s reason=generic_ime_state_unobservable",
                        self._active_voice_attempt_id or "unassociated",
                    )
                    return
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD:
                    # Doubao/Qianwen hold presets must emit only the configured
                    # hold chord.  Do not run the toggle-input opener path.
                    self._voice_raw_input_trigger_pending = True
                    # Match the reference Voice VibeCoding chord state: a new
                    # physical gesture always sanitizes a stale virtual-HID
                    # report before pressing the next chord.
                    try:
                        win32_input.send_voice_key_combo_up(
                            tuple(self._voice_hotkey.modifiers) + (self._voice_hotkey.key,)
                        )
                        self._logger.info(
                            "voice stage: hold_state_sanitized attempt_id=%s shortcut=%s",
                            self._active_voice_attempt_id or "unassociated",
                            self._voice_hotkey.serialize(),
                        )
                    except Exception:
                        self._logger.info(
                            "voice stage: hold_state_sanitize_skipped attempt_id=%s reason=no_previous_report",
                            self._active_voice_attempt_id or "unassociated",
                        )
                    self._logger.info(
                        "voice stage: hold_physical_down attempt_id=%s shortcut=%s backend=%s",
                        self._active_voice_attempt_id or "unassociated",
                        self._voice_hotkey.serialize(),
                        win32_input.voice_backend_name(),
                    )
                    # HOLD uses the primary configured shortcut directly. The
                    # action helper already resolves that shortcut by default;
                    # keeping the call positional preserves lightweight test
                    # doubles and legacy integrations.
                    if not self._apply_voice_action(
                        voice_controller.VoiceHostAction.KEY_DOWN,
                    ):
                        self._voice_raw_input_trigger_pending = False
                        return
                    self._voice_hold_started = True
                    self._statistics.record_button("mic")
                    self._statistics.voice_started()
                    self._logger.info(
                        "voice stage: hold_shortcut_started attempt_id=%s shortcut=%s state=held_until_physical_release",
                        self._active_voice_attempt_id or "unassociated",
                        self._voice_hotkey.serialize(),
                    )
                    return
                if self._voice.active:
                    self._logger.info(
                        "voice physical trigger ignored: voice session already active"
                    )
                    return
                if self._voice_raw_input_trigger_pending:
                    self._logger.info(
                        "voice physical trigger ignored: trigger already in progress"
                    )
                    return
                # The physical key is the earliest reliable signal. Send the
                # host shortcut before the device's audio-start event so
                # voice input is already armed when PCM arrives. The matching
                # ATVV event is consumed by this pending latch.
                self._voice_raw_input_trigger_pending = True
                self._logger.info(
                    "voice physical mic trigger received before audio start"
                )
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE and self._voice.active:
                    close_action = self._voice.on_mic_button_toggled()
                    if close_action is not None and not self._apply_voice_action(close_action):
                        self._voice.restore_pending(close_action)
                else:
                    self._handle_mic_button_pressed(
                        send_device_open=self._frida_input_enabled,
                        host_action_handled=host_action_handled,
                    )
                if self._voice.active and self._voice_hold_hotkey.serialize() != self._voice_hotkey.serialize():
                    timer = threading.Timer(0.28, self._start_voice_sustained_shortcut)
                    timer.daemon = True
                    self._voice_hold_timer = timer
                    timer.start()
                if not self._voice.active:
                    self._voice_raw_input_trigger_pending = False
            return

        if is_pressed:
            self._button_gestures.press(button_id)
        else:
            self._button_gestures.release(button_id)

    def _start_voice_sustained_shortcut(self) -> None:
        # Consume the timer before waiting for the input method to settle.
        # The physical release callback must be able to acquire the lock
        # during this wait and cancel the gesture cleanly.
        with self._voice_trigger_lock:
            if (
                self._voice.trigger_mode != key_mapping.VoiceTriggerMode.TOGGLE_INPUT
                or not self._mic_physical_down
                or self._voice_hold_started
            ):
                return
            self._voice_hold_timer = None
            self._voice_opener_settling = True

        try:
            # Do not hold the trigger lock while sleeping. Otherwise a
            # physical release can be delayed until after the settle window,
            # allowing the sustained key to start after the user lets go.
            if VOICE_OPENER_SETTLE_SECONDS > 0:
                time.sleep(VOICE_OPENER_SETTLE_SECONDS)
            with self._voice_trigger_lock:
                self._voice_opener_settling = False
                if (
                    self._voice.trigger_mode != key_mapping.VoiceTriggerMode.TOGGLE_INPUT
                    or not self._mic_physical_down
                    or not self._voice_toggle_input_shortcut_submitted
                    or self._voice_hold_started
                ):
                    self._logger.info(
                        "voice sustained shortcut cancelled after opener: physical mic released or state reset"
                    )
                    return
                self._logger.info(
                    "voice stage: opener_settled attempt_id=%s delay_ms=%d",
                    self._active_voice_attempt_id or "unassociated",
                    round(VOICE_OPENER_SETTLE_SECONDS * 1000),
                )
                tokens = tuple(self._voice_hold_hotkey.modifiers) + (self._voice_hold_hotkey.key,)
                win32_input.send_voice_key_combo_down(tokens)
                self._voice_hold_started = True
                self._logger.info(
                    "voice stage: sustained_shortcut_started attempt_id=%s shortcut=%s state=held_until_physical_release",
                    self._active_voice_attempt_id or "unassociated",
                    "+".join(tokens),
                )
        except Exception:
            with self._voice_trigger_lock:
                self._voice_opener_settling = False
            self._logger.exception("voice sustained shortcut failed")

    def _on_windows_button_event(self, button_id: str, is_pressed: bool) -> None:
        # Raw Input remains observation-only because its translated keyboard
        # event has no source-specific HID usage.  Production mappings must
        # retain exact device evidence from Frida.
        self._logger.debug(
            "Raw Input mapping callback ignored; Frida is production owner button=%s state=%s",
            button_id,
            "down" if is_pressed else "up",
        )

    def _is_button_action_configured(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> bool:
        action = key_mapping.button_action_for(self._bindings, button_id, trigger)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return False
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            try:
                win32_keys.resolve_vk_codes(action.keys)
            except win32_keys.UnknownKeyTokenError:
                return False
        if action_executor.is_application_action(action):
            # The action is intentional even if the app is currently not
            # installed.  Do not scan Start Menu/WindowsApps from the Raw
            # Input callback; dispatch will report the missing executable and
            # the configured mapping still correctly owns this physical key.
            return True
        return action.kind != key_mapping.ActionKind.VOICE

    def _is_button_repeatable(self, button_id: str) -> bool:
        if button_id not in {
            "up",
            "down",
            "left",
            "right",
            "back",
            "volume_up",
            "volume_down",
        }:
            return False
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        return key_mapping.action_allows_repeat(action)

    def _on_button_trigger(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> None:
        self._reload_bindings_if_changed()
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger(trigger.value),
        )
        try:
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_keys.resolve_vk_codes(action.keys)
        except (KeyError, TypeError, ValueError, win32_keys.UnknownKeyTokenError):
            # A hand-edited or partially corrupted bindings file must disable
            # only the affected button, never escape the Raw Input callback
            # and tear down ordinary-button processing for the whole device.
            self._logger.warning(
                "invalid button binding ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        self._logger.info(
            "RC003 mapped action: button=%s trigger=%s kind=%s keys=%s",
            button_id,
            trigger.value,
            action.kind.value,
            "+".join(action.keys) or "-",
        )
        try:
            publisher = self._status_publisher
            publish_action = getattr(publisher, "publish", None)
            if callable(publish_action):
                publish_action(
                    state="connected",
                    detail="动作执行中",
                    action=action.kind.value,
                    action_state="started",
                )
            self._apply_button_action(action)
            if callable(publish_action):
                publish_action(
                    state="connected",
                    detail="动作已执行",
                    action=action.kind.value,
                    action_state="completed",
                )
        except Exception:
            # Mapping callbacks run on the Frida/socket or gesture timer
            # thread. Never let one malformed/failed action kill that input
            # channel; keep the failure visible in the app log and allow the
            # next physical report to be processed.
            self._logger.exception(
                "RC003 mapped action callback failed: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            publisher = self._status_publisher
            publish_action = getattr(publisher, "publish", None)
            if callable(publish_action):
                try:
                    publish_action(
                        state="error",
                        detail="按键动作执行失败",
                        action=action.kind.value,
                        action_state="failed",
                        action_error="action callback failed",
                    )
                except Exception:
                    self._logger.exception("action failure status publish failed")

    def _apply_button_action(self, action: key_mapping.ButtonAction) -> None:
        try:
            if action.kind == key_mapping.ActionKind.DISABLED:
                self._logger.info("button action ignored: disabled")
                return
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_input.send_key_combo_tap(action.keys)
            elif action.kind == key_mapping.ActionKind.ESCAPE:
                win32_input.send_escape()
            elif action.kind == key_mapping.ActionKind.RETURN:
                win32_input.send_return()
            elif action.kind == key_mapping.ActionKind.ARROW_UP:
                win32_input.send_arrow_up()
            elif action.kind == key_mapping.ActionKind.ARROW_DOWN:
                win32_input.send_arrow_down()
            elif action.kind == key_mapping.ActionKind.ARROW_LEFT:
                win32_input.send_arrow_left()
            elif action.kind == key_mapping.ActionKind.ARROW_RIGHT:
                win32_input.send_arrow_right()
            elif action.kind == key_mapping.ActionKind.DELETE_BACKWARD:
                win32_input.send_delete_backward()
            elif action.kind == key_mapping.ActionKind.SHOW_DESKTOP:
                win32_input.send_show_desktop()
            elif action.kind == key_mapping.ActionKind.CONTEXT_MENU:
                win32_input.send_context_menu()
            elif action.kind == key_mapping.ActionKind.APP_SWITCHER:
                win32_input.send_app_switcher()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_UP:
                win32_input.send_volume_up()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_DOWN:
                win32_input.send_volume_down()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_MUTE:
                win32_input.send_volume_mute()
            elif action.kind == key_mapping.ActionKind.PLAY_PAUSE:
                win32_input.send_play_pause()
            elif action_executor.is_application_action(action):
                if not open_configured_application(action):
                    self._logger.warning(
                        "application action unavailable: action=%s", action.kind.value
                    )
                else:
                    self._logger.info(
                        "application action launched: action=%s", action.kind.value
                    )
            # ActionKind.VOICE is only driven by ATVV control opcodes
            # (_on_control_event), never dispatched from a HID button event.
            self._logger.info("button action delivered: kind=%s", action.kind.value)
        except win32_input.Win32InputUnavailableError:
            self._logger.warning(
                "button action failed: SendInput unavailable kind=%s",
                action.kind.value,
            )
        except OSError as exc:
            self._logger.exception(
                "button action failed to fully deliver: kind=%s error=%s",
                action.kind.value,
                type(exc).__name__,
            )

    # -- ATVV control-channel events (mic button + audio start/stop) ------

    def _on_control_event(self, event: object) -> None:
        self._reload_runtime_config_if_changed()
        if isinstance(event, CapsReceived):
            self._logger.info(
                "voice capabilities received: version=0x%04x sample_rate=%s frame_size=%s",
                event.capabilities.version,
                event.capabilities.sample_rate,
                event.capabilities.frame_size,
            )
        elif isinstance(event, MicButtonPressed):
            with self._voice_trigger_lock:
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
                    if self._voice_hold_started:
                        # This path is normally entered only by the physical
                        # HID edge; a test/legacy caller may still invoke the
                        # ATVV marker before the timer fires. Keep it pending
                        # and let the physical release decide short vs long.
                        self._logger.info("voice ATVV mic marker deferred for toggle-input mode")
                        return
                    # The physical HID edge owns the host shortcut timing.
                    # ATVV's marker is informational only for this mode.
                    self._logger.info(
                        "voice ATVV mic marker ignored for toggle-input mode"
                    )
                    return
                if self._frida_input_enabled:
                    # ATVV never reopens the IME after the physical release.
                    # The Frida edge owns the host shortcut and MIC_OPEN.
                    return
                if self._voice_raw_input_trigger_pending:
                    self._voice_raw_input_trigger_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: matched prior Raw Input trigger"
                    )
                    # Raw Input pre-arms the host only. START_SEARCH still
                    # requires its device response; dropping both lost MIC_OPEN.
                    if self._voice.active and not self._atvv_audio_active and self._ble_session is not None:
                        self._ble_session.send_mic_open_threadsafe()
                elif self._voice_audio_start_fallback_pending:
                    self._voice_audio_start_fallback_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: matched prior AUDIO_STARTED fallback"
                    )
                elif self._voice_audio_started_waiting_for_legacy_f5:
                    self._voice_audio_started_waiting_for_legacy_f5 = False
                    self._logger.info(
                        "voice mic trigger received without F5; using host fallback"
                    )
                    self._handle_mic_button_pressed(send_device_open=False)
                else:
                    if self._voice.active:
                        self._logger.info("voice duplicate START_SEARCH ignored")
                        return
                    if self._legacy_voice_transform_enabled():
                        self._logger.info(
                            "voice mic trigger received from ATVV; waiting for physical F5"
                        )
                        self._voice_audio_started_waiting_for_legacy_f5 = True
                        self._open_playback_for_new_session()
                    else:
                        self._logger.info("voice mic trigger received from ATVV control channel")
                        self._handle_mic_button_pressed()
        elif isinstance(event, AudioStarted):
            with self._voice_trigger_lock:
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
                    self._atvv_audio_active = True
                    self._active_atvv_session_id = event.session_id
                    # Physical HID owns the host state. ATVV only reports the
                    # device audio channel and must not toggle the IME.
                    self._logger.info(
                        "voice ATVV stream tracked without host shortcut for toggle-input mode session_id=%s",
                        event.session_id if event.session_id is not None else "unknown",
                    )
                    self._publish_status(
                        "voice_active",
                        "RC003 已连接，正在接收语音",
                        audio_active=True,
                        attempt=self._supervisor.attempt_count,
                    )
                    return
                self._atvv_audio_active = True
                self._logger.info("voice audio started")
                self._publish_status(
                    "voice_active",
                    "RC003 已连接，正在接收语音",
                    audio_active=True,
                    attempt=self._supervisor.attempt_count,
                )
                self._voice_pcm_stats.reset()
                self._active_atvv_session_id = event.session_id
                self._logger.info(
                    "voice stage: atvv_audio_started attempt_id=%s session_id=%s",
                    self._active_voice_attempt_id or "unassociated",
                    event.session_id if event.session_id is not None else "unknown",
                )
                self._voice_audio_start_fallback_pending = False
                if not self._voice.active and not self._frida_input_enabled:
                    if self._legacy_voice_transform_enabled():
                        self._logger.info(
                            "voice audio started before F5; waiting for physical mic edge"
                        )
                        self._voice_audio_started_waiting_for_legacy_f5 = True
                        self._open_playback_for_new_session()
                    else:
                        self._logger.info("voice audio start used as microphone trigger")
                        self._handle_mic_button_pressed(send_device_open=False)
                        self._voice_audio_start_fallback_pending = self._voice.active
        elif isinstance(event, AudioStopped):
            with self._voice_trigger_lock:
                if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
                    self._atvv_audio_active = False
                    self._active_atvv_session_id = None
                    self._logger.info(
                        "voice ATVV stop tracked without host shortcut for toggle-input mode"
                    )
                    self._publish_status(
                        "connected",
                        "RC003 已连接",
                        attempt=self._supervisor.attempt_count,
                    )
                    return
                self._atvv_audio_active = False
                self._logger.info(
                    "voice stage: atvv_audio_stopped attempt_id=%s session_id=%s",
                    self._active_voice_attempt_id or "unassociated",
                    self._active_atvv_session_id if self._active_atvv_session_id is not None else "unknown",
                )
                self._active_atvv_session_id = None
                self._publish_status(
                    "connected",
                    "RC003 已连接",
                    attempt=self._supervisor.attempt_count,
                )
                self._statistics.voice_stopped()
                stats = self._voice_pcm_stats.summary()
                self._logger.info(
                    "voice PCM summary: frames=%s samples=%s audio_ms=%.0f "
                    "peak=%s rms=%.1f mean_abs=%.1f mean=%.1f "
                    "clipped=%s(%.3f%%) zero_crossings=%s result=%s",
                    stats["frames"],
                    stats["samples"],
                    stats["audio_ms"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["mean"],
                    stats["clipped_samples"],
                    stats["clipped_pct"],
                    stats["zero_crossings"],
                    stats["result"],
                )
                self._voice_audio_start_fallback_pending = False
                self._voice_audio_started_waiting_for_legacy_f5 = False
                self._voice_raw_input_trigger_pending = False
                # Audio restarts are not physical key releases. Preserve the
                # held host shortcut until the mic up (or fault cleanup).
                action = None if self._frida_input_enabled and self._mic_physical_down else self._voice.on_audio_stopped()
                transformed_session = self._voice_legacy_transform_session
                action_applied = (
                    True
                    if action is None
                    else self._apply_voice_action(action)
                )
                if transformed_session:
                    self._voice_legacy_transform_session = False
                if action is not None and not action_applied:
                    # Same rule as _cleanup_once(): on_audio_stopped() already
                    # cleared the controller's pending state before we knew
                    # whether the closing action (HOLD's KEY_UP or TOGGLE's
                    # closing TAP) actually delivered. A failure here must not
                    # be recorded as a clean close - restore the owed state and
                    # fail closed by requesting a reconnect, the same way a BLE
                    # disconnect or a playback write failure does (XRBM-019
                    # review round 1 P1 #4).
                    self._voice.restore_pending(action)
                    self._logger.info(
                        "voice closing action failed to fully deliver; state retained, "
                        "requesting reconnect"
                    )
                    self._supervisor.request_reconnect()

    def _handle_mic_button_pressed(
        self,
        *,
        send_device_open: bool = True,
        host_action_handled: bool = False,
    ) -> None:
        """Submit the host shortcut independently from the audio path.

        ATVV/MIC_OPEN and playback are best-effort device-audio operations;
        neither should prevent a configured OS/IME shortcut from being sent.
        """

        self._voice_audio_started_waiting_for_legacy_f5 = False

        # Host-side IME activation is independent from the BLE microphone
        # audio path. In toggle-input mode, the RC003's keyboard HID edge is
        # sufficient to send the configured chord even while ATVV reconnects.
        if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE_INPUT:
            action = self._voice.on_mic_button_toggled()
            if action is None:
                return
            if not self._apply_voice_action(action):
                self._voice.restore_pending(action)
                return
            if self._voice.active:
                self._statistics.record_button("mic")
                self._statistics.voice_started()
                if send_device_open and self._ble_session is not None:
                    self._ble_session.send_mic_open_threadsafe()
            else:
                self._statistics.voice_stopped()
            return

        action = self._voice.on_mic_button_pressed()
        action_delivered = (
            True
            if host_action_handled
            else self._apply_voice_action(action)
        )
        if host_action_handled:
            self._logger.info(
                "voice host shortcut already handled by physical F5-to-right-Alt transform"
            )
        if not action_delivered:
            # Nothing physically landed (win32_input.py's own batching
            # already rolled back any partial key-down) - clear the
            # controller's logical state without emitting a second,
            # likely-just-as-doomed compensating action.
            self._voice.cancel_pending()
            self._logger.info(
                "voice host shortcut failed; audio start suppressed for this attempt"
            )
            return

        self._statistics.record_button("mic")
        self._statistics.voice_started()
        if self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD:
            self._logger.info(
                "voice stage: hold_shortcut_started attempt_id=%s shortcut=%s state=held_until_physical_release",
                self._active_voice_attempt_id or "unassociated",
                self._voice_hotkey.serialize(),
            )
        self._logger.info(
            "voice stage: ime_activation=unknown attempt_id=%s reason=generic_ime_state_unobservable",
            self._active_voice_attempt_id or "unassociated",
        )

        # Audio is an independent best-effort path. Keep the voice shortcut
        # active even if BLE, endpoint selection, or stream opening fails.
        if self._ble_session is None:
            self._logger.info(
                "voice audio unavailable: BLE session is not connected; host shortcut remains active"
            )
            return
        playback_ready = self._open_playback_for_new_session()
        if send_device_open and playback_ready:
            self._ble_session.send_mic_open_threadsafe()
        elif not self._atvv_audio_active:
            self._ble_session.expect_audio_threadsafe()

    def _apply_voice_action(
        self,
        action: voice_controller.VoiceHostAction,
        *,
        hotkey_spec: hotkey.HotkeySpec | None = None,
    ) -> bool:
        selected_hotkey = hotkey_spec or self._voice_hotkey
        tokens = tuple(selected_hotkey.modifiers) + (selected_hotkey.key,)
        attempt_id = self._active_voice_attempt_id or "unassociated"
        foreground = self._voice_foreground_context()
        if hotkey_spec is None and self._voice_legacy_transform_session:
            if (
                action == voice_controller.VoiceHostAction.KEY_UP
                and self._voice_legacy_transform_key_down
            ):
                # Audio can stop before the remote's leaked F5 key-up arrives.
                # Release the replacement right-Alt edge here so a disconnect
                # or early stream stop can never leave Alt logically held.
                try:
                    win32_input.send_voice_key_combo_up(("ralt",))
                    self._voice_legacy_transform_key_down = False
                    self._voice_legacy_transform_session = False
                    self._logger.info(
                        "voice released right-Alt replacement before physical F5 key-up"
                    )
                    return True
                except (
                    win32_input.Win32InputUnavailableError,
                    OSError,
                ):
                    self._logger.exception(
                        "voice right-Alt replacement release failed"
                    )
                    return False
            self._logger.info(
                "voice host action already delivered by physical F5-to-right-Alt transform: %s",
                action.value,
            )
            return True
        try:
            if action == voice_controller.VoiceHostAction.TAP:
                win32_input.send_voice_key_combo_tap(tokens)
            elif action == voice_controller.VoiceHostAction.KEY_DOWN:
                win32_input.send_voice_key_combo_down(tokens)
            else:
                win32_input.send_voice_key_combo_up(tokens)
            self._logger.info(
                "voice stage: shortcut_submitted attempt_id=%s result=accepted action=%s backend=%s shortcut=%s foreground=%s",
                attempt_id,
                action.value,
                win32_input.voice_backend_name(),
                "+".join(tokens),
                foreground,
            )
            return True
        except win32_input.Win32InputUnavailableError:
            self._logger.info(
                "voice stage: shortcut_submitted attempt_id=%s result=unavailable action=%s shortcut=%s foreground=%s backend=%s backend_error=%s",
                attempt_id,
                action.value,
                "+".join(tokens),
                foreground,
                win32_input.voice_backend_name(),
                win32_input.voice_backend_error() or "unknown",
            )
            return False
        except OSError as exc:
            self._logger.exception(
                "voice stage: shortcut_submitted attempt_id=%s result=failed action=%s shortcut=%s foreground=%s error=%s",
                attempt_id, action.value, "+".join(tokens), foreground, exc,
            )
            return False

    @staticmethod
    def _voice_foreground_context() -> str:
        """Best-effort foreground snapshot; never changes focus or target input."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return "none"
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return f"hwnd=0x{int(hwnd):x},pid={pid.value}"
        except Exception:
            return "unavailable"

    def _open_playback_for_new_session(self) -> bool:
        if self._playback is not None:
            return True
        endpoint_name = self._config.get("output_endpoint_name") or ""
        endpoint_host_api = self._config.get("output_endpoint_host_api") or ""
        try:
            endpoints = audio_output.enumerate_output_endpoints()
            audio_output.resolve_selected_endpoint(endpoints, endpoint_name, endpoint_host_api)
            sink = audio_playback.EndpointPlaybackSink(endpoint_name, endpoint_host_api)
            sink.open()
            self._playback = sink
            self._logger.info(
                "voice playback opened: host_api=%s sample_rate=%s channels=%s",
                endpoint_host_api or "unspecified",
                sink.output_sample_rate_hz,
                sink.output_channels,
            )
            return True
        except audio_output.AudioOutputUnavailableError as exc:
            self._logger.info("voice audio unavailable, failing closed: %s", exc)
            self._playback = None
            return False
        except Exception:
            self._logger.exception("voice audio failed to open, failing closed")
            self._playback = None
            return False

    def _on_pcm_frame(self, samples) -> None:
        """Fails closed on a write failure (XRBM-014 review round 2 P1 #6):
        a broken playback sink must not be left open logging indefinitely
        while the device keeps streaming into it - request a reconnect so
        the next attempt starts from a clean state, unconditionally either
        way. Whether ``self._playback`` itself is cleared here depends on
        whether the follow-up ``close()`` actually succeeds (XRBM-019
        review round 1 P1 #5): a sink whose close call also failed still
        owns a PortAudio stream, and clearing the reference would hide that
        incompletely closed resource and let a reconnect open a second sink
        over it - the owner is only cleared once close() confirms success,
        same rule as ``_cleanup_once()``'s HID/BLE/playback steps.

        Runs on ble_transport_winrt.py's dedicated worker thread (not the
        event loop thread), so ``request_reconnect()`` must be - and is -
        safe to call cross-thread (see connection_supervisor.py).
        """

        if self._playback is None:
            return
        try:
            self._voice_pcm_stats.add(samples)
            if self._voice_pcm_stats.frames in (1, 10) or self._voice_pcm_stats.frames % 200 == 0:
                stats = self._voice_pcm_stats.summary()
                if self._voice_pcm_stats.frames == 1:
                    self._logger.info(
                        "voice stage: pcm_first_frame attempt_id=%s session_id=%s",
                        self._active_voice_attempt_id or "unassociated",
                        self._active_atvv_session_id if self._active_atvv_session_id is not None else "unknown",
                    )
                self._logger.info(
                    "voice PCM progress: frames=%s samples=%s peak=%s rms=%.1f "
                    "mean_abs=%.1f clipped=%.3f%%",
                    stats["frames"],
                    stats["samples"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["clipped_pct"],
                )
            self._playback.write(samples)
        except Exception:
            self._logger.exception("audio playback write failed; failing closed")
            try:
                self._playback.close()
                self._playback = None
            except Exception:
                self._logger.exception("cleanup: closing the failed playback sink failed")
                # self._playback is intentionally NOT cleared here: it may
                # still own a live PortAudio stream.
            self._supervisor.request_reconnect()


async def _run() -> None:
    app = RC003App(
        status_publisher=bridge_status.BridgeStatusPublisher(config.config_root())
    )
    stop_signal = bridge_control.create_bridge_stop_signal()
    stop_waiter_shutdown = asyncio.Event()
    bridge_task = asyncio.create_task(app.run_forever())
    stop_task = asyncio.create_task(
        bridge_control.wait_for_stop_request(
            stop_signal,
            shutdown_event=stop_waiter_shutdown,
        )
    )
    primary_error: Optional[BaseException] = None
    primary_traceback = None
    cleanup_errors: List[BaseException] = []
    try:
        done, _ = await asyncio.wait(
            {bridge_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            if stop_task.result():
                await app.stop()
        await bridge_task
    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__
    finally:
        try:
            await app.stop()
        except BaseException as exc:
            cleanup_errors.append(exc)

        # Both tasks must settle before the named-event handle is closed.
        # The supervisor's stop event releases bridge_task; the local event
        # lets the bounded to_thread waiter return without publishing a fake
        # external stop request on the named event.
        stop_waiter_shutdown.set()
        for task in (bridge_task, stop_task):
            try:
                await task
            except asyncio.CancelledError as exc:
                cleanup_errors.append(exc)
            except BaseException as exc:
                cleanup_errors.append(exc)

        try:
            stop_signal.close()
        except BaseException as exc:
            cleanup_errors.append(exc)

    if primary_error is not None:
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_errors:
        raise cleanup_errors[0]


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
