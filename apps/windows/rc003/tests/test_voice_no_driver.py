import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import app as app_module
from ovb_rc003 import legacy_key_suppressor_windows, raw_input_windows


def _bare_app():
    app = app_module.RC003App.__new__(app_module.RC003App)
    app._logger = mock.Mock()
    app._f5_guard_until = 0.0
    app._mic_physical_down = False
    app._legacy_key_suppressor = None
    app._hid_listener = None
    app._input_source = "windows_hid"
    app._ble_session = None
    app._supervisor = SimpleNamespace(wake_retry=mock.Mock())
    return app


class DynamicF5GuardTests(unittest.TestCase):
    def test_dynamic_predicate_keeps_unrelated_keyboard_input_passthrough(self):
        gate = legacy_key_suppressor_windows.LegacyKeySuppressor(
            {0x74},
            should_suppress_event=lambda vk, _flags: vk == 0x74,
        )
        self.assertTrue(gate.should_suppress(0x74, 0))
        self.assertFalse(gate.should_suppress(0x41, 0))
        self.assertFalse(gate.should_suppress(0x74, legacy_key_suppressor_windows.LLKHF_INJECTED))

    def test_unarmed_f5_does_not_wait_in_the_hook_race_window(self):
        gate = legacy_key_suppressor_windows.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74}),
            consume_wait_seconds=10.0,
            should_suppress_event=lambda _vk, _flags: False,
        )
        started = __import__("time").monotonic()
        self.assertFalse(gate.consume_armed_key_event(0x74, 0x3F, False, True))
        self.assertLess(__import__("time").monotonic() - started, 0.25)

    def test_remote_mic_arms_f5_even_when_filter_is_marked_active(self):
        app = _bare_app()
        app._driver_filter_active = True
        suppressor = mock.Mock()
        app._legacy_key_suppressor = suppressor
        app._hid_listener = SimpleNamespace(_device_path=r"\\?\HID#VID_2717&PID_32B8#rc003")
        app._arm_from_direct_usage(0x003E, True)
        suppressor.arm_key_event.assert_called_once_with(
            0x74, 0x3F, False, True, lifetime_seconds=0.300
        )
        self.assertGreater(app._f5_guard_until, 0)

    def test_non_mic_remote_usage_never_arms_global_guard(self):
        app = _bare_app()
        suppressor = mock.Mock()
        app._legacy_key_suppressor = suppressor
        app._arm_from_direct_usage(0x0052, True)
        suppressor.arm_key_event.assert_not_called()
        self.assertEqual(app._f5_guard_until, 0.0)

    def test_raw_input_mic_arm_requires_selected_device_identity(self):
        app = _bare_app()
        suppressor = mock.Mock()
        app._legacy_key_suppressor = suppressor
        selected = r"\\?\HID#VID_2717&PID_32B8#selected#{guid}"
        other = r"\\?\HID#VID_0001&PID_0002#keyboard#{guid}"
        app._hid_listener = SimpleNamespace(_device_path=selected)
        app._on_raw_input_event(
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="mic",
                vkey=0x74,
                make_code=0x3F,
                flags=0,
                device_path=other,
            )
        )
        suppressor.arm_key_event.assert_not_called()
        app._on_raw_input_event(
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="mic",
                vkey=0x74,
                make_code=0x3F,
                flags=0,
                device_path=selected,
            )
        )
        suppressor.arm_key_event.assert_called_once()


class DriverIndependenceTests(unittest.TestCase):
    def test_frida_report_is_not_disabled_by_filter_state(self):
        app = app_module.RC003App.__new__(app_module.RC003App)
        app._driver_filter_active = True
        app._logger = mock.Mock()
        app._direct_hid_lock = __import__("threading").Lock()
        app._direct_hid_rebaseline = False
        app._direct_hid_usages = set()
        app._frida_mic_session_active = False
        app._last_frida_report_seq = None
        app._last_frida_report_received_at = None
        app._direct_hid_tap_active = False
        app._frida_input_enabled = False
        app._on_button_event = mock.Mock()
        app._arm_from_direct_usage = mock.Mock()
        app._on_direct_hid_report(1, bytes.fromhex("3e0000000000"))
        app._on_button_event.assert_called_once_with("mic", True)

    def test_first_mic_report_after_rebaseline_is_not_dropped(self):
        app = app_module.RC003App.__new__(app_module.RC003App)
        app._logger = mock.Mock()
        app._direct_hid_lock = __import__("threading").Lock()
        app._direct_hid_rebaseline = True
        app._direct_hid_usages = set()
        app._frida_mic_session_active = False
        app._last_frida_report_seq = None
        app._last_frida_report_received_at = None
        app._direct_hid_tap_active = False
        app._frida_input_enabled = False
        app._on_button_event = mock.Mock()
        app._arm_from_direct_usage = mock.Mock()

        app._on_direct_hid_report(1, bytes.fromhex("3e0000000000"), report_seq=7)

        app._on_button_event.assert_called_once_with("mic", True)
        self.assertFalse(app._direct_hid_rebaseline)

    def test_tap_loss_resets_mic_session_latch(self):
        app = app_module.RC003App.__new__(app_module.RC003App)
        app._logger = mock.Mock()
        app._button_gestures = mock.Mock()
        app._mic_physical_down = True
        app._f5_guard_until = 10.0
        app._voice_raw_input_trigger_pending = False
        app._statistics = mock.Mock()
        app._voice_trigger_lock = __import__("threading").Lock()
        app._voice = mock.Mock()
        app._voice.reset.return_value = None
        app._direct_hid_lock = __import__("threading").Lock()
        app._direct_hid_usages = {0x003E}
        app._direct_hid_generation = 1
        app._direct_hid_rebaseline = False
        app._frida_mic_session_active = True
        app._last_button_edges = {}
        app._direct_hid_tap_active = True
        app._frida_input_enabled = True
        app._active_voice_attempt_id = "test"
        app._input_source = "esp32_bridge"
        app._publish_status = mock.Mock()
        app._on_hid_tap_lost()

        self.assertFalse(app._frida_mic_session_active)
        self.assertTrue(app._direct_hid_rebaseline)


if __name__ == "__main__":
    unittest.main()
