"""Tests logging_setup.py's log-location helpers (XRBM-029): the canonical
path functions must never create anything as a side effect of merely being
asked a question, and open_log_location() must give an honest answer for a
directory/file that does not exist yet rather than fabricating one - all
exercised against real tmp directories (no mocking needed for pure
filesystem checks) plus an injected ``_open_directory`` for the one actual
OS-facing call, the same dependency-injection pattern
tests/test_bridge_launcher.py uses for subprocess.Popen.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import logging_setup


class LogPathHelpersTests(unittest.TestCase):
    def test_log_dir_is_logs_under_the_given_root(self):
        root = Path("/tmp/example-root")
        self.assertEqual(logging_setup.log_dir(root), root / "logs")

    def test_log_file_path_is_app_log_under_log_dir(self):
        root = Path("/tmp/example-root")
        self.assertEqual(
            logging_setup.log_file_path(root), root / "logs" / "app.log"
        )

    def test_log_dir_does_not_create_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-created-yet"
            logging_setup.log_dir(root)
            logging_setup.log_file_path(root)
            self.assertFalse(root.exists())

    def test_fallback_log_file_uses_a_distinct_process_specific_name(self):
        root = Path("/tmp/example-root")
        self.assertEqual(
            logging_setup.fallback_log_file_path(root, process_id=4242),
            root / "logs" / "app-4242.log",
        )

    def test_secondary_and_emergency_paths_are_outside_the_canonical_directory(self):
        root = Path("/tmp/example-root")
        temp_root = Path("/tmp/example-temp")
        self.assertEqual(
            logging_setup.secondary_log_file_path(root, process_id=4242),
            root / "logs-fallback" / "app-4242.log",
        )
        self.assertEqual(
            logging_setup.emergency_log_file_path(
                root,
                process_id=4242,
                _temp_directory=temp_root,
            ),
            temp_root
            / "RemoteMicRC003-logs"
            / logging_setup.emergency_log_scope(root)
            / "app-4242.log",
        )


class DescribeLogLocationTests(unittest.TestCase):
    def test_directory_missing_when_root_never_existed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "never-run"
            location = logging_setup.describe_log_location(root)
            self.assertEqual(
                location.status, logging_setup.LogLocationStatus.DIRECTORY_MISSING
            )
            self.assertEqual(location.directory, root / "logs")

    def test_file_missing_when_directory_exists_but_no_app_log_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            location = logging_setup.describe_log_location(root)
            self.assertEqual(
                location.status, logging_setup.LogLocationStatus.FILE_MISSING
            )

    def test_ready_when_both_directory_and_file_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "app.log").write_text("hello\n", encoding="utf-8")
            location = logging_setup.describe_log_location(root)
            self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)

    def test_ready_when_only_a_process_specific_fallback_log_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            fallback = log_dir / "app-4242.log"
            fallback.write_text("hello\n", encoding="utf-8")
            location = logging_setup.describe_log_location(root)
            self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)
            self.assertEqual(location.file_path, fallback)

    def test_newer_secondary_fallback_beats_stale_canonical_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_directory = root / "logs"
            canonical_directory.mkdir()
            canonical = canonical_directory / "app.log"
            canonical.write_text("old\n", encoding="utf-8")
            secondary = logging_setup.secondary_log_dir(root)
            secondary.mkdir()
            fallback = secondary / "app-4242.log"
            fallback.write_text("new\n", encoding="utf-8")
            # The timestamps are intentionally explicit so this test does
            # not depend on a filesystem's timestamp resolution.
            os.utime(canonical, (100, 100))
            os.utime(fallback, (200, 200))

            location = logging_setup.describe_log_location(root)

            self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)
            self.assertEqual(location.directory, secondary)
            self.assertEqual(location.file_path, fallback)

    def test_never_creates_the_directory_it_is_only_describing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "still-never-run"
            logging_setup.describe_log_location(root)
            self.assertFalse(root.exists())


class OpenLogLocationTests(unittest.TestCase):
    def test_directory_missing_never_calls_open_and_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "never-run"
            open_calls = []

            result = logging_setup.open_log_location(
                root, _open_directory=lambda directory: open_calls.append(directory)
            )

            self.assertEqual(
                result.outcome, logging_setup.LogOpenOutcome.DIRECTORY_MISSING
            )
            self.assertEqual(open_calls, [])
            # No fake log/directory was fabricated to make this "succeed".
            self.assertFalse(root.exists())

    def test_existing_directory_is_opened_and_reports_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "app.log").write_text("hello\n", encoding="utf-8")
            open_calls = []

            result = logging_setup.open_log_location(
                root, _open_directory=lambda directory: open_calls.append(directory)
            )

            self.assertEqual(result.outcome, logging_setup.LogOpenOutcome.OPENED)
            self.assertEqual(open_calls, [log_dir])
            self.assertEqual(
                result.location.status, logging_setup.LogLocationStatus.READY
            )

    def test_directory_exists_without_app_log_still_opens_and_flags_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            open_calls = []

            result = logging_setup.open_log_location(
                root, _open_directory=lambda directory: open_calls.append(directory)
            )

            self.assertEqual(result.outcome, logging_setup.LogOpenOutcome.OPENED)
            self.assertEqual(len(open_calls), 1)
            self.assertEqual(
                result.location.status, logging_setup.LogLocationStatus.FILE_MISSING
            )

    def test_open_call_raising_is_reported_as_open_failed_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()

            def raising_open(directory):
                raise OSError("no shell association available")

            result = logging_setup.open_log_location(root, _open_directory=raising_open)

            self.assertEqual(result.outcome, logging_setup.LogOpenOutcome.OPEN_FAILED)
            self.assertIn("no shell association available", result.error)

    def test_default_open_directory_raises_cleanly_when_startfile_is_unavailable(self):
        # Proves the off-Windows fallback path (os.startfile does not exist
        # on macOS/Linux) raises a clear OSError rather than an
        # AttributeError - exercised via the real _default_open_directory,
        # with os.startfile itself monkeypatched away for this one test.
        import os

        from ovb_rc003 import logging_setup as module_under_test

        had_startfile = hasattr(os, "startfile")
        original = getattr(os, "startfile", None)
        if had_startfile:
            del os.startfile
        try:
            with self.assertRaises(OSError):
                module_under_test._default_open_directory(Path("/tmp"))
        finally:
            if had_startfile:
                os.startfile = original


class GetLoggerFallbackTests(unittest.TestCase):
    def setUp(self):
        self._logger = logging_setup.logging.getLogger(logging_setup.LOGGER_NAME)
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)
        logging_setup._configured = False
        logging_setup._active_log_file = None
        logging_setup._active_log_root = None

    def tearDown(self):
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)
        logging_setup._configured = False
        logging_setup._active_log_file = None
        logging_setup._active_log_root = None

    def test_busy_canonical_file_falls_back_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_handler = logging_setup.logging.FileHandler

            def file_handler(path, *args, **kwargs):
                if Path(path).name == logging_setup.LOG_FILENAME:
                    raise PermissionError("app.log is busy")
                return original_handler(path, *args, **kwargs)

            try:
                with mock.patch.object(
                    logging_setup.logging,
                    "FileHandler",
                    side_effect=file_handler,
                ), mock.patch.object(logging_setup.os, "getpid", return_value=4242):
                    logger = logging_setup.get_logger(root)

                self.assertTrue(logging_setup.log_file_path(root).parent.is_dir())
                self.assertFalse(logging_setup.log_file_path(root).exists())
                self.assertEqual(len(logger.handlers), 1)
                self.assertEqual(
                    Path(logger.handlers[0].baseFilename).name,
                    "app-4242.log",
                )
                location = logging_setup.describe_log_location(root)
                self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)
                self.assertEqual(location.file_path.name, "app-4242.log")
            finally:
                # Windows will not delete the temporary directory while the
                # FileHandler still owns its file handle; test cleanup must
                # happen before TemporaryDirectory.__exit__ runs.
                for handler in list(self._logger.handlers):
                    handler.close()
                    self._logger.removeHandler(handler)
                logging_setup._configured = False

    def test_unusable_canonical_directory_uses_secondary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            original_handler = logging_setup.logging.FileHandler

            def file_handler(path, *args, **kwargs):
                if Path(path).parent == logging_setup.log_dir(root):
                    raise PermissionError("canonical log directory is blocked")
                return original_handler(path, *args, **kwargs)

            try:
                with mock.patch.object(
                    logging_setup.logging,
                    "FileHandler",
                    side_effect=file_handler,
                ), mock.patch.object(logging_setup.os, "getpid", return_value=4242):
                    logger = logging_setup.get_logger(root)

                self.assertEqual(
                    Path(logger.handlers[0].baseFilename),
                    logging_setup.secondary_log_file_path(root, process_id=4242),
                )
                location = logging_setup.describe_log_location(root)
                self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)
                self.assertEqual(
                    location.directory,
                    logging_setup.secondary_log_dir(root),
                )
            finally:
                for handler in list(self._logger.handlers):
                    handler.close()
                    self._logger.removeHandler(handler)
                logging_setup._configured = False

    def test_unusable_config_root_uses_emergency_temp_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            temp_root = Path(tmp) / "temp"
            original_handler = logging_setup.logging.FileHandler

            def file_handler(path, *args, **kwargs):
                if root in Path(path).parents:
                    raise PermissionError("config root is blocked")
                return original_handler(path, *args, **kwargs)

            try:
                with mock.patch.object(
                    logging_setup.logging,
                    "FileHandler",
                    side_effect=file_handler,
                ), mock.patch.object(logging_setup.os, "getpid", return_value=4242):
                    logger = logging_setup.get_logger(
                        root,
                        _temp_directory=temp_root,
                    )

                emergency_file = logging_setup.emergency_log_file_path(
                    root,
                    process_id=4242,
                    _temp_directory=temp_root,
                )
                self.assertEqual(Path(logger.handlers[0].baseFilename), emergency_file)
                location = logging_setup.describe_log_location(
                    root,
                    _temp_directory=temp_root,
                )
                self.assertEqual(location.status, logging_setup.LogLocationStatus.READY)
                self.assertEqual(location.directory, emergency_file.parent)
                self.assertEqual(location.file_path, emergency_file)
            finally:
                for handler in list(self._logger.handlers):
                    handler.close()
                    self._logger.removeHandler(handler)
                logging_setup._configured = False

    def test_every_destination_failing_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            temp_root = Path(tmp) / "temp"

            with mock.patch.object(
                logging_setup.logging,
                "FileHandler",
                side_effect=PermissionError("all destinations blocked"),
            ), mock.patch.object(logging_setup.os, "getpid", return_value=4242):
                with self.assertRaises(logging_setup.LogInitializationError) as raised:
                    logging_setup.get_logger(root, _temp_directory=temp_root)

            self.assertEqual(
                raised.exception.attempted_directories,
                (
                    logging_setup.log_dir(root),
                    logging_setup.secondary_log_dir(root),
                    logging_setup.emergency_log_dir(
                        root,
                        _temp_directory=temp_root,
                    ),
                ),
            )
            self.assertFalse(logging_setup._configured)


class SupportBundleTests(unittest.TestCase):
    def test_export_packs_recent_logs_without_creating_audio(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            (root / "logs").mkdir(parents=True)
            (root / "logs" / "app.log").write_text("ready\n", encoding="utf-8")
            (root / "bridge-status.json").write_text("{}\n", encoding="utf-8")
            rust = Path(tmp) / "rust"
            rust.mkdir()
            (rust / "events-1.jsonl").write_text(
                '{"type":"daemon_started","error":"地址 AABBCCDDEEFF"}\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"REMO_LOG_DIR": str(rust)}):
                bundle = logging_setup.export_support_bundle(root)
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                readme = archive.read("README.txt").decode("utf-8")
                journal = archive.read("rust/events-1.jsonl").decode("utf-8")
            self.assertIn("README.txt", names)
            self.assertIn("app.log", names)
            self.assertIn("bridge-status.json", names)
            self.assertIn("rust/events-1.jsonl", names)
            self.assertNotIn("AABBCCDDEEFF", journal)
            self.assertIn("[redacted]", journal)
            self.assertNotIn("wav", readme.lower())


if __name__ == "__main__":
    unittest.main()
