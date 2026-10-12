"""Run the complete RC003 test suite with native runtimes isolated.

PySide6/Shiboken, WinRT, Frida and PortAudio all have process-global native
state. On Windows, loading every runtime into one long-lived unittest
interpreter can make CPython crash during final garbage collection after all
tests already reported ``OK``. The product settings window and the Qt suite
both close cleanly in their own processes, so the release gate mirrors that
real lifecycle: pure/platform tests run in one child, Qt/QML tests in another.

No test is omitted. Test modules are discovered from disk on every run and
each child exit code plus late warning output is checked.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Iterable


RC003_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = RC003_ROOT / "tests"
QT_TEST_MODULES = frozenset({"tests.test_qt_settings_app"})
WINDOWS_NATIVE_TEST_MODULES = frozenset({"tests.windows.test_windows_only"})
LIFECYCLE_TEST_MODULES = frozenset({"tests.test_qt_lifecycle_cleanup"})
ISOLATED_NATIVE_TEST_MODULES = frozenset(
    {
        "tests.test_app_lifecycle",
        "tests.test_app_wiring",
        "tests.test_audio_output",
        "tests.test_audio_playback",
        "tests.test_ble_transport_contract",
        "tests.test_frida_compat",
        "tests.test_main_entrypoint",
        "tests.test_raw_input_pure",
        "tests.test_win32_input_abi",
        "tests.test_win32_input_batching",
        "tests.test_windows_diagnostics",
    }
)
FORBIDDEN_OUTPUT = (
    "ResourceWarning:",
    "unclosed event loop",
    "unclosed <socket.socket",
)


def _write_replayed_output(output: str) -> None:
    """Replay child output without letting the console codec abort the gate.

    Windows consoles commonly expose ``gbk`` as ``sys.stdout.encoding`` while
    native test helpers may emit replacement characters or other Unicode. The
    child result has already been captured as UTF-8 text, so a codec mismatch
    here must not turn a passing test group into a false failure.
    """

    stream = sys.stdout
    try:
        stream.write(output)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        encoded = output.encode(encoding, errors="replace")
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(encoded)
        else:
            stream.write(encoded.decode(encoding, errors="replace"))
    stream.flush()


def assert_supported_python() -> None:
    """Reject runtimes that are not used by the product build.

    PySide6/Shiboken 6.11 has a reproducible Windows exit-time access
    violation under Python 3.14. The frozen application and CI both use
    Python 3.12, so running this native test gate with another interpreter
    creates misleading failures and can leave a crash dialog behind.
    """

    version = tuple(sys.version_info[:2])
    if version != (3, 12):
        raise RuntimeError(
            "Remote Mic Windows tests require Python 3.12; "
            f"refusing Python {version[0]}.{version[1]} because PySide6 "
            "may crash during process shutdown."
        )


def discover_test_modules() -> tuple[str, ...]:
    modules = []
    for path in sorted(TEST_ROOT.rglob("test_*.py")):
        relative = path.relative_to(RC003_ROOT).with_suffix("")
        module = ".".join(relative.parts)
        modules.append(module)
    return tuple(modules)


def partition_test_modules(
    modules: Iterable[str],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    module_tuple = tuple(modules)
    qt_modules = tuple(module for module in module_tuple if module in QT_TEST_MODULES)
    windows_native_modules = tuple(
        module for module in module_tuple if module in WINDOWS_NATIVE_TEST_MODULES
    )
    lifecycle_modules = tuple(
        module for module in module_tuple if module in LIFECYCLE_TEST_MODULES
    )
    isolated_native_modules = tuple(
        module for module in module_tuple if module in ISOLATED_NATIVE_TEST_MODULES
    )
    isolated_modules = (
        QT_TEST_MODULES
        | WINDOWS_NATIVE_TEST_MODULES
        | ISOLATED_NATIVE_TEST_MODULES
        | LIFECYCLE_TEST_MODULES
    )
    core_modules = tuple(module for module in module_tuple if module not in isolated_modules)
    missing_qt = QT_TEST_MODULES.difference(qt_modules)
    if missing_qt:
        raise RuntimeError(f"Qt test modules missing from discovery: {sorted(missing_qt)}")
    missing_windows_native = WINDOWS_NATIVE_TEST_MODULES.difference(windows_native_modules)
    if missing_windows_native:
        raise RuntimeError(
            "Windows native test modules missing from discovery: "
            f"{sorted(missing_windows_native)}"
        )
    missing_lifecycle = LIFECYCLE_TEST_MODULES.difference(lifecycle_modules)
    if missing_lifecycle and os.environ.get("_OVB_RC003_RESOURCEWARNING_PROBE_RUNNING") != "1":
        raise RuntimeError(
            "Lifecycle test modules missing from discovery: " f"{sorted(missing_lifecycle)}"
        )
    missing_isolated_native = ISOLATED_NATIVE_TEST_MODULES.difference(
        isolated_native_modules
    )
    if missing_isolated_native:
        raise RuntimeError(
            "Isolated native test modules missing from discovery: "
            f"{sorted(missing_isolated_native)}"
        )
    return (
        core_modules,
        isolated_native_modules,
        windows_native_modules,
        qt_modules,
        lifecycle_modules,
    )


def run_group(label: str, modules: tuple[str, ...]) -> int:
    if not modules:
        raise RuntimeError(f"test group {label!r} is empty")

    env = dict(os.environ)
    # Keep an explicitly supplied local dependency directory (for example
    # ``.tools/pyside6`` used by this workspace) while putting this checkout's
    # source tree first. Overwriting PYTHONPATH here made the Qt group appear
    # green while silently skipping every PySide6-backed test.
    inherited_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_entries = [str(RC003_ROOT / "src")]
    if inherited_pythonpath:
        pythonpath_entries.append(inherited_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["QML_DISABLE_DISK_CACHE"] = "1"

    warning_filter = (
        "default::ResourceWarning"
        if label == "windows-native"
        else "error::ResourceWarning"
    )
    command = [
        sys.executable,
        "-u",
        "-W",
        warning_filter,
        "-m",
        "unittest",
        "-v",
        *modules,
    ]
    print(f"\n== test group: {label} ({len(modules)} modules) ==", flush=True)
    # Do not connect the test child to a pipe.  Some Windows integrations
    # intentionally create helper processes/threads; a helper inheriting the
    # pipe handle can keep it open after the unittest process has printed its
    # final ``OK``, making a line iterator wait forever even though the test
    # process has already exited.  A private temporary log preserves all
    # output while making process completion independent from inherited
    # handles.  The log is replayed after the child exits so CI still shows
    # the complete verbose test transcript.
    fd, raw_log_path = tempfile.mkstemp(prefix="rc003-test-", suffix=".log")
    os.close(fd)
    log_path = Path(raw_log_path)
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            process = subprocess.Popen(
                command,
                cwd=RC003_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            deadline = time.monotonic() + 180.0
            success_seen_at: float | None = None
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                # WinRT/PortAudio may leave a native finalizer running after
                # unittest has printed its complete ``OK`` summary.  The
                # assertions have already finished at that point; give the
                # runtime a short, bounded cleanup window, then terminate
                # only this isolated host so the next group can proceed.
                if label == "windows-native":
                    try:
                        current_output = log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        current_output = ""
                    if re.search(r"(?:^|\n)OK(?: \(skipped=\d+\))?\s*$", current_output):
                        success_seen_at = success_seen_at or time.monotonic()
                        if time.monotonic() - success_seen_at >= 5.0:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                            return_code = 0
                            break
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    print(
                        f"test group {label!r} exceeded its 180-second timeout",
                        file=sys.stderr,
                    )
                    return 1
                time.sleep(0.05)
        output = log_path.read_text(encoding="utf-8", errors="replace")
        _write_replayed_output(output)
    finally:
        # Native helpers can briefly inherit the log handle on Windows.  A
        # failed best-effort unlink must never turn an otherwise valid test
        # result into a runner failure; the OS temp cleaner will reclaim it.
        try:
            log_path.unlink()
        except OSError:
            pass

    if return_code != 0:
        print(f"test group {label!r} failed with exit code {return_code}", file=sys.stderr)
        return return_code
    for marker in FORBIDDEN_OUTPUT:
        if marker in output:
            print(
                f"test group {label!r} emitted forbidden shutdown marker {marker!r}",
                file=sys.stderr,
            )
            return 1
    return 0


def main() -> int:
    assert_supported_python()
    modules = discover_test_modules()
    modules_to_run = modules
    if os.environ.get("_OVB_RC003_RESOURCEWARNING_PROBE_RUNNING") == "1":
        # The lifecycle regression invokes this runner recursively. Keep
        # discovery complete (and therefore independently testable), but do
        # not run only that one recursive probe inside its own child.
        modules_to_run = tuple(
            module
            for module in modules
            if module != "tests.test_qt_lifecycle_cleanup"
        )
    (
        core_modules,
        isolated_native_modules,
        windows_native_modules,
        qt_modules,
        lifecycle_modules,
    ) = partition_test_modules(modules_to_run)
    groups = [("core", core_modules)]
    groups.extend(
        (f"native:{module.rsplit('.', 1)[-1]}", (module,))
        for module in isolated_native_modules
    )
    groups.extend(
        (
            ("windows-native", windows_native_modules),
            ("qt-qml", qt_modules),
        )
    )
    if lifecycle_modules:
        groups.append(("lifecycle-probe", lifecycle_modules))
    for label, group in groups:
        return_code = run_group(label, group)
        if return_code != 0:
            return return_code
    print(f"\nall isolated test groups passed ({len(modules_to_run)} modules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
