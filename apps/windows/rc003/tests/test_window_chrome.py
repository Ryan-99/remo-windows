"""Startup window chrome must stay optional, including on Windows 10.

Windows 10 rejects the Windows 11 DWM attributes used for rounded corners
and a borderless edge. That failure is E_INVALIDARG, and ctypes raises it
as OSError when a Win32 function is declared with an HRESULT restype. The
settings window used to die on that call before it was shown. Windows 10
then clips the same 12 DIP curve with a window mask.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QGuiApplication, QWindow

from ovb_rc003 import qt_settings_app


class OptionalDwmChromeTests(unittest.TestCase):
    def test_invalid_parameter_on_one_attribute_does_not_abort_the_other(self):
        calls = []

        def set_attribute(hwnd, attribute, _data, _size):
            calls.append((hwnd, attribute))
            if attribute == 33:
                raise OSError(87, "参数错误", None, -2147024809)
            return 0

        self.assertFalse(qt_settings_app._apply_optional_dwm_chrome(42, set_attribute))
        self.assertEqual(calls, [(42, 33), (42, 34)])

    def test_every_dwm_failure_is_ignored(self):
        def set_attribute(_hwnd, _attribute, _data, _size):
            raise OSError(87, "参数错误", None, -2147024809)

        self.assertFalse(qt_settings_app._apply_optional_dwm_chrome(7, set_attribute))

    def test_accepted_corner_attribute_keeps_the_system_frame(self):
        def set_attribute(_hwnd, _attribute, _data, _size):
            return 0

        self.assertTrue(qt_settings_app._apply_optional_dwm_chrome(7, set_attribute))

    def test_rejected_corner_attribute_requests_a_legacy_mask(self):
        def set_attribute(_hwnd, _attribute, _data, _size):
            return -2147024809

        self.assertFalse(qt_settings_app._apply_optional_dwm_chrome(7, set_attribute))


class LegacyRoundMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QGuiApplication.instance() or QGuiApplication([])

    def test_round_mask_keeps_the_center_and_drops_the_square_corners(self):
        region = qt_settings_app._legacy_corner_region(200, 100, False)

        self.assertTrue(region.contains(QPoint(100, 50)))
        self.assertTrue(region.contains(QPoint(0, 50)))
        self.assertFalse(region.contains(QPoint(0, 0)))
        self.assertFalse(region.contains(QPoint(199, 0)))
        self.assertFalse(region.contains(QPoint(0, 99)))
        self.assertFalse(region.contains(QPoint(199, 99)))

    def test_maximized_window_clears_the_mask(self):
        self.assertTrue(qt_settings_app._legacy_corner_region(1120, 760, True).isEmpty())

    def test_install_clips_a_normal_window_and_follows_maximize(self):
        class _Window:
            def __init__(self):
                self._width = 200
                self._height = 100
                self._visibility = QWindow.Visibility.Windowed
                self.mask = None

            def width(self):
                return self._width

            def height(self):
                return self._height

            def visibility(self):
                return self._visibility

            def setMask(self, region):
                self.mask = region

        window = _Window()
        qt_settings_app._install_legacy_round_corners(window)
        self.assertFalse(window.mask.contains(QPoint(0, 0)))
        self.assertTrue(window.mask.contains(QPoint(100, 50)))

        window._visibility = QWindow.Visibility.Maximized
        window._remo_round_mask_sync()
        self.assertTrue(window.mask.isEmpty())

        window._visibility = QWindow.Visibility.Windowed
        window._remo_round_mask_sync()
        self.assertFalse(window.mask.contains(QPoint(0, 0)))
        self.assertTrue(window.mask.contains(QPoint(100, 50)))


if __name__ == "__main__":
    unittest.main()
