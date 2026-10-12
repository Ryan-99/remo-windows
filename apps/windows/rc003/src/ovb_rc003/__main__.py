"""``python -m ovb_rc003`` (or the packaged ``RemoteMicRC003.exe``,
built from the standalone ``src/launcher.py`` entry point - see XRBM-021):

- (no args)     open the settings window - the DEFAULT double-click
                behavior, so a user who runs the exe always sees a window.
                Never touches BLE/HID/audio or the single-instance guard.
- ``--settings``  open the settings window (explicit form of the default)
- ``--bridge``  run the bridge - guarded by a per-session Windows named-
                mutex (single_instance.py) so a second concurrent launch
                never starts BLE/HID/audio: it shows a visible notice and
                exits with a deterministic nonzero code instead of ever
                calling ``app.main()``. This guard FAILS CLOSED (XRBM-021
                review round 1 P1 #1): if the mutex API itself could not be
                used to prove single ownership - not just a confirmed
                duplicate - the bridge does not start either. A caller
                that cannot prove it is the only owner of BLE/HID/audio
                resources must never gamble on being one anyway.
- ``--dry-run``   import every first-party module and exit 0, touching no
                  GUI, BLE, Raw Input, or audio device - the safe smoke
                  check build-candidate.ps1 and
                  .github/workflows/windows-rc003-ci.yml run against a
                  freshly built artifact (XRBM-014 review RETRY P2 #3: a
                  build that produces an executable which cannot even be
                  launched is not caught by the PyInstaller build step
                  alone).
- ``--qt-smoke``  HIDDEN release gate that imports the native Qt modules
                  used by the settings window. This catches a broken Qt
                  DLL closure without opening a GUI.
- ``--diagnose-ble-candidates <result-path>``  HIDDEN, undocumented in
                  ``--help`` on purpose (XRBM-035 RETRY 1): the settings
                  window's "检查与修复" page's BLE candidate check
                  re-invokes this same entry point (source:
                  ``sys.executable -m ovb_rc003 --diagnose-ble-candidates
                  <result-path>``; frozen build: the packaged .exe
                  re-invoked with just the flag + path - see
                  ``windows_diagnostics.build_ble_diagnostics_subprocess_
                  command()``) as a disposable CHILD PROCESS purely so the
                  parent can forcibly terminate/kill it with a real,
                  OS-confirmed hard bound if the native WinRT call it makes
                  never returns - something no in-process asyncio
                  cancellation can guarantee (see
                  ``windows_diagnostics.py``'s "-- BLE candidate --"
                  section for the full story). ``<result-path>`` is where
                  this process writes its ONE, strictly-shaped result JSON
                  file - NEVER stdout, which a real PyInstaller
                  ``console=False`` build sets to ``None`` (see that same
                  module section for the citation) - and a missing/empty
                  path here fails this branch CLOSED (a nonzero exit,
                  before ever attempting discovery, never a fallback
                  location and never falling through to running the
                  bridge). Never launched by a real end user directly; not
                  part of this program's public CLI surface.
- ``--help``/``-h``  print this usage and exit 0

``--settings``, ``--bridge``, ``--dry-run``,
``--diagnose-ble-candidates`` and ``--help``/``-h`` are all checked and
dispatched BEFORE the bridge branch below is ever reached, so none of them
touch the single-instance mutex at all (XRBM-021 changed threat model: the
guard applies only to the ``--bridge`` mode).
"""

from __future__ import annotations

import sys

from . import __version__


SETTINGS_STARTUP_FAILED_EXIT_CODE = 6
UNSUPPORTED_PYTHON_EXIT_CODE = 7
_FROZEN_SETTINGS_STARTUP_ERROR = "应用运行文件损坏或不兼容，请重新安装完整版本。"
_UNSUPPORTED_PYTHON_MESSAGE = (
    "当前 Python 运行时不受支持。Remo Windows 需要 Python 3.12；"
    "请使用完整安装包，或使用项目的 .venv-build-312 环境启动。"
)


def _require_supported_python() -> None:
    """Stop source launches before PySide6/native DLLs can load on 3.14."""

    if getattr(sys, "frozen", False):
        return
    if tuple(sys.version_info[:2]) != (3, 12):
        print(_UNSUPPORTED_PYTHON_MESSAGE, file=sys.stderr)
        from . import single_instance

        single_instance.show_bridge_startup_blocked_notice(
            _UNSUPPORTED_PYTHON_MESSAGE,
            title="Remo",
        )
        raise SystemExit(UNSUPPORTED_PYTHON_EXIT_CODE)


def _print_help() -> None:
    print(f"Remo - RC003 Windows client (source/build candidate) {__version__}")
    print("Not yet real-device verified - see this package's README.md 'Known gaps' section.")
    print()
    print("Usage:")
    print("  python -m ovb_rc003               open the settings window (default)")
    print("  python -m ovb_rc003 --settings    open the settings window")
    print("  python -m ovb_rc003 --bridge      run the bridge")
    print("  python -m ovb_rc003 --dry-run     import every module and exit 0 (CI smoke check)")
    print("  python -m ovb_rc003 --help        show this message and exit 0")


def _dry_run() -> int:
    """Imports every first-party module this package ships, without
    constructing a Qt window, opening a BLE connection, starting Raw Input,
    or touching an audio device. Importing ``settings_ui``/``qt_settings_app``
    never requires PySide6-Essentials to be installed (XRBM-030): both defer
    any Qt import to inside a function body, only reached when the settings
    window is actually opened - see qt_settings_app.py's module docstring.
    """

    from . import (  # noqa: F401
        app,
        atvv_protocol,
        atvv_session,
        audio_output,
        audio_playback,
        ble_transport_winrt,
        bridge_control,
        bridge_launcher,
        config,
        connection_supervisor,
        device_catalog,
        device_profile,
        frida_compat,
        hid_identity,
        hotkey,
        identity,
        key_mapping,
        logging_setup,
        qt_settings_app,
        raw_input_windows,
        remote_layout,
        resources,
        settings_ui,
        updater,
        shell_targets,
        single_instance,
        voice_controller,
        win32_input,
        win32_keys,
    )

    print("dry-run: all ovb_rc003 modules imported successfully")
    return 0


def _qt_smoke() -> int:
    """Import the exact Qt modules required by the settings window.

    ``--dry-run`` deliberately keeps Qt optional and therefore cannot catch
    a frozen package whose Qt DLL closure is broken.  The release pipeline
    invokes this hidden smoke mode against the built executable so a
    ``QtCore.pyd``/``Qt6Core.dll``/ICU mismatch fails the build before the
    installer or portable archive is produced.
    """

    from PySide6 import QtCore, QtGui, QtQml, QtQuick, QtQuickControls2  # noqa: F401

    # Access one exported Qt symbol, rather than treating a namespace import
    # alone as sufficient proof that the native extension initialized.
    QtCore.qVersion()
    return 0


def _run_bridge() -> None:
    """No-argument bridge mode: guarded by the per-session single-instance
    mutex (XRBM-021 In-scope items 2-3). ``app.main()`` is only ever called
    from INSIDE the guard's ``with`` block (first owner) - NEVER from any
    failure branch below. This fails CLOSED (XRBM-021 review round 1 P1
    #1): a confirmed duplicate and an acquisition failure the guard could
    not resolve are both treated as "cannot prove sole ownership", so both
    show a visible notice, exit nonzero, and never start BLE/HID/audio - a
    caller that cannot verify it is safe must not start anyway.

    ``MutexCleanupError`` (raised from the guard's ``__exit__``, i.e. AFTER
    ``app.main()`` has already run to completion or raised) is also caught
    here: the packaged executable is windowed (``console=False``), so an
    unhandled exception's traceback would never be seen by the user at all
    - this still needs a visible, SANITIZED notice (not the raw exception
    text, which is for diagnostics/stderr only) and a deterministic nonzero
    exit rather than silently disappearing.
    """

    _require_supported_python()
    from . import app, config, device_catalog, logging_setup, single_instance

    selected_device_id = device_catalog.normalize_device_id(
        config.load_config(config.config_path()).get("selected_device_profile")
    )
    if selected_device_id == device_catalog.DJI_MIC_2_ID:
        single_instance.show_bridge_startup_blocked_notice(
            "当前设备是 DJI Mic 2。它由 Windows 作为系统录音输入使用，不需要也不会启动 "
            "RC003 BLE/HID/ATVV 桥。请在 Remo 设置中检查录音端点。",
            title="Remo",
        )
        return

    try:
        with single_instance.BridgeInstanceGuard():
            app.main()
    except single_instance.DuplicateInstanceError as exc:
        single_instance.show_bridge_startup_blocked_notice(str(exc))
        raise SystemExit(single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
    except single_instance.SingleInstanceUnavailableError as exc:
        single_instance.show_bridge_startup_blocked_notice(
            "Remo · RC003 could not verify no other instance "
            f"is already running, so it will not start. ({exc})"
        )
        raise SystemExit(single_instance.GUARD_UNAVAILABLE_EXIT_CODE)
    except single_instance.MutexCleanupError as exc:
        # Diagnostic detail (fixed operation names/error codes only, per
        # single_instance.py's "no raw handle" contract - see that
        # exception's own message construction) goes to stderr for anyone
        # who can see it; the user-visible notice stays a fixed, sanitized
        # sentence regardless of the exact underlying failure.
        print(f"single-instance mutex cleanup failed: {exc}", file=sys.stderr)
        single_instance.show_bridge_startup_blocked_notice(
            "Remo · RC003 closed, but could not fully release "
            "its single-instance lock. If it will not start again, check "
            "Task Manager for a lingering process before retrying."
        )
        raise SystemExit(single_instance.CLEANUP_FAILED_EXIT_CODE)
    except logging_setup.LogInitializationError as exc:
        print(f"log initialization failed: {exc}", file=sys.stderr)
        single_instance.show_bridge_startup_blocked_notice(
            "Remo 无法创建运行日志，因此桥接没有启动。请检查本地应用数据目录和临时目录的写入权限后重试。",
            title="Remo",
        )
        raise SystemExit(logging_setup.LOG_INITIALIZATION_FAILED_EXIT_CODE)


def _run_settings() -> None:
    """Open settings and turn known Qt startup failures into a visible error.

    A windowed PyInstaller executable has no useful console. Letting
    ``QtUnavailableError`` escape makes PyInstaller show an implementation
    traceback, while the user only needs to know that the installed runtime
    is incomplete or incompatible. Source runs retain the actionable
    developer message from ``qt_settings_app``.
    """

    _require_supported_python()
    from . import qt_settings_app, settings_ui, single_instance

    try:
        settings_ui.main()
    except qt_settings_app.QtUnavailableError as exc:
        if sys.stderr is not None:
            print(f"settings startup failed: {exc}", file=sys.stderr)
        message = (
            _FROZEN_SETTINGS_STARTUP_ERROR
            if getattr(sys, "frozen", False)
            else str(exc)
        )
        single_instance.show_bridge_startup_blocked_notice(
            message,
            title="Remo",
        )
        raise SystemExit(SETTINGS_STARTUP_FAILED_EXIT_CODE) from exc


def main() -> None:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        _print_help()
        return
    if "--dry-run" in args:
        raise SystemExit(_dry_run())
    if "--qt-smoke" in args:
        raise SystemExit(_qt_smoke())
    if "--diagnose-ble-candidates" in args:
        # XRBM-035 RETRY 1: hidden, undocumented child-process entry point -
        # see this module's own docstring. Always raises SystemExit from
        # this branch (never `return`s, never falls through below), which
        # is itself part of the fail-closed contract: whatever
        # run_ble_diagnostics_subprocess_entrypoint() decides, this process
        # can never end up calling _run_bridge() by accident. A missing
        # result-path argument (index out of range) is passed through as
        # None - that function's own contract is to fail closed on that,
        # not this dispatch site's job to second-guess.
        from . import windows_diagnostics

        flag_index = args.index("--diagnose-ble-candidates")
        result_path = args[flag_index + 1] if flag_index + 1 < len(args) else None
        raise SystemExit(
            windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint(result_path)
        )
    if "--rc003-hid-injector" in args:
        # Hidden entry point used only by the verified Frida Gadget tap. It
        # must never fall through to bridge startup.
        from . import frida_compat

        flag_index = args.index("--rc003-hid-injector")
        raise SystemExit(frida_compat.injector_main(args[flag_index + 1 :]))
    if "--bridge" in args:
        _run_bridge()
        return

    # Default (no arguments) and explicit --settings both open the settings
    # window. A user who double-clicks the packaged exe must see a window,
    # never a headless bridge process with no UI.
    _run_settings()


if __name__ == "__main__":
    main()
