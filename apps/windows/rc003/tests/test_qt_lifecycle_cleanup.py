"""Regression test (XRBM-030 RETRY 1 blocker 3): proves the REAL Windows CI
command this package's test-suite step actually runs produces NO
"ResourceWarning:" output at interpreter shutdown, with PySide6-Essentials
installed.

Red evidence this guards against: before ``qt_settings_app.py`` gained
``_release_qt_classes_cache()`` (an ``atexit``-registered hook that drops
this module's cached dynamically-created ``QObject``/``QAbstractListModel``
subclasses and calls ``gc.collect()`` while the interpreter is still fully
alive), running

    PYTHONPATH=apps/windows/rc003/src QT_QPA_PLATFORM=offscreen \\
      QML_DISABLE_DISK_CACHE=1 python -W default::ResourceWarning \\
      -m unittest discover -s apps/windows/rc003/tests -t apps/windows/rc003 \\
      -p "test_*.py"

ended with exactly::

    gc:0: ResourceWarning: gc: 13 uncollectable objects at shutdown; use gc.set_debug(gc.DEBUG_UNCOLLECTABLE) to list them

- root-caused to ``PySide6.QtCore.Property`` descriptors participating in a
Python-level reference cycle that shiboken's C extension type does not fully
support ``tp_clear`` for (reproduced with a minimal, completely unrelated
repro: a single trivial ``Property``-having ``QObject`` subclass, module-
level, no closures, no caching). ``.github/workflows/windows-rc003-ci.yml``'s
test-suite step explicitly greps its captured output for the literal
substring ``"ResourceWarning:"`` and fails the whole job if it appears
anywhere - this substring match, not the process exit code, is what a
finalizer-time warning like this actually threatens (an exception raised
inside a finalizer at interpreter shutdown is unraisable and never changes
an already-computed exit code - see tests/test_resourcewarning_gate_replay.py
and tests/test_build_artifacts.py's XRBM-026 tests for that same fact
established for a different warning shape).

This test does NOT weaken, suppress, or edit that CI gate - it proves the
isolated runner used by the gate is clean, by literally invoking it (as a
subprocess, so this test's own process is never placed at risk of recursion;
the nested runner marks this probe with the guard below).
"""

import os
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

_RC003_ROOT = Path(__file__).resolve().parents[1]

# Sentinel env var: guards against the nested runner re-discovering and
# re-running THIS test,
# which would otherwise recurse (a subprocess spawning a subprocess
# spawning a subprocess...) forever.
_RECURSION_GUARD_ENV_VAR = "_OVB_RC003_RESOURCEWARNING_PROBE_RUNNING"


def _has_pyside6() -> bool:
    # Only inspect the import spec in the parent process.  Importing QtCore
    # here creates a native Qt/shiboken runtime that must be torn down during
    # this test process' own interpreter shutdown; on Python 3.14 that can
    # keep the parent alive after the child suite has already completed.  The
    # subprocess below still performs the real import and remains the source
    # of truth for the lifecycle assertion.
    # Looking up the top-level package avoids importing PySide6 itself (a
    # dotted ``find_spec`` would import the parent package as a side effect).
    return importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(
    _has_pyside6(), "PySide6-Essentials not installed - nothing Qt-related to prove clean here"
)
@unittest.skipIf(
    os.environ.get(_RECURSION_GUARD_ENV_VAR) == "1",
    "already running inside this test's own subprocess - see module docstring",
)
class NoResourceWarningAtShutdownTests(unittest.TestCase):
    def test_full_test_suite_command_produces_no_resourcewarning_at_shutdown(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_RC003_ROOT / "src")
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["QML_DISABLE_DISK_CACHE"] = "1"
        env[_RECURSION_GUARD_ENV_VAR] = "1"

        # Invoke the same isolated runner as the build/CI gate. The runner
        # itself applies `-W error::ResourceWarning` in each child and scans
        # the complete child output for late warning markers.
        result = subprocess.run(
            [
                sys.executable,
                "-u", str(_RC003_ROOT / "tools" / "run_test_suite.py"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

        self.assertEqual(
            result.returncode,
            0,
            f"full test suite subprocess failed:\nSTDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}",
        )
        self.assertNotIn(
            "ResourceWarning:",
            result.stdout,
            "the real CI command's stdout contains a forbidden "
            "'ResourceWarning:' substring",
        )
        self.assertNotIn(
            "ResourceWarning:",
            result.stderr,
            "the real CI command's stderr contains a forbidden "
            "'ResourceWarning:' substring",
        )


if __name__ == "__main__":
    unittest.main()
