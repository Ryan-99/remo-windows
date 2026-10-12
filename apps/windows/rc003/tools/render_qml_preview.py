"""Render a selected Remote Mic QML page to a PNG for visual QA."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from ovb_rc003 import config, qt_settings_app  # noqa: E402


def _qa_statistics_snapshot() -> dict:
    """Return deterministic preview-only data without touching user storage."""

    return {
        "period": "week",
        "button_total": 504,
        "voice_total_seconds": 1522.0,
        "by_button": {"ok": 132, "up": 96, "down": 81, "home": 54},
        "voice_sessions": 8,
        "longest_seconds": [85.0, 38.0, 38.0],
        "longest_sessions": [
            {"seconds": 85.0, "started_at": "2026-08-15T02:22:00+08:00"},
            {"seconds": 38.0, "started_at": "2026-08-11T00:52:00+08:00"},
            {"seconds": 38.0, "started_at": "2026-08-15T02:21:00+08:00"},
        ],
        "daily_activity": [
            {"label": "08/09", "button_count": 12, "voice_seconds": 42.0},
            {"label": "08/10", "button_count": 68, "voice_seconds": 490.0},
            {"label": "08/11", "button_count": 94, "voice_seconds": 318.0},
            {"label": "08/12", "button_count": 77, "voice_seconds": 441.0},
            {"label": "08/13", "button_count": 145, "voice_seconds": 862.0},
            {"label": "08/14", "button_count": 83, "voice_seconds": 322.0},
            {"label": "08/15", "button_count": 25, "voice_seconds": 71.0},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--page", type=int, default=1, choices=range(5))
    parser.add_argument(
        "--seed-statistics",
        action="store_true",
        help="render deterministic QA-only statistics without writing user data",
    )
    parser.add_argument(
        "--system-subpage",
        choices=("permissions", "diagnostics"),
        default="permissions",
        help="select the permissions tab's secondary view",
    )
    parser.add_argument(
        "--expand-button",
        default="",
        help="preview a selected mapping card in its expanded state",
    )
    parser.add_argument(
        "--physical-button",
        default="",
        help="emit a simulated bridge physical-button signal before capture",
    )
    args = parser.parse_args()

    classes = qt_settings_app._load_qt_classes()
    model = classes["ButtonMappingModel"]()
    controller = classes["SettingsController"](model)
    if args.seed_statistics:
        controller._statistics_period = "week"  # preview-only state; never persisted
        controller._statistics_snapshot = _qa_statistics_snapshot()
    diagnostics = classes["DiagnosticsController"](controller, config.config_root())

    QQuickStyle.setStyle("FluentWinUI3")
    app = QGuiApplication.instance() or QGuiApplication([])
    # The offscreen QPA plugin does not enumerate the Windows font registry.
    # Load the OS-owned font only for this QA renderer; it is never bundled.
    windows_ui_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if windows_ui_font.is_file():
        font_id = QFontDatabase.addApplicationFont(str(windows_ui_font))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont("Microsoft YaHei UI" if "Microsoft YaHei UI" in families else families[0]))
    qmlRegisterSingletonInstance(
        type(controller), "OvbRc003Settings", 1, 0, "SettingsController", controller
    )
    qmlRegisterSingletonInstance(
        type(model), "OvbRc003Settings", 1, 0, "ButtonMappingModel", model
    )
    qmlRegisterSingletonInstance(
        type(diagnostics),
        "OvbRc003Settings",
        1,
        0,
        "DiagnosticsController",
        diagnostics,
    )

    engine = QQmlApplicationEngine()
    qml_dir = qt_settings_app._qml_directory()
    engine.addImportPath(str(qml_dir))
    engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
    roots = engine.rootObjects()
    if len(roots) != 1:
        raise RuntimeError("main.qml did not instantiate exactly one root object")

    window = roots[0]
    tab_bar = window.findChild(QObject, "tabBar")
    if tab_bar is None:
        raise RuntimeError("tabBar was not found")
    tab_bar.setProperty("currentIndex", args.page)
    if args.page == 3 and args.system_subpage == "diagnostics":
        system_tabs = window.findChild(QObject, "systemTabs")
        if system_tabs is None:
            raise RuntimeError("systemTabs was not found")
        system_tabs.setProperty("currentIndex", 1)
    window.show()
    for _ in range(30):
        app.processEvents()

    if args.expand_button and args.page == 1:
        buttons_page = window.findChild(QObject, "buttonsPageRoot")
        if buttons_page is None:
            raise RuntimeError("buttonsPageRoot was not found")
        # Exercise the same public interaction path as a real card click.
        # Merely assigning the backing property can leave Layout bindings in
        # an unpolished state in an offscreen frame and did not prove that the
        # delegate's click handler works.
        buttons_page.setProperty("inlineEditorButtonId", "")
        buttons_page.setProperty("activationButtonId", args.expand_button)
        buttons_page.setProperty("inlineEditorButtonId", args.expand_button)
        QTest.qWait(360)
        for _ in range(45):
            app.processEvents()
        def _find_visual(item, object_name):
            if item is None:
                return None
            if item.objectName() == object_name:
                return item
            for child in item.childItems():
                found = _find_visual(child, object_name)
                if found is not None:
                    return found
            return None
        mic_card = _find_visual(window.contentItem(), "summaryButton_mic")
        if mic_card is not None:
            print(
                "expand-debug",
                {
                    "inline": buttons_page.property("inlineEditorButtonId"),
                    "height": mic_card.property("height"),
                    "preferredHeight": mic_card.property("Layout.preferredHeight"),
                    "minimumHeight": mic_card.property("Layout.minimumHeight"),
                    "maximumHeight": mic_card.property("Layout.maximumHeight"),
                    "implicitHeight": mic_card.property("implicitHeight"),
                    "parentHeight": mic_card.parentItem().property("height") if mic_card.parentItem() else None,
                    "visible": mic_card.property("visible"),
                    "buttonId": mic_card.property("buttonId"),
                    "isMic": mic_card.property("isMic"),
                    "y": mic_card.property("y"),
                },
            )
    if args.physical_button:
        # This exercises the same Qt signal that qt_settings_app emits after
        # consuming bridge-status.json.  It deliberately does not synthesize
        # keyboard input or call the backend.
        controller.setPhysicalButtonFeedbackActive(args.page == 1)
        controller.physicalButtonActivated.emit(args.physical_button)
        QTest.qWait(360)
        for _ in range(45):
            app.processEvents()
        buttons_page = window.findChild(QObject, "buttonsPageRoot")
        if buttons_page is not None:
            active = args.page == 1
            expected = args.physical_button if active else ""
            actual = buttons_page.property("inlineEditorButtonId")
            if actual != expected:
                raise RuntimeError(
                    f"physical feedback page gate failed: page={args.page} "
                    f"expected={expected!r} got={actual!r}"
                )
            print(
                "physical-debug",
                {
                    "page": tab_bar.property("currentIndex"),
                    "inline": buttons_page.property("inlineEditorButtonId"),
                    "activation": buttons_page.property("activationButtonId"),
                },
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    image = window.grabWindow()
    if image.isNull() or not image.save(str(args.output)):
        raise RuntimeError(f"failed to save preview to {args.output}")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
