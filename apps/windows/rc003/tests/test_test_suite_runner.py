"""Contract tests for the isolated native-runtime test runner."""

import importlib.util
import io
import os
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "run_test_suite.py"
SPEC = importlib.util.spec_from_file_location("rc003_test_suite_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class IsolatedTestSuiteRunnerTests(unittest.TestCase):
    def test_replayed_output_replaces_characters_unsupported_by_a_gbk_console(self):
        class _GbkConsole:
            encoding = "gbk"

            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, _text):
                raise UnicodeEncodeError("gbk", "�", 0, 1, "character not encodable")

            def flush(self):
                return None

        stream = _GbkConsole()
        with mock.patch.object(runner.sys, "stdout", stream):
            runner._write_replayed_output("native probe � passed\n")

        self.assertEqual(
            stream.buffer.getvalue(),
            "native probe � passed\n".encode("gbk", errors="replace"),
        )

    def test_runner_accepts_only_the_pinned_python_312_runtime(self):
        with mock.patch.object(sys, "version_info", (3, 14, 2)):
            with self.assertRaisesRegex(RuntimeError, "require Python 3.12"):
                runner.assert_supported_python()

    def test_discovers_every_test_module_including_windows_subdirectory(self):
        modules = runner.discover_test_modules()
        expected = {
            ".".join(path.relative_to(ROOT).with_suffix("").parts)
            for path in (ROOT / "tests").rglob("test_*.py")
        }
        self.assertEqual(set(modules), expected)
        self.assertIn("tests.windows.test_windows_only", modules)
        self.assertIn("tests.test_release_scripts", modules)

    def test_qt_module_is_isolated_without_omitting_any_module(self):
        modules = runner.discover_test_modules()
        core, isolated_native, windows_native, qt, lifecycle = runner.partition_test_modules(modules)
        self.assertEqual(qt, ("tests.test_qt_settings_app",))
        self.assertEqual(windows_native, ("tests.windows.test_windows_only",))
        self.assertEqual(lifecycle, ("tests.test_qt_lifecycle_cleanup",))
        groups = (set(core), set(isolated_native), set(windows_native), set(qt), set(lifecycle))
        self.assertEqual(set().union(*groups), set(modules))
        for index, group in enumerate(groups):
            for other in groups[index + 1 :]:
                self.assertFalse(group & other)

    def test_real_windows_native_integrations_never_share_the_core_process(self):
        modules = runner.discover_test_modules()
        core, isolated_native, windows_native, _qt, lifecycle = runner.partition_test_modules(modules)
        self.assertNotIn("tests.windows.test_windows_only", core)
        self.assertIn("tests.windows.test_windows_only", windows_native)
        self.assertNotIn("tests.test_qt_lifecycle_cleanup", core)
        self.assertEqual(lifecycle, ("tests.test_qt_lifecycle_cleanup",))
        self.assertTrue(set(runner.ISOLATED_NATIVE_TEST_MODULES) <= set(isolated_native))
        self.assertFalse(set(runner.ISOLATED_NATIVE_TEST_MODULES) & set(core))

    def test_runner_hard_gates_late_native_warning_markers(self):
        self.assertIn("ResourceWarning:", runner.FORBIDDEN_OUTPUT)
        self.assertIn("unclosed event loop", runner.FORBIDDEN_OUTPUT)
        self.assertIn("unclosed <socket.socket", runner.FORBIDDEN_OUTPUT)

    def test_windows_native_group_uses_printable_resource_warnings(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('if label == "windows-native"', source)
        self.assertIn('"default::ResourceWarning"', source)

    def test_runner_preserves_an_explicit_dependency_pythonpath(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('inherited_pythonpath = env.get("PYTHONPATH", "")', source)
        self.assertIn('os.pathsep.join(pythonpath_entries)', source)


if __name__ == "__main__":
    unittest.main()
