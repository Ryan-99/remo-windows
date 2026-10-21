from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "src" / "ovb_rc003" / "qml"
SPEC = (ROOT / "build" / "RemoteMicRC003.spec").read_text(encoding="utf-8")
MAIN = (QML / "main.qml").read_text(encoding="utf-8")
SYSTEM = (QML / "SystemPage.qml").read_text(encoding="utf-8")


def test_design_pages_are_real_qml_files_and_collected_by_spec():
    for filename in ("StatisticsPage.qml", "AboutPage.qml", "SystemPage.qml"):
        assert (QML / filename).is_file()
    assert "QML_SOURCE_DIR" in SPEC
    assert 'datas.append((str(QML_SOURCE_DIR), "ovb_rc003_qml"))' in SPEC


def test_main_has_five_navigation_entries_and_five_stack_pages():
    assert MAIN.count("onClicked: tabBar.currentIndex =") == 4
    assert MAIN.count("Layout.preferredHeight: 56") == 4
    assert "StatisticsPage" not in MAIN
    for name in ("ConnectionPage", "SystemPage", "AboutPage"):
        assert f"{name} {{ tokens: window.tokens }}" in MAIN
    assert "ButtonsPage { id: buttonsPage; tokens: window.tokens }" in MAIN


def test_main_uses_a_real_integrated_title_bar():
    assert "Qt.Window | Qt.FramelessWindowHint" in MAIN
    assert 'objectName: "customTitleBar"' in MAIN
    assert "window.startSystemMove()" in MAIN
    assert 'objectName: "minimizeWindowButton"' in MAIN
    assert 'objectName: "maximizeWindowButton"' in MAIN
    assert 'objectName: "closeWindowButton"' in MAIN


def test_connection_page_does_not_launch_the_legacy_bridge():
    page = (QML / "ConnectionPage.qml").read_text(encoding="utf-8")
    assert "保存并启动桥接" not in page
    assert "停止桥接" not in page
    assert "SettingsController.saveAndLaunch()" not in page
    assert 'objectName: "bridgeRuntimeStatusLabel"' in page
    assert 'objectName: "openLogButton"' in page


def test_connection_page_uses_runtime_bridge_status_not_launch_result_only():
    page = (QML / "ConnectionPage.qml").read_text(encoding="utf-8")
    controller = (QML.parent / "qt_settings_app.py").read_text(encoding="utf-8")
    # Polling is owned by the root window so physical presses keep flowing
    # while the user is on mapping/statistics/diagnostics pages.
    assert 'objectName: "bridgeStatusPollTimer"' in MAIN
    assert 'objectName: "bridgeRuntimeStatusLabel"' in page
    assert "SettingsController.bridgeRuntimeStatusText" in page
    assert "def refreshBridgeStatus(self)" in controller
    assert "bridgeRuntimeState" in controller


def test_physical_button_animation_does_not_steal_other_pages_or_run_actions():
    # Mapping/action execution belongs to the bridge; QML receives the event
    # only to animate and expand a card when the mapping page is visible.
    assert "SettingsController.setPhysicalButtonFeedbackActive(tabBar.currentIndex === 1)" in MAIN
    assert "tabBar.currentIndex = 1\n                buttonsPage.revealButton" not in MAIN
    controller = (QML.parent / "qt_settings_app.py").read_text(encoding="utf-8")
    assert "def setPhysicalButtonFeedbackActive" in controller
    assert "def deliverPhysicalButtonFeedback" in controller
    assert "if not self._physical_button_feedback_active or not button_id" in controller


def test_connection_page_defers_input_source_selection_and_esp32_controls():
    page = (QML / "ConnectionPage.qml").read_text(encoding="utf-8")
    assert "按键输入源" not in page
    assert "inputSourceCombo" not in page
    assert "esp32PortField" not in page
    controller = (QML.parent / "qt_settings_app.py").read_text(encoding="utf-8")
    assert "def inputSourceOptions" not in controller
    assert "def setInputSource" not in controller
    # The bridge continues to expose the supported-device voice endpoint.
    assert 'text: qsTr("语音输出")' in page


def test_permissions_page_keeps_complex_diagnostics_as_a_secondary_view():
    assert 'text: qsTr("权限")' in SYSTEM
    assert 'text: qsTr("日志与诊断")' in SYSTEM
    assert SYSTEM.index('objectName: "diagnosticsTabButton"') < SYSTEM.index('objectName: "permissionsTabButton"')
    assert SYSTEM.index("DiagnosticsPage { tokens: root.tokens }") < SYSTEM.index("PermissionsPage { tokens: root.tokens }")


def test_statistics_page_uses_controller_data_not_hardcoded_demo_values():
    page = (QML / "ConnectionPage.qml").read_text(encoding="utf-8")
    assert 'objectName: "usageSummaryCard"' in page
    assert "SettingsController.statisticsButtonTotal" in page
    assert "SettingsController.statisticsVoiceSeconds" in page
    assert "SettingsController.statisticsButtonRows" in page
    assert "SettingsController.refreshStatistics()" in page
    assert "style: 'height" not in page


def test_about_page_uses_the_remo_logo_asset():
    page = (QML / "AboutPage.qml").read_text(encoding="utf-8")
    assert "SettingsController.appLogoSource" in page
    assert "Remo" in page
    assert 'objectName: "aboutInfoCard"' in page


def test_spec_collects_the_remo_logo_asset():
    assert 'REMO_LOGO = REPO_ROOT / "Resources" / "Remo-logo.png"' in SPEC
    assert 'datas.append((str(REMO_LOGO), "Resources"))' in SPEC


def test_buttons_page_uses_the_approved_balanced_native_layout():
    page = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    assert 'objectName: "visualMapping"' in page
    assert '"summaryButton_" + buttonId' in page
    assert '[\n                            "back", "menu", "volume_up", "up", "left", "home"' in page
    assert '[\n                            "power", "volume_down", "tv", "down", "right", "ok"' in page
    assert 'text: qsTr("语音输入")' in page
    assert page.index('id: leftSummary') < page.index('id: photoFrame') < page.index('id: rightSummary')


def test_buttons_page_keeps_all_real_mapping_controls_after_redesign():
    page = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    for hook in (
        'objectName: "photoImage"',
        'objectName: "photoHotspot_" + buttonId',
        'objectName: "mappingList"',
        'objectName: "saveMappingButton"',
        'objectName: "detectRealKeyButton"',
        'objectName: "shortcutRecorderDialog"',
        'objectName: "advancedMappingToggle"',
    ):
        assert hook in page
    assert "setActionTextAt" in page
    assert '"double_click"' in page
    assert '"long_press"' not in page
    assert "SettingsController.startHotkeyCapture()" in page
    assert 'objectName: "detailActionCombo_" + detailEditor.buttonId' in page
    assert 'objectName: "doubleActionCombo_" + detailEditor.buttonId' in page
    assert "ScrollView {" not in page


def test_buttons_page_exposes_real_voice_input_presets_and_shortcut_recording():
    page = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    controller = (QML.parent / "qt_settings_app.py").read_text(encoding="utf-8")
    assert 'objectName: "voicePresetCombo"' in page
    assert 'objectName: "recordVoiceShortcutButton"' in page
    assert "SettingsController.applyVoicePreset" in page
    assert "def applyVoicePreset" in controller
    for preset in ("wechat", "doubao", "qianwen_ralt", "bage"):
        assert f'"{preset}"' in controller


def load_tests(_loader, _tests, _pattern):
    """Make these lightweight contract functions part of the unittest gate."""
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
