import unittest
from unittest import mock

from ovb_rc003 import updater


class UpdateServiceTests(unittest.TestCase):
    def test_version_comparison_ignores_v_prefix_and_candidate_suffix(self):
        self.assertGreater(updater._version_key("v0.2.0"), updater._version_key("0.1.0-candidate"))
        self.assertEqual(updater._version_key("0.1.0"), (0, 1, 0))

    def test_release_page_redirect_selects_the_versioned_installer(self):
        with mock.patch.object(updater, "_latest_release_tag", return_value="v0.1.2"):
            info = updater.check_latest_release()
        self.assertGreater(updater._version_key(info.latest_version), updater._version_key("0.1.0"))
        self.assertEqual(
            info.installer_url,
            "https://github.com/Ryan-99/remo-windows/releases/download/v0.1.2/RemoSetup-0.1.2.exe",
        )
        self.assertEqual(info.installer_name, "RemoSetup-0.1.2.exe")

    def test_tag_is_read_from_the_release_page_url(self):
        self.assertEqual(
            updater._tag_from_release_url(
                "https://github.com/Ryan-99/remo-windows/releases/tag/v0.1.2"
            ),
            "v0.1.2",
        )

    def test_release_page_without_a_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "最新版本"):
            updater._tag_from_release_url("https://github.com/Ryan-99/remo-windows/releases")


if __name__ == "__main__":
    unittest.main()
