import ctypes
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import winuhid


class _FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeDll:
    def __init__(self, *, fail_create_number=None, fail_start_number=None):
        self.create_count = 0
        self.start_count = 0
        self.destroyed = []
        self.submitted = []
        self.configs = []
        self.fail_create_number = fail_create_number
        self.fail_start_number = fail_start_number
        self.WinUHidGetDriverInterfaceVersion = _FakeFunction(lambda: 1)
        self.WinUHidCreateDevice = _FakeFunction(self._create)
        self.WinUHidStartDevice = _FakeFunction(self._start)
        self.WinUHidSubmitInputReport = _FakeFunction(self._submit)
        self.WinUHidDestroyDevice = _FakeFunction(self._destroy)

    @staticmethod
    def _wide_string(pointer):
        values = []
        index = 0
        while pointer[index]:
            values.append(int(pointer[index]))
            index += 1
        return b"".join(value.to_bytes(2, "little") for value in values).decode("utf-16-le")

    def _create(self, config_pointer):
        self.create_count += 1
        config = ctypes.cast(
            config_pointer, ctypes.POINTER(winuhid._WinUHidDeviceConfig)
        ).contents
        descriptor = bytes(
            ctypes.string_at(config.report_descriptor, config.report_descriptor_length)
        )
        self.configs.append(
            {
                "product_id": int(config.product_id),
                "descriptor": descriptor,
                "instance_id": self._wide_string(config.instance_id),
            }
        )
        if self.create_count == self.fail_create_number:
            return 0
        return 100 + self.create_count

    def _start(self, handle, _context, _callback):
        self.start_count += 1
        return 0 if self.start_count == self.fail_start_number else 1

    def _submit(self, handle, payload, length):
        self.submitted.append((int(handle), bytes(ctypes.string_at(payload, length))))
        return 1

    def _destroy(self, handle):
        self.destroyed.append(int(handle))


class ReportBuilderTests(unittest.TestCase):
    def test_modifier_bits_preserve_left_and_right_identity(self):
        self.assertEqual(winuhid.build_keyboard_report([0xA5]), bytes([0x40, 0, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(winuhid.build_keyboard_report([0xA2, 0x5B])[0], 0x09)
        self.assertEqual(winuhid.build_keyboard_report([0x5B, 0xA4])[0], 0x0C)

    def test_letters_digits_and_function_keys_map_to_hid_usages(self):
        report = winuhid.build_keyboard_report([0x41, 0x31, 0x30, 0x70, 0x7B])
        self.assertEqual(report[2:7], bytes([0x04, 0x1E, 0x27, 0x3A, 0x45]))

    def test_duplicate_usage_is_emitted_once(self):
        report = winuhid.build_keyboard_report([0x41, 0x41])
        self.assertEqual(report[2:], bytes([0x04, 0, 0, 0, 0, 0]))

    def test_more_than_six_regular_keys_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at most six"):
            winuhid.build_keyboard_report(range(0x41, 0x48))

    def test_unknown_vk_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported VK"):
            winuhid.build_keyboard_report([0xFF])

    def test_consumer_usages_cover_volume_and_playback(self):
        self.assertEqual(winuhid.consumer_usage(0xAF), 0x00E9)
        self.assertEqual(winuhid.consumer_usage(0xAE), 0x00EA)
        self.assertEqual(winuhid.consumer_usage(0xB3), 0x00CD)

    def test_modifier_order_keeps_win_before_alt(self):
        modifiers, keys = winuhid.split_modifiers([0xA4, 0x41, 0x5B, 0xA2])
        self.assertEqual(modifiers, [0xA2, 0x5B, 0xA4])
        self.assertEqual(keys, [0x41])


class AbiContractTests(unittest.TestCase):
    def test_device_config_matches_reference_packed_abi(self):
        config = winuhid._WinUHidDeviceConfig
        self.assertEqual(config._pack_, 1)
        self.assertEqual(config.report_descriptor.offset, 12)
        self.assertEqual(config.container_id.offset, 12 + ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(
            ctypes.sizeof(config),
            32 + (3 * ctypes.sizeof(ctypes.c_void_p)),
        )


class BackendStateMachineTests(unittest.TestCase):
    def _backend(self, fake):
        return winuhid.WinUHidBackend(
            dll_path=Path("C:/fake/WinUHid.dll"), loader=lambda _path: fake
        )

    def test_ensure_creates_two_devices_with_utf16_ids_and_zeroes_them(self):
        fake = _FakeDll()
        backend = winuhid.WinUHidBackend(
            dll_path=Path("C:/fake/WinUHid.dll"),
            loader=lambda _path: fake,
            instance_tag="test-instance",
        )
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(backend.ensure())
        self.assertEqual(
            [row["instance_id"] for row in fake.configs],
            ["RemoteMicKeyboard-test-instance", "RemoteMicConsumer-test-instance"],
        )
        self.assertEqual([row["product_id"] for row in fake.configs], [1, 2])
        self.assertEqual(fake.configs[0]["descriptor"], winuhid.KEYBOARD_REPORT_DESCRIPTOR)
        self.assertEqual(fake.configs[1]["descriptor"], winuhid.CONSUMER_REPORT_DESCRIPTOR)
        self.assertEqual(fake.submitted[-2:], [(101, bytes(8)), (102, bytes(2))])

    def test_default_instance_tags_are_unique_between_backend_instances(self):
        first = winuhid.WinUHidBackend(instance_tag=None)
        second = winuhid.WinUHidBackend(instance_tag=None)
        self.assertNotEqual(first._instance_tag, second._instance_tag)

    def test_second_device_create_failure_destroys_first_device(self):
        fake = _FakeDll(fail_create_number=2)
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(backend.ensure())
        self.assertEqual(fake.destroyed, [101])
        self.assertIn("WinUHid", backend.error)

    def test_second_device_start_failure_destroys_both_handles(self):
        fake = _FakeDll(fail_start_number=2)
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(backend.ensure())
        self.assertEqual(fake.destroyed, [102, 101])

    def test_voice_press_stages_multi_modifier_chord_then_releases(self):
        fake = _FakeDll()
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(backend.ensure())
            fake.submitted.clear()
            backend.press_single([0xA2, 0x5B])
            backend.release_single()
        self.assertEqual(
            fake.submitted,
            [
                (101, bytes([0x01, 0, 0, 0, 0, 0, 0, 0])),
                (101, bytes([0x09, 0, 0, 0, 0, 0, 0, 0])),
                (101, bytes(8)),
            ],
        )

    def test_consumer_control_rejects_combinations_and_releases_zero(self):
        fake = _FakeDll()
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(backend.ensure())
            fake.submitted.clear()
            backend.press([0xAF])
            backend.release([0xAF])
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                backend.press([0xAF, 0x41])
        self.assertEqual(fake.submitted, [(102, b"\xe9\x00"), (102, bytes(2))])

    def test_release_all_attempts_both_collections(self):
        fake = _FakeDll()
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(backend.ensure())
            fake.submitted.clear()
            backend.release_all()
        self.assertEqual(fake.submitted, [(101, bytes(8)), (102, bytes(2))])

    def test_missing_dll_is_an_honest_unavailable_state(self):
        backend = winuhid.WinUHidBackend()
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            winuhid, "dll_candidates", return_value=()
        ):
            self.assertFalse(backend.ensure())
        self.assertIn("未找到", backend.error)

    def test_failure_keeps_stage_and_native_message(self):
        fake = _FakeDll(fail_create_number=1)
        backend = self._backend(fake)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(backend.ensure())
        self.assertEqual(backend.failure_stage, "keyboard_create")
        self.assertIn("stage=keyboard_create", backend.error)
        self.assertIn("WinUHidCreateDevice failed", backend.error)

    def test_dll_candidates_prefer_abi_matched_manual_package(self):
        with mock.patch.object(winuhid, "sys", mock.Mock(platform="win32", executable="C:/remo/Remo.exe", _MEIPASS="C:/remo")), mock.patch.dict(os.environ, {}, clear=True):
            candidates = winuhid.dll_candidates()
        self.assertEqual(candidates[0], Path("C:/remo/winuhid/WinUHid.dll"))
        self.assertEqual(candidates[1], Path("C:/remo/WinUHid.dll"))

    def test_dll_candidate_order_prefers_packaged_vendor_copy_over_legacy_root(self):
        root = Path("C:/remo")
        candidates = winuhid.dll_candidates_for_roots((root,))
        self.assertLess(
            candidates.index(root / "winuhid" / "WinUHid.dll"),
            candidates.index(root / "WinUHid.dll"),
        )

    def test_last_win32_error_is_formatted_when_available(self):
        with mock.patch.object(winuhid.ctypes, "get_last_error", return_value=2), mock.patch.object(
            winuhid.ctypes, "FormatError", return_value="file missing"
        ):
            self.assertEqual(
                winuhid._last_win32_error(),
                "winerror=2 (file missing)",
            )


if __name__ == "__main__":
    unittest.main()
