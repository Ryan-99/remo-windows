"""Contract checks for the local release and installer acceptance scripts."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = (ROOT / "build" / "package-release.ps1").read_text(encoding="utf-8")
INSTALLER = (ROOT / "build" / "test-installer.ps1").read_text(encoding="utf-8")
BUILD = (ROOT / "build" / "build-candidate.ps1").read_text(encoding="utf-8")


class ReleaseScriptContractTests(unittest.TestCase):
    def test_local_build_delegates_distribution_staging_to_package_script(self):
        self.assertIn("package-release.ps1", BUILD)
        self.assertIn('Assert-LastExitCode "package-release.ps1"', BUILD)

    def test_release_packager_requires_frozen_exe_and_iscc_and_verifies_three_files(self):
        self.assertIn("RemoteMicRC003.exe", PACKAGE)
        self.assertIn("Resolve-Iscc", PACKAGE)
        self.assertIn("Compress-Archive -LiteralPath $stagingDir", PACKAGE)
        self.assertIn("SHA256SUMS.txt", PACKAGE)
        self.assertIn("releaseFiles.Count -ne 3", PACKAGE)
        self.assertIn("malformed SHA256SUMS.txt line", PACKAGE)

    def test_release_packager_carries_provenance_into_portable_zip(self):
        for name in (
            "LICENSE.txt",
            "COPYRIGHT.txt",
            "THIRD_PARTY_NOTICES.md",
            "ATTRIBUTION.md",
            "README.txt",
            "HARDWARE_ACCEPTANCE.md",
        ):
            self.assertIn(name, PACKAGE)

    def test_installer_acceptance_is_current_user_and_reversible(self):
        self.assertIn("/VERYSILENT", INSTALLER)
        self.assertIn("/NORESTART", INSTALLER)
        self.assertIn("Start Menu shortcut", INSTALLER)
        self.assertIn("upgrade did not stop the running installed process", INSTALLER)
        self.assertIn("user-created sentinel after uninstall", INSTALLER)
        self.assertIn("$sentinel = Join-Path $configDir", INSTALLER)
        self.assertNotIn("fetch-vb-cable.ps1", INSTALLER)
        self.assertIn("Uninstallation process succeeded", INSTALLER)

    def test_installer_acceptance_never_runs_bridge_mode(self):
        self.assertNotIn("--bridge", INSTALLER)
        self.assertIn("--dry-run", INSTALLER)


if __name__ == "__main__":
    unittest.main()
