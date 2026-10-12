import unittest
from unittest import mock

from ovb_rc003 import updater


class UpdateServiceTests(unittest.TestCase):
    def test_version_comparison_ignores_v_prefix_and_candidate_suffix(self):
        self.assertGreater(updater._version_key("v0.2.0"), updater._version_key("0.1.0-candidate"))
        self.assertEqual(updater._version_key("0.1.0"), (0, 1, 0))

    def test_latest_release_selects_windows_installer_asset(self):
        payload = {
            "tag_name": "v0.2.0",
            "html_url": "https://github.com/example/repo/releases/tag/v0.2.0",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "https://example/notes"},
                {"name": "RemoSetup.exe", "browser_download_url": "https://example/remo.exe"},
            ],
        }
        with mock.patch.object(updater, "_request_json", return_value=payload):
            info = updater.check_latest_release()
        self.assertTrue(info.available)
        self.assertEqual(info.installer_url, "https://example/remo.exe")

    def test_release_without_installer_fails_closed(self):
        with mock.patch.object(updater, "_request_json", return_value={"tag_name": "v0.2.0", "assets": []}):
            with self.assertRaisesRegex(ValueError, "安装包"):
                updater.check_latest_release()


if __name__ == "__main__":
    unittest.main()
