import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    readonly property bool bridgeUp: SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active"

    function formatDuration(seconds) {
        var total = Math.max(0, Math.round(Number(seconds) || 0))
        var minutes = Math.floor(total / 60)
        var remainder = total % 60
        if (minutes <= 0)
            return remainder + "秒"
        if (remainder === 0)
            return minutes + "分"
        return minutes + "分" + remainder + "秒"
    }

    function voiceChordLabel(raw) {
        var names = {
            "lalt": "左 Alt",
            "ralt": "右 Alt",
            "lctrl": "左 Ctrl",
            "rctrl": "右 Ctrl",
            "lshift": "左 Shift",
            "rshift": "右 Shift",
            "lwin": "左 Win",
            "rwin": "右 Win",
            "space": "空格",
            "tab": "Tab",
            "enter": "回车",
            "return": "回车",
            "esc": "Esc",
            "escape": "Esc"
        }
        var text = String(raw || "").trim()
        if (!text)
            return "未设置"
        var parts = text.split("+")
        var labels = []
        for (var index = 0; index < parts.length; index++) {
            var token = parts[index].trim().toLowerCase()
            labels.push(names[token] || parts[index].trim())
        }
        return labels.join(" + ")
    }

    function dailyPeak() {
        var peak = 1
        var rows = SettingsController.statisticsDailyRows
        for (var index = 0; index < rows.length; index++) {
            peak = Math.max(peak, Number(rows[index].button_count) || 0)
        }
        return peak
    }

    Component.onCompleted: SettingsController.refreshStatistics()

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: pageColumn
            width: root.width - 84
            x: 42
            y: 24
            height: Math.max(implicitHeight, root.height - 40)
            spacing: 16

            RowLayout {
                Layout.fillWidth: true
                spacing: 16
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Label { text: qsTr("连接"); color: tokens.textPrimary; font.pixelSize: tokens.fontSizePageTitle; font.bold: true }
                    Label { text: qsTr("把遥控器变成可靠的 Windows 语音入口"); color: tokens.textSecondary; font.pixelSize: tokens.fontSizeBody }
                }
                Rectangle {
                    implicitWidth: statusText.implicitWidth + 26
                    implicitHeight: 32
                    radius: 16
                    color: SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active" ? "#e8faef" : SettingsController.bridgeRuntimeState === "error" ? "#fff0f0" : tokens.fieldBackground
                    Label {
                        id: statusText
                        anchors.centerIn: parent
                        text: (SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active" ? qsTr("● 已连接") : "● " + SettingsController.bridgeRuntimeStatusText)
                        color: SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active" ? "#208f5c" : SettingsController.bridgeRuntimeState === "error" ? tokens.errorColor : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 18

                UiCard {
                    Layout.preferredWidth: 220
                    Layout.fillHeight: true
                    Layout.minimumHeight: 420
                    fillColor: tokens.surface
                    strokeColor: tokens.border
                    cornerRadius: tokens.cornerRadiusLarge
                    ColumnLayout {
                        id: deviceColumn
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 10
                        Label {
                            Layout.fillWidth: true
                            text: qsTr("小米蓝牙遥控器 2 Pro")
                            horizontalAlignment: Text.AlignHCenter
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeBody
                            font.bold: true
                            wrapMode: Text.WordWrap
                        }
                        Rectangle {
                            Layout.alignment: Qt.AlignHCenter
                            implicitWidth: deviceState.implicitWidth + 20
                            implicitHeight: 25
                            radius: 13
                            color: "#e8faef"
                            Label { id: deviceState; anchors.centerIn: parent; text: qsTr("RC003"); color: "#208f5c"; font.pixelSize: 10 }
                        }
                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 180
                            Image {
                                anchors.centerIn: parent
                                height: Math.min(parent.height - 8, 320)
                                width: height * 0.7
                                source: SettingsController.photoAvailable ? SettingsController.photoSource : ""
                                visible: SettingsController.photoAvailable
                                fillMode: Image.PreserveAspectFit
                                smooth: true
                            }
                        }
                        ComboBox {
                            id: deviceCombo
                            objectName: "deviceCombo"
                            Layout.fillWidth: true
                            model: SettingsController.deviceOptions
                            currentIndex: SettingsController.selectedDeviceIndex
                            // Keep the compact card readable while the popup still
                            // exposes the controller's complete device names.
                            displayText: qsTr("RC003 遥控器")
                            onActivated: SettingsController.selectedDeviceIndex = index
                            enabled: SettingsController.deviceCatalogAvailable
                            Accessible.name: qsTr("当前设备")
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            maximumLineCount: 3
                            elide: Text.ElideRight
                            text: SettingsController.deviceCatalogAvailable
                                ? qsTr("按键、语音和自定义快捷键")
                                : SettingsController.deviceCatalogErrorText
                            color: SettingsController.deviceCatalogAvailable ? tokens.textSecondary : tokens.errorColor
                            font.pixelSize: 10
                            visible: !SettingsController.deviceCatalogAvailable || text.length > 0
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12

                    UiCard {
                        visible: SettingsController.isRc003Device
                        Layout.fillWidth: true
                        Layout.fillHeight: false
                        Layout.preferredHeight: controlColumn.implicitHeight + 36
                        Layout.maximumHeight: controlColumn.implicitHeight + 36
                        implicitHeight: controlColumn.implicitHeight + 36
                        fillColor: tokens.surface
                        strokeColor: tokens.border
                        cornerRadius: tokens.cornerRadiusLarge
                        ColumnLayout {
                            id: controlColumn
                            x: 18
                            y: 18
                            width: parent.width - 36
                            spacing: 7
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("连接控制"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 15 }
                                Item { Layout.fillWidth: true }
                                Rectangle { width: 9; height: 9; radius: 5; color: SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active" ? tokens.successColor : SettingsController.bridgeRuntimeState === "error" ? tokens.errorColor : tokens.accent }
                                Label { objectName: "bridgeRuntimeStatusLabel"; text: SettingsController.bridgeRuntimeStatusText; color: tokens.textSecondary; font.pixelSize: tokens.fontSizeSmall }
                            }
                            Label {
                                objectName: "bridgeLaunchResultLabel"
                                Layout.fillWidth: true
                                visible: false
                                text: SettingsController.launchStatusText
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: qsTr("关掉这个窗口不会停止按键。不想再由本软件映射时，点停止接管。")
                                color: tokens.textSecondary
                                font.pixelSize: 12
                            }
                            RowLayout {
                                spacing: 8
                                UiButton {
                                    objectName: "startTakeoverButton"
                                    text: qsTr("开始接管")
                                    primary: true
                                    enabled: !SettingsController.takeoverRunning
                                    onClicked: SettingsController.startTakeover()
                                }
                                UiButton {
                                    objectName: "stopTakeoverButton"
                                    text: qsTr("停止接管")
                                    enabled: SettingsController.takeoverRunning
                                    onClicked: SettingsController.stopTakeover()
                                }
                                UiButton { id: openLogButton; objectName: "openLogButton"; text: qsTr("打开日志目录"); quiet: true; onClicked: SettingsController.openLogLocation() }
                            }
                        }
                    }

                    UiCard {
                        visible: SettingsController.isRc003Device
                        Layout.fillWidth: true
                        Layout.fillHeight: false
                        Layout.preferredHeight: voiceColumn.implicitHeight + 36
                        Layout.maximumHeight: voiceColumn.implicitHeight + 36
                        implicitHeight: voiceColumn.implicitHeight + 36
                        fillColor: tokens.surface
                        strokeColor: tokens.border
                        cornerRadius: tokens.cornerRadiusLarge
                        ColumnLayout {
                            id: voiceColumn
                            x: 18
                            y: 18
                            width: parent.width - 36
                            spacing: 8
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("语音输出"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 15 }
                                Item { Layout.fillWidth: true }
                                Label { text: SettingsController.selectedEndpointIndex >= 0 ? qsTr("当前设备") : qsTr("尚未选择"); color: SettingsController.selectedEndpointIndex >= 0 ? tokens.successColor : tokens.textSecondary; font.pixelSize: 10 }
                            }
                            ComboBox { id: endpointCombo; objectName: "endpointCombo"; Layout.fillWidth: true; model: SettingsController.endpointOptions; currentIndex: SettingsController.selectedEndpointIndex; onActivated: SettingsController.selectedEndpointIndex = index; Accessible.name: qsTr("语音输出设备") }
                            Label { Layout.fillWidth: true; text: qsTr("只写入这块虚拟声卡，不改系统默认设备。"); color: tokens.textSecondary; font.pixelSize: 12; wrapMode: Text.WordWrap }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 220
                        spacing: 16

                    UiCard {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 280
                        Layout.maximumWidth: 340
                        fillColor: tokens.surface
                        strokeColor: tokens.border
                        cornerRadius: tokens.cornerRadiusLarge
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 18
                            spacing: 10
                            Label { text: qsTr("语音输入法"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 15 }
                            Item {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                ColumnLayout {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.verticalCenterOffset: -36
                                    spacing: 10
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("输入法")
                                color: tokens.textSecondary
                                font.pixelSize: 12
                            }
                            ComboBox {
                                id: connectionVoicePresetCombo
                                objectName: "connectionVoicePresetCombo"
                                Layout.fillWidth: true
                                implicitHeight: 40
                                model: SettingsController.voicePresetOptions
                                currentIndex: Math.max(0, SettingsController.voicePresetIds.indexOf(SettingsController.voicePresetId))
                                onActivated: {
                                    var presetId = SettingsController.voicePresetIds[index]
                                    SettingsController.applyVoicePreset(presetId)
                                    if (presetId === "custom")
                                        voiceChordField.forceActiveFocus()
                                }
                                Accessible.name: qsTr("语音输入法")
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("按住的键")
                                color: tokens.textSecondary
                                font.pixelSize: 12
                            }
                            Rectangle {
                                id: voiceChordField
                                objectName: "connectionVoiceChordField"
                                Layout.fillWidth: true
                                implicitHeight: 44
                                radius: 10
                                activeFocusOnTab: SettingsController.voicePresetId === "custom"
                                property bool capturing: false
                                color: capturing ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.12) : "#f4f6f8"
                                border.width: capturing || activeFocus ? 1 : 0
                                border.color: tokens.accent
                                function startCapture() {
                                    if (capturing || SettingsController.voicePresetId !== "custom")
                                        return
                                    capturing = true
                                    SettingsController.startHotkeyCapture()
                                }
                                function stopCapture() {
                                    if (!capturing)
                                        return
                                    capturing = false
                                    SettingsController.stopHotkeyCapture()
                                }
                                onActiveFocusChanged: activeFocus ? startCapture() : stopCapture()
                                Label {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.leftMargin: 12
                                    anchors.rightMargin: 12
                                    text: voiceChordField.capturing
                                          ? qsTr("请按下要按住的键")
                                          : qsTr("按住  %1").arg(root.voiceChordLabel(SettingsController.hotkeyText))
                                    color: voiceChordField.capturing ? Qt.darker(tokens.accent, 1.25) : tokens.textPrimary
                                    font.pixelSize: 14
                                    font.bold: true
                                    elide: Text.ElideRight
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    enabled: SettingsController.voicePresetId === "custom"
                                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                    onClicked: voiceChordField.forceActiveFocus()
                                }
                                Connections {
                                    target: SettingsController
                                    function onHotkeyCaptured(chord) {
                                        if (!voiceChordField.capturing)
                                            return
                                        voiceChordField.stopCapture()
                                        SettingsController.commitCustomVoiceHotkey(chord)
                                    }
                                    function onHotkeyCaptureError(message) {
                                        if (voiceChordField.capturing)
                                            voiceChordField.stopCapture()
                                    }
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: SettingsController.voicePresetId === "wechat"
                                wrapMode: Text.WordWrap
                                text: qsTr("右 Alt 会唤起豆包。请把微信输入法的语音快捷键改成右 Ctrl + 空格。")
                                color: tokens.textSecondary
                                font.pixelSize: 12
                            }
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: text.length > 0
                                wrapMode: Text.WordWrap
                                text: SettingsController.voiceFeedback
                                color: SettingsController.voiceFeedbackIsError ? tokens.errorColor : tokens.successColor
                                font.pixelSize: 12
                            }
                        }
                    }

                    UiCard {
                        objectName: "usageSummaryCard"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        fillColor: tokens.surface
                        strokeColor: tokens.border
                        cornerRadius: tokens.cornerRadiusLarge
                        ColumnLayout {
                            id: usageColumn
                            anchors.fill: parent
                            anchors.margins: 18
                            spacing: 10
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: qsTr("最近 7 天"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 15 }
                                Item { Layout.fillWidth: true }
                                Label {
                                    visible: SettingsController.statisticsButtonTotal > 0 || SettingsController.statisticsVoiceSeconds > 0
                                    text: qsTr("今天 %1 次 · 语音 %2")
                                          .arg(SettingsController.statisticsButtonTotal)
                                          .arg(root.formatDuration(SettingsController.statisticsVoiceSeconds))
                                    color: tokens.textSecondary
                                    font.pixelSize: 11
                                }
                                UiButton {
                                    objectName: "statisticsRefreshButton"
                                    text: qsTr("刷新")
                                    quiet: true
                                    implicitWidth: 64
                                    onClicked: SettingsController.refreshStatistics()
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.minimumHeight: 148
                                Layout.maximumHeight: 280
                                Layout.alignment: Qt.AlignTop
                                spacing: 8
                                Repeater {
                                    model: SettingsController.statisticsDailyRows
                                    delegate: ColumnLayout {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        spacing: 6
                                        Item {
                                            Layout.fillWidth: true
                                            Layout.fillHeight: true
                                            Rectangle {
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                anchors.bottom: parent.bottom
                                                width: 22
                                                height: Math.max(6, parent.height * (Number(modelData.button_count) || 0) / root.dailyPeak())
                                                radius: 6
                                                color: (Number(modelData.button_count) || 0) > 0 ? tokens.accent : tokens.border
                                            }
                                        }
                                        Label {
                                            Layout.alignment: Qt.AlignHCenter
                                            text: modelData.label
                                            color: tokens.textSecondary
                                            font.pixelSize: 10
                                        }
                                    }
                                }
                            }
                            Label {
                                visible: SettingsController.statisticsButtonRows.length === 0 && SettingsController.statisticsButtonTotal === 0
                                text: qsTr("还没有按键记录")
                                color: tokens.textSecondary
                                font.pixelSize: 11
                            }
                        }
                    }
                    }
                }
            }

            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: SettingsController.errorMessage.length > 0 ? SettingsController.errorMessage : SettingsController.statusMessage
                color: SettingsController.errorMessage.length > 0 ? tokens.errorColor : tokens.successColor
                font.pixelSize: tokens.fontSizeSmall
                elide: Text.ElideRight
            }
            Item { Layout.preferredHeight: 8 }
        }
    }
}
