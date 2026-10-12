import struct
import unittest

from ovb_rc003 import driver_contract, raw_input_windows

SELECTED = r"\\?\HID#VID_2717&PID_32B8&REV_00A4#selected#{guid}"
OTHER_REMOTE = r"\\?\HID#VID_2717&PID_32B8&REV_00A4#other#{guid}"
KEYBOARD = r"\\?\HID#VID_0001&PID_0002#keyboard#{guid}"


class FilterInputTests(unittest.TestCase):
    def test_all_eight_translations_are_diagnostic_only(self):
        names = ("volume_up", "volume_down", "back", "home", "menu", "tv", "power", "mic")
        for vkey, name in enumerate(names, 0x7C):
            with self.subTest(name=name):
                events = []
                raw_events = []
                listener = raw_input_windows.RawInputButtonListener(
                    lambda *e: events.append(e), raw_events.append
                )
                listener._device_path = SELECTED
                for message in (0x100, 0x100, 0x101, 0x100, 0x101):
                    listener._handle_keyboard_body(
                        struct.pack("<HHHHII", 0, int(message == 0x101), 0, vkey, message, 0),
                        device_path=SELECTED.lower(),
                    )
                self.assertEqual(events, [])
                self.assertTrue(raw_events)

    def test_reserved_keys_from_unselected_or_missing_source_are_ignored(self):
        for source in (OTHER_REMOTE, KEYBOARD, None, ""):
            for vkey in range(0x7C, 0x84):
                self.assertIsNone(driver_contract.filtered_button(vkey, source, SELECTED))
        self.assertIsNone(driver_contract.filtered_button(0x83, KEYBOARD, KEYBOARD))
        self.assertIsNone(driver_contract.filtered_button(0x83, SELECTED, None))

    def test_reserved_key_translations_do_not_dispatch_from_raw_keyboard(self):
        events = []
        listener = raw_input_windows.RawInputButtonListener(lambda *e: events.append(e))
        listener._device_path = SELECTED
        listener._handle_keyboard_body(struct.pack("<HHHHII", 0, 0, 0, 0x83, 0x100, 0), device_path=SELECTED)
        listener._handle_keyboard_body(struct.pack("<HHHHII", 0, 1, 0, 0x83, 0x101, 0), device_path=KEYBOARD)
        self.assertEqual(events, [])

    def test_native_f5_is_not_a_filter_vk(self):
        self.assertIsNone(driver_contract.filtered_button(0x74, SELECTED, SELECTED))

    def test_filter_button_set_is_exactly_the_reserved_range(self):
        self.assertEqual(
            driver_contract.FILTER_BUTTONS,
            {"volume_up", "volume_down", "back", "home", "menu", "tv", "power", "mic"},
        )
