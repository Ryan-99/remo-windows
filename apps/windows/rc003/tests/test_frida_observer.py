import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ovb_rc003 import frida_hid_tap_injector as injector

spec = importlib.util.spec_from_file_location(
    "observe_frida", Path(__file__).resolve().parents[1] / "tools" / "observe_frida.py"
)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class ObserverDecodeTests(unittest.TestCase):
    def test_all_thirteen_buttons_and_release(self):
        self.assertEqual(len(observer.BUTTONS), 13)
        for usage in [0, *observer.BUTTONS]:
            data = b"\x01\0\0" + usage.to_bytes(2, "little") + b"\0" * 4
            self.assertEqual(observer.valid_report({"raw": data.hex()}),
                             (data, {usage} if usage else set()))

    def test_rejects_unknown_device_layout_and_malformed_payload(self):
        for raw in [None, [], "ff", "zz" * 9, "020000f10000000000", "010000ffff00000000"]:
            self.assertIsNone(observer.valid_report({"raw": raw}))


class LoaderLifetimeTests(unittest.TestCase):
    def kernel(self, wait_result):
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 100
        kernel.VirtualAllocEx.return_value = 200
        kernel.CreateRemoteThread.return_value = 300
        kernel.WaitForSingleObject.return_value = wait_result
        def write(_process, _address, _buffer, length, written):
            written._obj.value = length
            return True
        def exit_code(_thread, code):
            code._obj.value = 1
            return True
        kernel.WriteProcessMemory.side_effect = write
        kernel.GetExitCodeThread.side_effect = exit_code
        return kernel

    def test_timeout_does_not_free_path_while_remote_thread_may_use_it(self):
        kernel = self.kernel(258)
        with patch.object(injector.ctypes, "WinDLL", return_value=kernel):
            with self.assertRaises(TimeoutError):
                injector.inject_library(12, Path("Gadget.dll"))
        kernel.VirtualFreeEx.assert_not_called()
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_completed_loader_releases_path(self):
        kernel = self.kernel(0)
        with patch.object(injector.ctypes, "WinDLL", return_value=kernel):
            injector.inject_library(12, Path("Gadget.dll"))
        kernel.VirtualFreeEx.assert_called_once()


if __name__ == "__main__":
    unittest.main()
