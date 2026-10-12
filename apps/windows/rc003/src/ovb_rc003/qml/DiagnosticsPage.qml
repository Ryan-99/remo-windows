// "检查与修复" tab (XRBM-031 In-scope items 1/4/5/6): a fourth top-level page
// that groups every windows_diagnostics.py check into distinct categories:
// ordinary buttons, RC003 voice, external microphones, Windows dictation,
// and the optional third-party driver state. It never lets pairing alone look
// like proof that buttons or speech actually work. DiagnosticsController
// runs every check off the Qt GUI thread (see qt_settings_app.py's module
// docstring), so "重新检测" never freezes this window.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    // Stable ids from windows_diagnostics.py's CheckGroup enum.
    readonly property string groupOrdinaryButtons: "ordinary_buttons"
    readonly property string groupVoiceBridge: "voice_bridge"
    readonly property string groupDictation: "dictation"
    readonly property string groupOptionalDriver: "optional_driver"
    readonly property string groupExternalMicrophone: "external_microphone"

    function rowsForGroup(groupId) {
        var rows = []
        var results = DiagnosticsController.checkResults
        for (var i = 0; i < results.length; i++) {
            if (results[i].group === groupId) {
                rows.push(results[i])
            }
        }
        return rows
    }

    function statusColor(status) {
        if (status === "pass") return tokens.successColor
        if (status === "fail") return tokens.errorColor
        if (status === "manual") return tokens.voiceAccent
        return tokens.textSecondary // "unsupported"
    }

    function statusLabel(status) {
        if (status === "pass") return qsTr("正常")
        if (status === "fail") return qsTr("未通过")
        if (status === "manual") return qsTr("待手动验证")
        return qsTr("不支持/不可用")
    }

    function inspectionGuide(checkId) {
        if (checkId === "ble_candidate")
            return { hint: qsTr("到系统蓝牙里确认遥控器已配对，并让它保持唤醒。"), action: "bluetooth", label: qsTr("打开蓝牙设置") }
        if (checkId === "vb_cable_endpoints")
            return { hint: qsTr("没有虚拟声卡，语音送不进输入法。安装基础版 VB-CABLE 后重启电脑。"), action: "install", label: qsTr("安装 VB-CABLE") }
        if (checkId === "output_endpoint")
            return { hint: qsTr("当前输出没有对上 CABLE Input。改过去之后，豆包要从 CABLE Output 录音。"), action: "cable", label: qsTr("改用 CABLE Input") }
        if (checkId === "winuhid")
            return { hint: qsTr("虚拟键盘没有就绪，输入法唤不醒。先看日志，再重新打开 Remo。"), action: "log", label: qsTr("打开日志") }
        if (checkId === "raw_input")
            return { hint: qsTr("系统还没看到按键设备。重新插上或唤醒遥控器后再巡检一次。"), action: "log", label: qsTr("打开日志") }
        if (checkId === "os_version")
            return { hint: qsTr("需要 64 位 Windows 10 或 11。这个只能换系统，程序里修不了。"), action: "", label: "" }
        if (checkId === "dictation")
            return { hint: qsTr("Windows 听写没有就绪。Remo 自己的语音不依赖它，只有你要用 Win+H 时才需要。"), action: "speech", label: qsTr("打开语音设置") }
        return { hint: qsTr("看下面这一项的说明，处理完再点重新检测。"), action: "log", label: qsTr("打开日志") }
    }

    function inspectionItems() {
        var items = []
        var results = DiagnosticsController.checkResults
        for (var i = 0; i < results.length; i++) {
            var row = results[i]
            if (row.status !== "fail")
                continue
            var guide = inspectionGuide(row.checkId)
            items.push({
                checkId: row.checkId,
                title: row.title,
                detail: row.detail,
                hint: guide.hint,
                action: guide.action,
                label: guide.label
            })
        }
        return items
    }

    function runInspectionAction(action) {
        if (action === "bluetooth")
            SettingsController.openBluetoothSettings()
        else if (action === "install")
            driverConfirmDialog.open()
        else if (action === "cable")
            DiagnosticsController.selectDetectedCableInputAsOutput()
        else if (action === "speech")
            SettingsController.openSpeechSettings()
        else if (action === "log")
            SettingsController.openLogLocation()
    }

    Dialog {
        id: driverConfirmDialog
        objectName: "driverConfirmDialog"
        title: qsTr("启动 VB-CABLE 官方安装程序？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        Label {
            width: 360
            wrapMode: Text.WordWrap
            text: qsTr("即将启动 VB-Audio 官方 VB-CABLE 安装程序（VBCABLE_Setup_x64.exe），"
                + "会弹出 Windows 用户账户控制 (UAC) 提示请求管理员权限——本程序自身不会以管理员"
                + "身份运行。VB-CABLE 是独立的 Donationware（非 GPL 项目代码），来自 VB-Audio，"
                + "见 https://www.vb-cable.com；仅随包提供基础版，不包含付费的 A+B / C+D。"
                + "安装后需要重启电脑，安装/卸载过程都会改变系统状态。确定要继续吗？")
        }
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: root.width - tokens.spacingLarge * 2
            x: tokens.spacingLarge
            y: tokens.spacingLarge
            spacing: tokens.spacingLarge

            Rectangle {
                id: inspectionCard
                objectName: "inspectionCard"
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: inspectionItems().length > 0
                       ? Qt.rgba(tokens.errorColor.r, tokens.errorColor.g, tokens.errorColor.b, 0.08)
                       : tokens.surface
                border.color: inspectionItems().length > 0 ? tokens.errorColor : tokens.border
                border.width: 1
                implicitHeight: inspectionColumn.implicitHeight + 28

                ColumnLayout {
                    id: inspectionColumn
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        Label {
                            Layout.fillWidth: true
                            text: DiagnosticsController.isRefreshing && DiagnosticsController.checkResults.length === 0
                                  ? qsTr("正在巡检…")
                                  : (inspectionItems().length > 0
                                     ? qsTr("巡检发现 %1 个问题").arg(inspectionItems().length)
                                     : qsTr("巡检正常"))
                            color: tokens.textPrimary
                            font.pixelSize: 18
                            font.bold: true
                        }
                        BusyIndicator {
                            id: refreshBusyIndicator
                            objectName: "refreshBusyIndicator"
                            running: DiagnosticsController.isRefreshing
                            visible: running
                            implicitWidth: 22
                            implicitHeight: 22
                        }
                        UiButton {
                            id: refreshButton
                            objectName: "refreshButton"
                            text: qsTr("重新检测")
                            primary: true
                            large: true
                            enabled: !DiagnosticsController.isRefreshing
                            onClicked: DiagnosticsController.refreshDiagnostics()
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: inspectionItems().length === 0 && !DiagnosticsController.isRefreshing
                        wrapMode: Text.WordWrap
                        text: qsTr("遥控器、语音输出和虚拟声卡都没有需要立刻处理的问题。")
                        color: tokens.textSecondary
                        font.pixelSize: 12
                    }
                    Repeater {
                        model: inspectionItems()
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 4
                            Label {
                                Layout.fillWidth: true
                                text: modelData.title
                                color: tokens.textPrimary
                                font.bold: true
                                font.pixelSize: 13
                                wrapMode: Text.WordWrap
                            }
                            Label {
                                Layout.fillWidth: true
                                text: modelData.hint
                                color: tokens.textSecondary
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                            UiButton {
                                visible: modelData.label.length > 0
                                text: modelData.label
                                primary: true
                                onClicked: root.runInspectionAction(modelData.action)
                            }
                        }
                    }
                }
            }

            // Prominent, page-level error (XRBM-031 RETRY 1 item 2): shown
            // right under the intro text - NOT only as a driver-card
            // message below the fold - whenever the background diagnostics
            // run itself failed unexpectedly, so a user is never left
            // looking at a blank/stale-looking page with no explanation.
            Rectangle {
                id: diagnosticsErrorBanner
                objectName: "diagnosticsErrorBanner"
                Layout.fillWidth: true
                visible: DiagnosticsController.diagnosticsErrorMessage.length > 0
                radius: tokens.cornerRadiusSmall
                color: Qt.rgba(tokens.errorColor.r, tokens.errorColor.g, tokens.errorColor.b, 0.12)
                border.color: tokens.errorColor
                border.width: 1
                implicitHeight: diagnosticsErrorLabel.implicitHeight + tokens.spacingMedium * 2

                Label {
                    id: diagnosticsErrorLabel
                    anchors.fill: parent
                    anchors.margins: tokens.spacingMedium
                    wrapMode: Text.WordWrap
                    text: DiagnosticsController.diagnosticsErrorMessage
                    color: tokens.errorColor
                    font.pixelSize: tokens.fontSizeSmall
                }
            }

            // -- Group: ordinary buttons -------------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: ordinaryButtonsColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: ordinaryButtonsColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("普通按键前提")
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Repeater {
                        model: root.rowsForGroup(root.groupOrdinaryButtons)
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            spacing: tokens.spacingSmall
                            Rectangle {
                                width: 10; height: 10; radius: 5
                                color: root.statusColor(modelData.status)
                            }
                            Label {
                                Layout.preferredWidth: 160
                                text: modelData.title
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: root.statusLabel(modelData.status) + " — " + modelData.detail
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                    }
                }
            }

            // -- Group: RC003 voice bridge -----------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: voiceBridgeColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: voiceBridgeColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("RC003 语音链路前提")
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Repeater {
                        model: root.rowsForGroup(root.groupVoiceBridge)
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            spacing: tokens.spacingSmall
                            Rectangle {
                                width: 10; height: 10; radius: 5
                                color: root.statusColor(modelData.status)
                            }
                            Label {
                                Layout.preferredWidth: 160
                                text: modelData.title
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: root.statusLabel(modelData.status) + " — " + modelData.detail
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                    }
                }
            }


            // -- Group: Windows dictation (always manual) --------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: dictationColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: dictationColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("Windows 听写前提")
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Repeater {
                        model: root.rowsForGroup(root.groupDictation)
                        delegate: ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            RowLayout {
                                spacing: tokens.spacingSmall
                                Rectangle {
                                    width: 10; height: 10; radius: 5
                                    color: root.statusColor(modelData.status)
                                }
                                Label {
                                    text: modelData.title + " — " + root.statusLabel(modelData.status)
                                    color: tokens.textPrimary
                                    font.pixelSize: tokens.fontSizeSmall
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: modelData.detail
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                    }
                    RowLayout {
                        spacing: tokens.spacingSmall
                        Button {
                            text: qsTr("打开语音识别设置")
                            onClicked: SettingsController.openSpeechSettings()
                        }
                        Button {
                            text: qsTr("打开麦克风隐私设置")
                            onClicked: SettingsController.openMicrophonePrivacySettings()
                        }
                    }
                }
            }

            // -- Group: optional VB-CABLE driver ------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: driverColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: driverColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("可选：VB-CABLE 虚拟音频驱动")
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("VB-CABLE by VB-Audio（https://www.vb-cable.com）——独立的 "
                            + "Donationware，不是本项目 GPL-3.0 代码的一部分，可自愿捐赠/购买授权。"
                            + "本页仅随包提供基础版（Basic）安装包，不包含付费的 A+B / C+D 版本。"
                            + "安装会改变系统状态、请求管理员权限，安装/卸载都需要重启；本程序"
                            + "从不修改 Windows 默认输入/输出设备。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Repeater {
                        model: root.rowsForGroup(root.groupOptionalDriver)
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            spacing: tokens.spacingSmall
                            Rectangle {
                                width: 10; height: 10; radius: 5
                                color: root.statusColor(modelData.status)
                            }
                            Label {
                                Layout.preferredWidth: 160
                                text: modelData.title
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: root.statusLabel(modelData.status) + " — " + modelData.detail
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                    }
                    RowLayout {
                        spacing: tokens.spacingSmall
                        Button {
                            id: selectCableInputButton
                            objectName: "selectCableInputButton"
                            text: qsTr("选择检测到的 CABLE Input 作为输出")
                            onClicked: DiagnosticsController.selectDetectedCableInputAsOutput()
                        }
                        Button {
                            id: launchDriverSetupButton
                            objectName: "launchDriverSetupButton"
                            text: qsTr("安装/修复 VB-CABLE…")
                            onClicked: driverConfirmDialog.open()
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        visible: text.length > 0
                        text: DiagnosticsController.driverErrorMessage
                        color: tokens.errorColor
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        visible: text.length > 0 && DiagnosticsController.driverErrorMessage.length === 0
                        text: DiagnosticsController.driverStatusMessage
                        color: tokens.successColor
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    // Neutral/informational outcome (XRBM-031 RETRY 1 item
                    // 7): a UAC cancellation or "vendor setup launched"
                    // notice - neither is a completed success, so neither
                    // ever renders in the success (green) color above.
                    Label {
                        id: driverInfoLabel
                        objectName: "driverInfoLabel"
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        visible: text.length > 0
                            && DiagnosticsController.driverErrorMessage.length === 0
                            && DiagnosticsController.driverStatusMessage.length === 0
                        text: DiagnosticsController.driverInfoMessage
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            UiCard {
                objectName: "diagnosticsUsageCard"
                Layout.fillWidth: true
                Layout.fillHeight: false
                visible: SettingsController.statisticsButtonRows.length > 0
                implicitHeight: diagnosticsUsageColumn.implicitHeight + 36
                fillColor: tokens.surface
                strokeColor: tokens.border
                cornerRadius: tokens.cornerRadiusLarge
                ColumnLayout {
                    id: diagnosticsUsageColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 18
                    spacing: 8
                    Label {
                        text: qsTr("按键记录")
                        font.bold: true
                        color: tokens.textPrimary
                        font.pixelSize: 15
                    }
                    Repeater {
                        model: SettingsController.statisticsButtonRows
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Label { text: modelData.name; color: tokens.textPrimary; font.pixelSize: 12; Layout.fillWidth: true }
                            Label { text: String(modelData.count); color: tokens.textSecondary; font.pixelSize: 12 }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.fieldBackground
                border.color: tokens.border
                border.width: 1
                implicitHeight: debugColumn.implicitHeight + tokens.spacingMedium * 2

                ColumnLayout {
                    id: debugColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: tokens.spacingMedium
                    spacing: tokens.spacingSmall
                    Label {
                        text: qsTr("最近按键调试")
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Label {
                        Layout.fillWidth: true
                        text: SettingsController.lastButtonDebug
                        elide: Text.ElideRight
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("完整链路写入 app.log：物理报告 → 映射 → 动作开始/完成/失败")
                        color: tokens.textTertiary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        text: SettingsController.bridgeRuntimeStatusText
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 132
                        color: tokens.surface
                        radius: tokens.cornerRadiusSmall
                        clip: true
                        ScrollView {
                            anchors.fill: parent
                            anchors.margins: 8
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                            ListView {
                                id: liveDebugList
                                model: SettingsController.liveDebugEvents
                                spacing: 3
                                delegate: Label {
                                    width: liveDebugList.width
                                    text: modelData
                                    color: tokens.textSecondary
                                    font.family: "Consolas"
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                }
                                onCountChanged: if (count > 0) positionViewAtEnd()
                            }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.spacingLarge }
        }
    }
}
