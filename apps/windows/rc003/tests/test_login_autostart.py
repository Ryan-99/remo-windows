import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, login_autostart


class LoginAutostartTests(unittest.TestCase):
    def test_enable_and_disable_round_trip_on_a_private_key(self):
        if login_autostart.sys.platform != "win32":
            self.skipTest("registry login entry is Windows-only")
        import winreg

        key_path = r"Software\RemoLoginAutostartTest"
        executable = Path(tempfile.gettempdir()) / "remo-login-autostart-test.exe"
        executable.write_bytes(b"")
        try:
            login_autostart.apply(True, executable, key_path)
            self.assertTrue(login_autostart.is_enabled(key_path))
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, login_autostart.VALUE_NAME)
            self.assertIn("--backend ble --execute", value)
            self.assertIn(str(executable), value)
            login_autostart.apply(False, executable, key_path)
            self.assertFalse(login_autostart.is_enabled(key_path))
            login_autostart.apply(False, executable, key_path)
        finally:
            executable.unlink(missing_ok=True)
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass

    def test_missing_daemon_is_not_registered(self):
        if login_autostart.sys.platform != "win32":
            self.skipTest("registry login entry is Windows-only")
        key_path = r"Software\RemoLoginAutostartMissing"
        with self.assertRaises(FileNotFoundError):
            login_autostart.apply(True, Path("Z:/missing/remo-daemon.exe"), key_path)
        self.assertFalse(login_autostart.is_enabled(key_path))


class LaunchAtLoginConfigTests(unittest.TestCase):
    def test_store_changes_only_the_login_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = config.config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"voice_input_preset": "wechat", "voice_hotkey": "lalt+lwin"}) + "\n",
                encoding="utf-8",
            )
            config.store_launch_at_login(root, True)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["voice_input_preset"], "wechat")
            self.assertEqual(stored["voice_hotkey"], "lalt+lwin")
            self.assertIs(stored["launch_at_login"], True)
            self.assertIs(config.load_config(path)["launch_at_login"], True)
            self.assertIs(config.default_config()["launch_at_login"], False)


class SettingsSwitchTests(unittest.TestCase):
    def test_switch_starts_off_and_records_the_choice(self):
        from ovb_rc003 import qt_settings_app

        classes = qt_settings_app._load_qt_classes()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"LOCALAPPDATA": tmp}):
            with mock.patch.object(login_autostart, "is_enabled", return_value=False), mock.patch.object(
                login_autostart, "apply"
            ) as apply:
                controller = classes["SettingsController"](classes["ButtonMappingModel"]())
                self.assertFalse(controller.launchAtLogin)
                controller.launchAtLogin = True
                apply.assert_called_once()
                self.assertTrue(controller.launchAtLogin)
                stored = json.loads(config.config_path(controller._config_root).read_text(encoding="utf-8"))
                self.assertIs(stored["launch_at_login"], True)
