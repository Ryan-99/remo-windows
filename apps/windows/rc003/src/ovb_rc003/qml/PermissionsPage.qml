import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    readonly property bool linkUp: SettingsController.bridgeRuntimeState === "connected" || SettingsController.bridgeRuntimeState === "voice_active"

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: root.width - 84
            x: 42
            y: 8
            spacing: 16

            UiCard {
                Layout.fillWidth: true
                implicitHeight: statusColumn.implicitHeight + 40

                ColumnLayout {
                    id: statusColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 20
                    spacing: 12

                    Label {
                        text: qsTr("当前状态")
                        color: tokens.textPrimary
                        font.pixelSize: 16
                        font.bold: true
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        Rectangle { width: 9; height: 9; radius: 5; color: root.linkUp ? tokens.successColor : SettingsController.bridgeRuntimeState === "error" ? tokens.errorColor : tokens.accent }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label { text: qsTr("遥控器"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 13 }
                            Label {
                                objectName: "permissionBridgeStatus"
                                Layout.fillWidth: true
                                text: SettingsController.bridgeRuntimeStatusText
                                color: tokens.textSecondary
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                }
            }

            UiCard {
                Layout.fillWidth: true
                implicitHeight: startupColumn.implicitHeight + 40
                ColumnLayout {
                    id: startupColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 20
                    spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 16
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Label {
                                text: qsTr("开机自启")
                                color: tokens.textPrimary
                                font.pixelSize: 16
                                font.bold: true
                            }
                            Label {
                                objectName: "launchAtLoginHint"
                                Layout.fillWidth: true
                                text: qsTr("登录 Windows 后自动启动遥控器。默认关闭。")
                                color: tokens.textSecondary
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                        }
                        Switch {
                            id: launchAtLoginSwitch
                            objectName: "launchAtLoginSwitch"
                            checked: SettingsController.launchAtLogin
                            onToggled: {
                                if (launchAtLoginSwitch._applying)
                                    return
                                launchAtLoginSwitch._applying = true
                                SettingsController.launchAtLogin = checked
                                launchAtLoginSwitch.checked = SettingsController.launchAtLogin
                                launchAtLoginSwitch._applying = false
                            }
                            property bool _applying: false
                        }
                    }
                }
            }

            UiCard {
                Layout.fillWidth: true
                implicitHeight: actionColumn.implicitHeight + 40
                ColumnLayout {
                    id: actionColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 20
                    spacing: 12
                    Label { text: qsTr("连不上或要反馈时"); color: tokens.textPrimary; font.pixelSize: 16; font.bold: true }
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("系统入口在这里。导出的日志只有运行状态和按键名，没有录音，打包后把压缩包发回来即可。")
                        color: tokens.textSecondary
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                    Flow {
                        Layout.fillWidth: true
                        width: parent.width
                        spacing: 8
                        UiButton {
                            objectName: "openBluetoothSettingsButton"
                            text: qsTr("打开蓝牙设置")
                            onClicked: SettingsController.openBluetoothSettings()
                        }
                        UiButton {
                            objectName: "openMicrophonePrivacyButton"
                            text: qsTr("打开麦克风隐私设置")
                            onClicked: SettingsController.openMicrophonePrivacySettings()
                        }
                        UiButton {
                            objectName: "openSoundSettingsButton"
                            text: qsTr("打开声音输入/输出设置")
                            onClicked: SettingsController.openSoundSettings()
                        }
                        UiButton {
                            objectName: "openAppsSettingsButton"
                            text: qsTr("打开应用设置")
                            onClicked: SettingsController.openAppsSettings()
                        }
                        UiButton {
                            objectName: "permissionsOpenLogButton"
                            text: qsTr("打开日志目录")
                            onClicked: SettingsController.openLogLocation()
                        }
                        UiButton {
                            objectName: "exportLogsButton"
                            text: qsTr("导出日志")
                            primary: true
                            onClicked: SettingsController.exportSupportBundle()
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 16 }
        }
    }
}
