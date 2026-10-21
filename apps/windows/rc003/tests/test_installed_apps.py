import tempfile
import unittest
from pathlib import Path

from ovb_rc003 import installed_apps


class InstalledAppTests(unittest.TestCase):
    def test_listing_uses_shortcut_names_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "微信输入法.lnk").write_bytes(b"x")
            nested = root / "nested"
            nested.mkdir()
            (nested / "微信.lnk").write_bytes(b"x")
            (root / "微信.lnk").write_bytes(b"x")
            names = [item["name"] for item in installed_apps.list_start_menu_apps([root])]
        self.assertEqual(names, ["微信", "微信输入法"])

    def test_an_executable_is_stored_by_its_own_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "记事本.exe"
            executable.write_bytes(b"")
            described = installed_apps.describe_application(executable)
        self.assertEqual(described["name"], "记事本")
        self.assertEqual(described["path"], str(executable))

    def test_a_shortcut_stores_the_resolved_executable_when_it_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Weixin.exe"
            executable.write_bytes(b"")
            shortcut = root / "微信.lnk"
            shortcut.write_bytes(b"")
            described = installed_apps.describe_application(
                shortcut, resolve=lambda _path: str(executable)
            )
        self.assertEqual(described["name"], "微信")
        self.assertEqual(described["path"], str(executable))
